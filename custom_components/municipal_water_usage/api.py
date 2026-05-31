"""Municipal Water Usage API client.

Pulls hourly/daily water consumption from a Tyler Smart Meters utility portal
hosted behind ``*.municipalonlinepayments.com`` (default: City of Bastrop, TX).

Flow:

1. ``async_login`` performs the multi-step OpenID Connect dance against the
   shared identity provider at ``account.municipalonlinepayments.com``. The
   resulting cookies (``.AspNet.Cookies`` etc.) are kept in this client's
   ``aiohttp.ClientSession`` cookie jar.
2. ``async_get_chart_context`` fetches the per-account consumption page on the
   tenant host (e.g. ``bastroptx.municipalonlinepayments.com``) and extracts the
   short-lived JWT, meter info, billing history JSON, and account start date
   from the ``<script src="https://www.tylersmartmeters.com/charts.js" data-*>``
   block.
3. ``async_get_usage`` POSTs the JWT plus an interval/date window to
   ``https://www.tylersmartmeters.com/``. The response is an HTML page with
   ``series0.push(['MM/DD/YYYY HH:MM:SS', value]);`` lines embedded in a
   ``<script>``. Values are HGAL (hundreds of gallons), so they are multiplied
   by ``HGAL_TO_GALLONS`` to produce gallons.
"""
from __future__ import annotations

import asyncio
import base64
import html as _html
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import ClientError, ClientTimeout

from .const import (
    DEFAULT_TIMEOUT,
    HGAL_TO_GALLONS,
    IDP_HOST,
    JWT_REFRESH_MARGIN,
    MAX_RETRIES,
    METER_LAST_REPORTED_KEY,
    METER_NAME,
    METER_REGISTER_READ_KEY,
    RETRY_DELAY,
    SESSION_TIMEOUT,
    TSM_DAY_FETCH_DELAY,
    TSM_HOST,
    USER_AGENT,
)
from .exceptions import (
    WaterUsageAuthenticationError,
    WaterUsageConnectionError,
    WaterUsageDataError,
    WaterUsageError,
)
from .utils import sanitize_host

_LOGGER = logging.getLogger(__name__)

# Regexes used to scrape data from HTML responses.
#
# Real-world HTML from this portal mixes single and double quotes for
# attribute values — the IDP's login form uses ``"`` but the OIDC
# ``form_post`` callback page uses ``'``. Every regex below tolerates either
# by accepting ``["']`` around the value and capturing whatever's in between.
_TOKEN_FIELD_RE = re.compile(
    r"""<input[^>]*name=["']__RequestVerificationToken["']"""
    r"""[^>]*value=["']([^"']+)["']""",
    re.IGNORECASE,
)
_RETURN_URL_RE = re.compile(
    r"""<input[^>]*name=["']ReturnUrl["'][^>]*value=["']([^"']+)["']""",
    re.IGNORECASE,
)
_OIDC_FORM_ACTION_RE = re.compile(
    r"""<form[^>]*method=["']post["'][^>]*action=["']([^"']+)["']""",
    re.IGNORECASE,
)
# Hidden inputs: name and value may appear in either order, and type may
# be omitted in some renderings. We grab every attribute on the tag and
# look the name/value pair up afterwards.
_HIDDEN_INPUT_TAG_RE = re.compile(r"<input\b([^>]*)>", re.IGNORECASE)
_ATTR_RE = re.compile(
    r"""([a-zA-Z][a-zA-Z0-9_-]*)\s*=\s*["']([^"']*)["']"""
)
_CHARTS_SCRIPT_RE = re.compile(
    r"""<script[^>]*src=["']https://www\.tylersmartmeters\.com/charts\.js["']"""
    r"""[^>]*>""",
    re.IGNORECASE,
)
_DATA_ATTR_RE = re.compile(
    r"""data-([a-z0-9-]+)=["']([^"']*)["']""",
    re.IGNORECASE,
)
_SERIES_PUSH_RE = re.compile(
    r"series0\.push\(\['([^']+)',\s*(-?[\d.]+)\]\);"
)
# TSM marks hours still awaiting meter data with missingReads.push(index)
# immediately before a placeholder series0 value (typically 0.1 HGAL).
_MISSING_READS_PUSH_RE = re.compile(r"missingReads\.push\((\d+)\)")
# Billing sidebar on TSM chart pages (forge UI).
_METER_LAST_REPORTED_RE = re.compile(
    r"Meter last reported.*?(\d{1,2}/\d{1,2}/\d{4}\s*-\s*\d{1,2}:\d{2}\s*(?:AM|PM))",
    re.IGNORECASE | re.DOTALL,
)
_METER_REGISTER_READ_ARIA_RE = re.compile(
    r"""aria-label=["']Read:\s*([\d,.]+)["']""",
    re.IGNORECASE,
)
_METER_REGISTER_READ_TEXT_RE = re.compile(
    r"Read:\s*([\d,.]+)",
    re.IGNORECASE,
)


class Aggregation(StrEnum):
    """Supported usage aggregation buckets."""

    HOURLY = "HOURLY"
    DAILY = "DAILY"

    @property
    def label(self) -> str:
        if self == Aggregation.HOURLY:
            return "Hourly"
        if self == Aggregation.DAILY:
            return "Daily"
        return "Unknown"

    @property
    def suffix(self) -> str:
        if self == Aggregation.HOURLY:
            return ""
        if self == Aggregation.DAILY:
            return "_daily"
        return "_unknown"

    @property
    def period(self) -> str:
        if self == Aggregation.HOURLY:
            return "hour"
        if self == Aggregation.DAILY:
            return "day"
        return "unknown"

    @property
    def interval_type(self) -> str:
        """Tyler Smart Meters interval_type form value."""
        if self == Aggregation.HOURLY:
            return "H"
        if self == Aggregation.DAILY:
            return "D"
        return "D"


class MunicipalWaterAPI:
    """Async client for a municipalonlinepayments + tylersmartmeters portal."""

    def __init__(
        self,
        email: str,
        password: str,
        account_id: str,
        host: str,
        timezone: str,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.email = email
        self.password = password
        self.account_id = account_id
        self.host = sanitize_host(host)
        self.timezone = timezone
        self.timeout = timeout

        self._session: Optional[aiohttp.ClientSession] = None
        self._session_created_at: Optional[datetime] = None
        self._authenticated: bool = False

        # Populated by async_get_chart_context.
        self._jwt: Optional[str] = None
        self._jwt_exp: Optional[int] = None
        self._meter_info_json: Optional[str] = None
        self._chart_info_json: Optional[str] = None
        self._monthly_bills_json: Optional[str] = None
        self._account_start_date: Optional[str] = None
        self._meter_name: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Session lifecycle
    # ------------------------------------------------------------------ #

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return a session with a cookie jar, recycling stale ones."""
        now = datetime.now()

        if (
            self._session_created_at
            and (now - self._session_created_at).total_seconds() > SESSION_TIMEOUT
        ):
            _LOGGER.debug("Session timeout reached, refreshing aiohttp session")
            await self._close_session()

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=ClientTimeout(total=self.timeout),
                connector=aiohttp.TCPConnector(ssl=True, limit=10),
                cookie_jar=aiohttp.CookieJar(),
                headers={"User-Agent": USER_AGENT},
            )
            self._session_created_at = now
            self._authenticated = False
            self._jwt = None
            self._jwt_exp = None
            _LOGGER.debug("Created new aiohttp session")

        return self._session

    async def _close_session(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        self._session_created_at = None
        self._authenticated = False
        self._jwt = None
        self._jwt_exp = None

    async def close(self) -> None:
        """Public close hook used during unload."""
        await self._close_session()

    async def _refresh_authentication(self) -> None:
        """Throw away everything and log in from scratch."""
        _LOGGER.debug("Refreshing authentication from scratch")
        await self._close_session()
        await self.async_login()

    # ------------------------------------------------------------------ #
    # OpenID Connect login
    # ------------------------------------------------------------------ #

    async def async_login(self) -> None:
        """Run the full OIDC dance against account.municipalonlinepayments.com.

        Steps:

        1. ``GET https://{host}/bastroptx/login?returnUrl=...`` — kicks off the
           OIDC redirect to the identity provider.
        2. ``GET https://account.../Account/Login?ReturnUrl=...`` — login form;
           we scrape ``__RequestVerificationToken`` and ``ReturnUrl`` from it.
        3. ``POST`` the form with ``Email`` + ``Password`` (the matching
           ``.AspNetCore.Antiforgery.*`` cookie was set on step 2 and is carried
           by the cookie jar).
        4. The 200 response is the OIDC callback page containing an
           auto-submit ``<form action=".../signin-oidc" method="post">``. We
           parse its hidden inputs and POST them ourselves — aiohttp does not
           auto-submit HTML forms.
        """
        session = await self._get_session()

        login_kickoff_url = (
            f"https://{self.host}/bastroptx/login?"
            f"returnUrl=%2Fbastroptx%2Futilities"
        )

        try:
            # Steps 1 + 2: follow redirects until we land on the login form.
            async with session.get(
                login_kickoff_url, allow_redirects=True
            ) as response:
                login_form_url = str(response.url)
                if response.status != 200:
                    raise WaterUsageConnectionError(
                        f"Could not load login form (HTTP {response.status})"
                    )
                login_html = await response.text()

            csrf_match = _TOKEN_FIELD_RE.search(login_html)
            return_url_match = _RETURN_URL_RE.search(login_html)
            if not csrf_match or not return_url_match:
                raise WaterUsageDataError(
                    "Login form did not include the expected hidden fields"
                )
            csrf_token = csrf_match.group(1)
            return_url = _html.unescape(return_url_match.group(1))

            # Step 3: submit credentials.
            payload = {
                "ReturnUrl": return_url,
                "Email": self.email,
                "Password": self.password,
                "button": "login",
                "__RequestVerificationToken": csrf_token,
            }
            async with session.post(
                login_form_url,
                data=payload,
                allow_redirects=True,
            ) as response:
                callback_html = await response.text()
                if response.status >= 400:
                    raise WaterUsageAuthenticationError(
                        f"Login submit failed (HTTP {response.status})"
                    )
                if 'name="Password"' in callback_html:
                    # The IDP re-renders the login form on bad credentials.
                    raise WaterUsageAuthenticationError(
                        "Login rejected; the portal returned the sign-in form again"
                    )
                callback_url = str(response.url)

            # Step 4: parse + post the OIDC form_post callback HTML.
            action, fields = self._parse_oidc_form_post(callback_html, callback_url)
            if action:
                _LOGGER.debug(
                    "Posting OIDC form_post to %s with %d hidden fields",
                    action,
                    len(fields),
                )
                async with session.post(
                    action,
                    data=fields,
                    allow_redirects=True,
                ) as response:
                    if response.status >= 400:
                        raise WaterUsageAuthenticationError(
                            f"OIDC callback POST failed (HTTP {response.status})"
                        )
            else:
                _LOGGER.warning(
                    "OIDC callback page contained no form_post action; "
                    "the bastrop tenant session may not be established"
                )

            # Sanity check: the bastrop tenant session lives in a cookie
            # named ``.AspNet.Cookies``. If we didn't see one set, the OIDC
            # form_post didn't complete and subsequent requests to the
            # tenant will bounce back to /connect/authorize.
            cookie_names = {c.key for c in session.cookie_jar}
            _LOGGER.debug("Cookies after login: %s", sorted(cookie_names))
            if ".AspNet.Cookies" not in cookie_names:
                raise WaterUsageAuthenticationError(
                    "Login appeared to succeed at the identity provider, but "
                    "the tenant session cookie (.AspNet.Cookies) was not set. "
                    "This usually means the OIDC form_post callback failed; "
                    f"received cookies: {sorted(cookie_names)}"
                )

            self._authenticated = True
            _LOGGER.info("Logged in to %s as %s", self.host, self.email)

        except WaterUsageError:
            raise
        except ClientError as err:
            raise WaterUsageConnectionError(
                f"Connection error during login: {err}"
            ) from err

    def _parse_oidc_form_post(
        self, html: str, base_url: str
    ) -> Tuple[Optional[str], Dict[str, str]]:
        """Extract the action URL + hidden inputs of the OIDC ``form_post``.

        The identity provider returns HTML like::

            <form method="post" action="https://.../signin-oidc">
                <input type="hidden" name="code" value="..."/>
                <input type="hidden" name="id_token" value="..."/>
                ...
                <noscript><button type="submit">Continue</button></noscript>
            </form>

        Browsers auto-submit it via inline JavaScript. We replicate that.
        """
        action_match = _OIDC_FORM_ACTION_RE.search(html)
        if not action_match:
            # Not strictly fatal — some flows finish entirely via redirects.
            return None, {}

        action = _html.unescape(action_match.group(1))
        if action.startswith("/"):
            # Resolve relative action against the page's host.
            scheme_host = re.match(r"^(https?://[^/]+)", base_url)
            if scheme_host:
                action = f"{scheme_host.group(1)}{action}"

        fields: Dict[str, str] = {}
        for tag_match in _HIDDEN_INPUT_TAG_RE.finditer(html):
            attrs = {
                m.group(1).lower(): m.group(2)
                for m in _ATTR_RE.finditer(tag_match.group(1))
            }
            # Only collect explicit hidden inputs (the form_post body shouldn't
            # include any non-hidden inputs anyway).
            if attrs.get("type", "").lower() not in ("hidden", ""):
                continue
            name = attrs.get("name")
            if not name:
                continue
            fields[name] = _html.unescape(attrs.get("value", ""))

        return action, fields

    # ------------------------------------------------------------------ #
    # JWT + per-account metadata extraction
    # ------------------------------------------------------------------ #

    @staticmethod
    def _has_tenant_session(session: aiohttp.ClientSession) -> bool:
        """True when the bastrop tenant cookie from OIDC form_post is present."""
        return any(c.key == ".AspNet.Cookies" for c in session.cookie_jar)

    @staticmethod
    def _diagnose_consumption_html(html_body: str, final_url: str) -> Dict[str, Any]:
        """Classify a consumption-page response without logging secrets."""
        lower = html_body.lower()
        url_lower = final_url.lower()
        if "charts.js" in lower and "data-token" in lower:
            page_type = "consumption"
        elif "signin-oidc" in lower or "/connect/authorize" in url_lower:
            page_type = "oidc_interstitial"
        elif "/account/login" in url_lower or 'name="password"' in lower:
            page_type = "login"
        else:
            page_type = "unknown"
        return {
            "page_type": page_type,
            "has_charts_js": "charts.js" in lower,
            "has_data_token": "data-token" in lower,
            "final_url_host": urlparse(final_url).netloc,
            "body_length": len(html_body),
        }

    async def _fetch_consumption_page(
        self, session: aiohttp.ClientSession
    ) -> Tuple[str, str, int]:
        """GET the account consumption page; return ``(html, final_url, status)``."""
        url = (
            f"https://{self.host}/bastroptx/utilities/accounts/consumption/"
            f"{self.account_id}"
        )
        async with session.get(url, allow_redirects=True) as response:
            html_body = await response.text()
            if response.status == 401 or response.status == 403:
                raise WaterUsageAuthenticationError(
                    f"Consumption page returned HTTP {response.status}; "
                    "session may have expired"
                )
            if response.status != 200:
                raise WaterUsageConnectionError(
                    f"Consumption page returned HTTP {response.status}"
                )
            return html_body, str(response.url), response.status

    async def async_get_chart_context(self) -> None:
        """Fetch the consumption page and cache JWT + meter metadata.

        The tenant page embeds the Tyler Smart Meters chart loader as::

            <script src="https://www.tylersmartmeters.com/charts.js"
                    data-token="<JWT>"
                    data-tsm-smartmeter-info="<json-escaped>"
                    data-tsm-chart-info="<json-escaped>"
                    data-monthly-bills="<json-escaped>"
                    data-account-start-date="6/14/2024 12:00:00 AM"
                    ...>
        """
        session = await self._get_session()

        if not self._authenticated or not self._has_tenant_session(session):
            # IDP cookies may still be present while the tenant cookie expired;
            # a full session reset avoids landing on the wrong login HTML.
            await self._refresh_authentication()
            session = await self._get_session()

        for attempt in (1, 2):
            html_body, final_url, status = await self._fetch_consumption_page(session)
            diagnosis = self._diagnose_consumption_html(html_body, final_url)
            _LOGGER.debug(
                "Consumption page attempt %d: status=%s page_type=%s",
                attempt,
                status,
                diagnosis["page_type"],
            )

            try:
                attrs = self._extract_charts_data_attrs(html_body)
            except WaterUsageDataError:
                if attempt == 2:
                    raise WaterUsageDataError(
                        "Could not locate the Tyler Smart Meters chart loader "
                        f"in the page (page_type={diagnosis['page_type']}, "
                        f"url_host={diagnosis['final_url_host']})"
                    ) from None
                _LOGGER.warning(
                    "Chart loader missing (page_type=%s, url_host=%s); "
                    "re-authenticating",
                    diagnosis["page_type"],
                    diagnosis["final_url_host"],
                )
                await self._refresh_authentication()
                session = await self._get_session()
                continue

            token = attrs.get("token")
            if not token:
                raise WaterUsageDataError(
                    "Consumption page did not include a TSM token — "
                    "is the account number correct?"
                )

            self._jwt = token
            self._jwt_exp = self._decode_jwt_exp(token)
            self._meter_info_json = attrs.get("tsm-smartmeter-info")
            self._chart_info_json = attrs.get("tsm-chart-info")
            self._monthly_bills_json = attrs.get("monthly-bills")
            self._account_start_date = attrs.get("account-start-date")
            self._meter_name = self._extract_meter_name(self._meter_info_json)

            _LOGGER.debug(
                "Cached TSM context (attempt %d): meter=%s, jwt_exp=%s",
                attempt,
                self._meter_name,
                self._jwt_exp,
            )
            return

    def _extract_charts_data_attrs(self, html_body: str) -> Dict[str, str]:
        """Pull every ``data-*`` attribute from the charts.js ``<script>`` tag."""
        script_match = _CHARTS_SCRIPT_RE.search(html_body)
        if not script_match:
            raise WaterUsageDataError(
                "Could not locate the Tyler Smart Meters chart loader in the page"
            )
        script_tag = script_match.group(0)
        attrs: Dict[str, str] = {}
        for match in _DATA_ATTR_RE.finditer(script_tag):
            attrs[match.group(1).lower()] = _html.unescape(match.group(2))
        return attrs

    @staticmethod
    def _extract_meter_name(meter_info_json: Optional[str]) -> Optional[str]:
        if not meter_info_json:
            return None
        try:
            data = json.loads(meter_info_json)
            meters = data.get("SmartMeterInfo", [])
            if meters:
                miuid = meters[0].get("MIUID")
                return f"MIU {miuid}" if miuid else None
        except (json.JSONDecodeError, AttributeError, TypeError):
            _LOGGER.debug("Could not parse meter info JSON: %s", meter_info_json)
        return None

    @staticmethod
    def _decode_jwt_exp(token: str) -> Optional[int]:
        """Decode a JWT payload to read the ``exp`` claim (epoch seconds)."""
        try:
            payload_segment = token.split(".")[1]
            # Re-pad before decoding.
            padding = "=" * (-len(payload_segment) % 4)
            payload_json = base64.urlsafe_b64decode(payload_segment + padding)
            payload = json.loads(payload_json)
            exp = payload.get("exp")
            return int(exp) if exp is not None else None
        except (IndexError, ValueError, json.JSONDecodeError) as err:
            _LOGGER.debug("Could not parse JWT exp claim: %s", err)
            return None

    def _jwt_is_expired(self) -> bool:
        if not self._jwt:
            return True
        if not self._jwt_exp:
            return False
        return time.time() + JWT_REFRESH_MARGIN >= self._jwt_exp

    # ------------------------------------------------------------------ #
    # Usage fetch
    # ------------------------------------------------------------------ #

    async def async_get_usage(
        self,
        aggregation: Aggregation,
        start_datetime: datetime,
        end_datetime: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Fetch usage from Tyler Smart Meters for the given window.

        Returns a dict shaped like::

            {"USAGE": [{"reading_time": <aware datetime>,
                        "consumption": <float gallons>,
                        "raw_timestamp": <int epoch ms>},
                       ...],
             "meter_name": "MIU 131356596"}
        """
        if end_datetime is None:
            end_datetime = datetime.now().replace(minute=0, second=0, microsecond=0)

        reauth_attempted = False
        refresh_attempted = False

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if not self._authenticated:
                    await self.async_login()
                if not self._jwt or self._jwt_is_expired():
                    await self.async_get_chart_context()

                body = self._build_tsm_form(aggregation, start_datetime, end_datetime)
                session = await self._get_session()
                headers = {
                    "Origin": f"https://{TSM_HOST}",
                    "Referer": f"https://{TSM_HOST}/",
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                    ),
                }

                async with session.post(
                    f"https://{TSM_HOST}/",
                    data=body,
                    headers=headers,
                    allow_redirects=True,
                ) as response:
                    response_text = await response.text()
                    if response.status in (401, 403):
                        if not refresh_attempted:
                            _LOGGER.info(
                                "TSM rejected JWT (HTTP %s); refreshing context",
                                response.status,
                            )
                            await self.async_get_chart_context()
                            refresh_attempted = True
                            continue
                        if not reauth_attempted:
                            _LOGGER.info(
                                "TSM still rejecting after JWT refresh; "
                                "re-running full login"
                            )
                            await self._refresh_authentication()
                            await self.async_get_chart_context()
                            reauth_attempted = True
                            continue
                        raise WaterUsageAuthenticationError(
                            "TSM kept rejecting requests after full re-login"
                        )
                    if response.status != 200:
                        raise WaterUsageConnectionError(
                            f"TSM responded with HTTP {response.status}"
                        )

                    return self._parse_tsm_html(response_text)

            except WaterUsageAuthenticationError:
                raise
            except ClientError as err:
                if attempt < MAX_RETRIES:
                    _LOGGER.warning(
                        "TSM request attempt %d failed with %s; retrying...",
                        attempt,
                        err,
                    )
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                raise WaterUsageConnectionError(
                    f"TSM request failed after {MAX_RETRIES} attempts: {err}"
                ) from err

        raise WaterUsageError(
            f"Failed to retrieve usage data after {MAX_RETRIES} attempts"
        )

    async def async_get_hourly_usage_range(
        self,
        start_datetime: datetime,
        end_datetime: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Fetch hourly usage for each calendar day in the inclusive date range.

        Tyler Smart Meters only returns one day of hourly bars per POST. A
        90-day Energy dashboard backfill therefore requires one request per day.
        """
        tz = ZoneInfo(self.timezone)
        start = self._local_day_start(start_datetime, tz)
        end = self._local_day_start(
            end_datetime or datetime.now(tz), tz
        )
        if end < start:
            start, end = end, start

        merged: List[Dict[str, Any]] = []
        meter_status: Dict[str, Any] = {}
        day = start
        day_count = (end.date() - start.date()).days + 1
        day_index = 0

        while day <= end:
            day_index += 1
            if day_index == 1 or day_index % 10 == 0 or day_index == day_count:
                _LOGGER.info(
                    "Fetching hourly usage for %s (%d/%d)",
                    day.date().isoformat(),
                    day_index,
                    day_count,
                )

            day_data = await self.async_get_usage(
                aggregation=Aggregation.HOURLY,
                start_datetime=day,
                end_datetime=day,
            )
            merged.extend(day_data.get("USAGE", []))
            meter_status = {
                k: day_data[k]
                for k in (METER_NAME, METER_LAST_REPORTED_KEY, METER_REGISTER_READ_KEY)
                if k in day_data
            }

            day += timedelta(days=1)
            if day <= end and TSM_DAY_FETCH_DELAY > 0:
                await asyncio.sleep(TSM_DAY_FETCH_DELAY)

        merged.sort(key=lambda r: r["reading_time"])
        result: Dict[str, Any] = {"USAGE": merged}
        result.update(meter_status)
        if self._meter_name and METER_NAME not in result:
            result[METER_NAME] = self._meter_name
        return result

    @staticmethod
    def _local_day_start(dt: datetime, tz: ZoneInfo) -> datetime:
        """Normalize to midnight in the account timezone."""
        if dt.tzinfo is None:
            local = dt.replace(tzinfo=tz)
        else:
            local = dt.astimezone(tz)
        return local.replace(hour=0, minute=0, second=0, microsecond=0)

    def _build_tsm_form(
        self,
        aggregation: Aggregation,
        start_datetime: datetime,
        end_datetime: datetime,
    ) -> Dict[str, str]:
        """Build the full ``application/x-www-form-urlencoded`` body for TSM.

        The portal's chart endpoint is an ASP.NET MVC view that round-trips
        view-state via the POST body — sending only a token + dates isn't
        enough; the server renders the chart shell but skips the data
        injection unless it sees the expected billing/cycle context.

        We populate the body to match the captured working request:

        - JWT + meter/chart info (from the consumption page).
        - The selected ``interval_type`` and per-interval date_range fields.
        - ``view_type=Current``, plus ``date_ranges`` / ``default_series`` /
          ``monthly_bills`` derived from the bills JSON embedded in the page.
        - UI flags (``show_controls`` etc.) — harmless ``True`` defaults.
        - Prepaid/disconnect placeholders that the live request always sends.

        For HOURLY, only a single day is queried — TSM clamps the window
        regardless of what we send. ``start_datetime`` selects the day.
        For DAILY, the current billing cycle is used and the chart returns
        one point per day of that cycle.
        """
        if self._jwt is None:
            raise WaterUsageDataError(
                "JWT is not available; call async_get_chart_context first"
            )

        cycle_month, cycle_year, cycle_start, cycle_end = self._current_bill_cycle()
        date_ranges_json, default_series_json = self._build_cycle_payloads()
        previous_month, previous_year = _shift_month(cycle_month, cycle_year, -1)
        next_month, next_year = _shift_month(cycle_month, cycle_year, 1)

        if aggregation == Aggregation.HOURLY:
            target_day = start_datetime.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            start_date = _format_tsm_start_date(target_day)
            end_date = _format_tsm_end_date_hourly(target_day)
            hourly_date_range = (
                f"{_format_tsm_full_datetime(target_day, hour=0, minute=0)}|"
                f"{_format_tsm_full_datetime(target_day, hour=23, minute=0)}"
            )
            form_month = str(target_day.month)
            form_year = str(target_day.year)
        else:
            start_date = _format_tsm_start_date(cycle_start)
            end_date = _format_tsm_end_date_daily(cycle_end)
            hourly_date_range = (
                f"{_format_tsm_full_datetime(end_datetime, hour=0, minute=0)}|"
                f"{_format_tsm_full_datetime(end_datetime, hour=23, minute=0)}"
            )
            form_month = str(cycle_month)
            form_year = str(cycle_year)

        daily_date_range = (
            f"{_format_tsm_full_datetime(cycle_start, hour=0, minute=0)}|"
            f"{_format_tsm_full_datetime(cycle_end, hour=23, minute=59, second=59)}"
            f"|{cycle_month}|{cycle_year}"
        )

        body: Dict[str, str] = {
            "token": self._jwt,
            "interval_type": aggregation.interval_type,
            "start_date": start_date,
            "end_date": end_date,
            "month": form_month,
            "year": form_year,
            "period_type": "none",
            "amiData_Type": "Consumption",
            "netMetering_Type": "None",
            "option_toggle": "Usage",
            # Captured working request sends WeatherSelection=true; the server
            # uses this to decide whether to embed weather overlay data, but
            # series0.push lines still depend on it being present.
            "WeatherSelection": "true",
            "UsageCompMonthlySelected": "False",
            "optShow": "Temp",
            "use_forge": "true",
            "view_type": "Current",
            "original_interval_type": aggregation.interval_type,
            "original_start_date": _format_tsm_full_datetime(
                cycle_start, hour=0, minute=0
            ),
            "original_end_date": _format_tsm_full_datetime(
                cycle_end, hour=23, minute=59
            ),
            "previous_month": str(previous_month),
            "previous_year": str(previous_year),
            "next_month": str(next_month),
            "next_year": str(next_year),
            "monthly_date_range": "",
            "daily_date_range": daily_date_range,
            "custom_date_range": daily_date_range,
            "hourly_date_range": hourly_date_range,
            "show_controls": "True",
            "allow_switch_intervals": "True",
            "hide_consumption_table": "False",
            "show_manage_alerts": "True",
            "hide_billing_and_weekly_comparison": "False",
            "hide_date_control": "False",
            "show_avghighlow": "True",
            "allow_drilldown": "True",
            "ElaspsedTimeLastReadAPI": "",
            "account_disconnect_date": "1/1/0001 12:00:00 AM",
            "is_prepaid_account": "False",
            "prepaid_balance": "0.00",
            "prepaid_billed_yesterday": "0",
            "prepaid_average_cost": "0",
            "can_show_usage_comparison": "True",
            "date_ranges": date_ranges_json,
            "default_series": default_series_json,
            # ``monthly_bills`` is sent as a JSON-encoded string (i.e. the
            # whole JSON is itself wrapped in quotes and inner quotes
            # are backslash-escaped) — matching the captured wire format.
            "monthly_bills": json.dumps(self._monthly_bills_json or "[]"),
        }
        if self._meter_info_json:
            body["tsm_smartmeter_info"] = self._meter_info_json
        if self._chart_info_json:
            body["tsm_chart_info"] = self._chart_info_json
        if self._account_start_date:
            body["account_start_date"] = self._account_start_date
        return body

    def _current_bill_cycle(self) -> Tuple[int, int, datetime, datetime]:
        """Derive the current (in-progress) bill cycle from ``monthly_bills``.

        The ``data-monthly-bills`` JSON on the consumption page only lists
        BILLED cycles. The current, in-progress cycle starts the day the
        most recent bill ended and is numbered as ``(most_recent_month + 1)``.
        Cycles are roughly 30-31 days long.
        """
        bills = self._parse_monthly_bills()
        if not bills:
            now = datetime.now()
            return now.month, now.year, now.replace(day=1), now

        most_recent = bills[0]
        try:
            cycle_start = datetime.fromisoformat(most_recent["EndDate"])
        except (KeyError, ValueError):
            now = datetime.now()
            return now.month, now.year, now.replace(day=1), now

        # Approximate one billing month ahead for TSM view-state. The portal
        # only needs a plausible in-progress window; it must include "today" so
        # per-day hourly requests for recent calendar days are accepted.
        cycle_end = cycle_start + timedelta(days=31)
        now = datetime.now()
        if cycle_end < now:
            cycle_end = now

        cycle_month, cycle_year = _shift_month(
            most_recent.get("Month", cycle_start.month),
            most_recent.get("Year", cycle_start.year),
            1,
        )
        return cycle_month, cycle_year, cycle_start, cycle_end

    def _parse_monthly_bills(self) -> List[Dict[str, Any]]:
        """Return ``data-monthly-bills`` as a Python list (defensive)."""
        if not self._monthly_bills_json:
            return []
        try:
            data = json.loads(self._monthly_bills_json)
        except (TypeError, ValueError):
            _LOGGER.debug("monthly_bills JSON could not be parsed")
            return []
        if not isinstance(data, list):
            return []
        return data

    def _build_cycle_payloads(self) -> Tuple[str, str]:
        """Compute ``date_ranges`` and ``default_series`` JSON strings.

        The captured working request shows the chart UI re-sends the entire
        cycle history (oldest → current) plus a comparison series spanning
        two billing years. We rebuild that here so the server believes it's
        getting a complete view-state round-trip.
        """
        bills = self._parse_monthly_bills()
        if not bills:
            return "[]", "[]"

        # ``data_ranges`` is just the cycles in chronological order with the
        # in-progress cycle appended (``NotBilledYet=true``).
        bills_sorted = sorted(
            bills,
            key=lambda b: (b.get("Year", 0), b.get("Month", 0)),
        )
        date_ranges: List[Dict[str, Any]] = []
        for bill in bills_sorted:
            start = bill.get("StartDate")
            end = bill.get("EndDate")
            if not start or not end:
                continue
            # Bump end to the .999 millisecond version used in the capture.
            end_inclusive = end
            if end.endswith("T00:00:00"):
                end_inclusive = end.replace("T00:00:00", "T23:59:59.999")
            date_ranges.append(
                {
                    "Month": bill.get("Month"),
                    "Year": bill.get("Year"),
                    "StartDate": start,
                    "EndDate": end_inclusive,
                    "NotBilledYet": False,
                }
            )

        # Append the in-progress cycle.
        cycle_month, cycle_year, cycle_start, cycle_end = self._current_bill_cycle()
        date_ranges.append(
            {
                "Month": cycle_month,
                "Year": cycle_year,
                "StartDate": cycle_start.strftime("%Y-%m-%dT00:00:00"),
                "EndDate": cycle_end.strftime("%Y-%m-%dT23:59:59.999"),
                "NotBilledYet": True,
            }
        )

        # ``default_series`` is the same cycle list but with ``Consumption``
        # values and a ``SeriesName`` that groups them into yearly series.
        default_series: List[Dict[str, Any]] = []
        for bill in bills_sorted:
            consumption = float(bill.get("Amount", 0) or 0)
            month = bill.get("Month", 1)
            year = bill.get("Year", cycle_year)
            # Bills run on a fiscal year starting in July at this utility;
            # group accordingly.
            fiscal_start_year = year if month >= 7 else year - 1
            series_name = (
                f"Jul {fiscal_start_year} to Jun {fiscal_start_year + 1}"
            )
            default_series.append(
                {
                    "StartDate": bill.get("StartDate"),
                    "EndDate": bill.get("EndDate"),
                    "StartRead": 0.0,
                    "EndRead": 0.0,
                    "SeriesName": series_name,
                    "Consumption": consumption,
                    "TotalServiceCharge": bill.get("TotalServiceCharge", 0.0),
                    "Demand": 0.0,
                    "Usage": 0.0,
                    "DemandCharge": 0.0,
                    "Month": month,
                    "Year": year,
                    "NoMonthlyBill": False,
                    "TimeOfUsePeriods": [],
                    "NetUsage": consumption,
                    "ReceivedUsage": 0.0,
                    "BilledUsage": 0.0,
                    "Carryover": 0.0,
                }
            )

        return json.dumps(date_ranges), json.dumps(default_series)

    def _parse_tsm_html(self, html_body: str) -> Dict[str, Any]:
        """Extract every ``series0.push([ts, value])`` line from the response.

        Values are HGAL (hundreds of gallons) per the portal's own scaling
        notice; we convert to gallons here so all downstream code stays in
        the integration's declared unit.

        Hours listed in ``missingReads`` are skipped — TSM injects a
        placeholder bar (usually 0.1 HGAL) for intervals whose data has not
        arrived yet.
        """
        tz = ZoneInfo(self.timezone)
        missing_indices = {
            int(match.group(1))
            for match in _MISSING_READS_PUSH_RE.finditer(html_body)
        }
        readings: List[Dict[str, Any]] = []

        for point_index, match in enumerate(_SERIES_PUSH_RE.finditer(html_body)):
            if point_index in missing_indices:
                continue

            ts_str = match.group(1)
            value_str = match.group(2)
            try:
                naive = datetime.strptime(ts_str, "%m/%d/%Y %H:%M:%S")
            except ValueError:
                _LOGGER.debug("Skipping unparseable TSM timestamp: %s", ts_str)
                continue
            aware = naive.replace(tzinfo=tz)
            try:
                consumption = max(0.0, float(value_str)) * HGAL_TO_GALLONS
            except ValueError:
                continue

            readings.append(
                {
                    "reading_time": aware,
                    "consumption": consumption,
                    "raw_timestamp": int(
                        aware.astimezone(timezone.utc).timestamp() * 1000
                    ),
                }
            )

        # Sort defensively — TSM normally returns chronological order but
        # downstream statistics importing relies on it.
        readings.sort(key=lambda r: r["reading_time"])

        meter_status = self._parse_meter_status_from_tsm(html_body, tz)

        result: Dict[str, Any] = {
            "USAGE": readings,
            METER_NAME: self._meter_name,
        }
        result.update(meter_status)
        return result

    def _parse_meter_status_from_tsm(
        self, html_body: str, tz: ZoneInfo
    ) -> Dict[str, Any]:
        """Extract meter telemetry sidebar fields from a TSM HTML response."""
        status: Dict[str, Any] = {}

        reported_match = _METER_LAST_REPORTED_RE.search(html_body)
        if reported_match:
            reported_text = reported_match.group(1).strip()
            try:
                naive = datetime.strptime(
                    reported_text, "%m/%d/%Y - %I:%M %p"
                )
                status[METER_LAST_REPORTED_KEY] = naive.replace(tzinfo=tz)
            except ValueError:
                _LOGGER.debug(
                    "Could not parse meter last reported time: %s",
                    reported_text,
                )

        read_match = _METER_REGISTER_READ_ARIA_RE.search(html_body)
        if not read_match:
            read_match = _METER_REGISTER_READ_TEXT_RE.search(html_body)
        if read_match:
            read_text = read_match.group(1).replace(",", "")
            try:
                status[METER_REGISTER_READ_KEY] = float(read_text)
            except ValueError:
                _LOGGER.debug(
                    "Could not parse meter register read: %s", read_text
                )

        return status


def _format_tsm_start_date(dt: datetime) -> str:
    """Return the Tyler Smart Meters ``start_date`` value.

    Captured traffic uses ``M/D/YYYY`` (no leading zeros, no time component).
    """
    return f"{dt.month}/{dt.day}/{dt.year}"


def _format_tsm_end_date_hourly(dt: datetime) -> str:
    """Return ``end_date`` for hourly mode: same day as ``dt`` at 11:00 PM.

    Captured wire format: ``5/14/2026 11:00pm``.
    """
    return f"{dt.month}/{dt.day}/{dt.year} 11:00pm"


def _format_tsm_end_date_daily(dt: datetime) -> str:
    """Return ``end_date`` for daily mode: last day of cycle at 11:00 PM."""
    return f"{dt.month}/{dt.day}/{dt.year} 11:00pm"


# Backwards-compat alias used by tests that expect the historical name.
_format_tsm_end_date = _format_tsm_end_date_hourly


def _format_tsm_full_datetime(
    dt: datetime,
    *,
    hour: Optional[int] = None,
    minute: Optional[int] = None,
    second: int = 0,
) -> str:
    """Return ``M/D/YYYY h:mm:ss AM/PM`` matching the captured cycle bounds.

    Hour 0 renders as ``12:00:00 AM``; hour 23 renders as ``11:00:00 PM``.
    Optional overrides let callers pin the time to a chosen wall-clock value
    (e.g. cycle start at 00:00, cycle end at 23:59).
    """
    h = hour if hour is not None else dt.hour
    m = minute if minute is not None else dt.minute
    s = second
    am_pm = "AM" if h < 12 else "PM"
    display_hour = h % 12 or 12
    return (
        f"{dt.month}/{dt.day}/{dt.year} "
        f"{display_hour}:{m:02d}:{s:02d} {am_pm}"
    )


def _shift_month(month: int, year: int, delta: int) -> Tuple[int, int]:
    """Return ``(month, year)`` shifted by ``delta`` calendar months."""
    total = (year * 12 + (month - 1)) + delta
    new_year, new_month_index = divmod(total, 12)
    return new_month_index + 1, new_year
