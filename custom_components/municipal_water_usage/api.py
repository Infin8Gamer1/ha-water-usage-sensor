"""Municipal Water Usage API client for Home Assistant integration.

This module talks to a municipalonlinepayments.com utility portal
(default: ``bastroptx.municipalonlinepayments.com``) and returns
hourly/daily/monthly water consumption readings in U.S. gallons.

The login endpoint, CSRF token field, and usage-data endpoint are
SCAFFOLDED here behind ``TODO`` markers. They will be wired up after
inspecting the portal's network traffic. The retry / session refresh /
parsing infrastructure mirrors the proven SmartHub implementation so the
rest of the integration can be wired against it today.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import ClientError, ClientTimeout

from .const import (
    DEFAULT_TIMEOUT,
    MAX_RETRIES,
    METER_NAME,
    RETRY_DELAY,
    SESSION_TIMEOUT,
)
from .exceptions import (
    WaterUsageAuthenticationError,
    WaterUsageConnectionError,
    WaterUsageDataError,
    WaterUsageError,
)
from .utils import parse_epoch_set_timezone, sanitize_host

_LOGGER = logging.getLogger(__name__)


class Aggregation(StrEnum):
    """Supported usage aggregation buckets."""

    HOURLY = "HOURLY"
    DAILY = "DAILY"
    MONTHLY = "MONTHLY"

    @property
    def label(self) -> str:
        """Return human readable label."""
        if self == Aggregation.HOURLY:
            return "Hourly"
        if self == Aggregation.DAILY:
            return "Daily"
        if self == Aggregation.MONTHLY:
            return "Monthly"
        return "Unknown"

    @property
    def suffix(self) -> str:
        """Return statistic ID suffix."""
        if self == Aggregation.HOURLY:
            return ""
        if self == Aggregation.DAILY:
            return "_daily"
        if self == Aggregation.MONTHLY:
            return "_monthly"
        return "_unknown"

    @property
    def period(self) -> str:
        """Return statistic period."""
        if self == Aggregation.HOURLY:
            return "hour"
        if self == Aggregation.DAILY:
            return "day"
        if self == Aggregation.MONTHLY:
            return "month"
        return "unknown"


class MunicipalWaterAPI:
    """Client for a municipalonlinepayments.com water-usage portal."""

    def __init__(
        self,
        email: str,
        password: str,
        account_id: str,
        host: str,
        timezone: str,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        """Initialize the API client."""
        self.email = email
        self.password = password
        self.account_id = account_id
        self.host = sanitize_host(host)
        self.timezone = timezone
        self.timeout = timeout

        self._session: Optional[aiohttp.ClientSession] = None
        self._session_created_at: Optional[datetime] = None
        self._authenticated: bool = False

    # ------------------------------------------------------------------ #
    # Session management
    # ------------------------------------------------------------------ #

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session with a cookie jar."""
        now = datetime.now()

        if (
            self._session_created_at
            and (now - self._session_created_at).total_seconds() > SESSION_TIMEOUT
        ):
            _LOGGER.debug("Session timeout reached, refreshing session")
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None
            self._session_created_at = None
            self._authenticated = False

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=ClientTimeout(total=self.timeout),
                connector=aiohttp.TCPConnector(ssl=True, limit=10),
                cookie_jar=aiohttp.CookieJar(),
            )
            self._session_created_at = now
            self._authenticated = False
            _LOGGER.debug("Created new aiohttp session")

        return self._session

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        self._session_created_at = None
        self._authenticated = False

    async def _refresh_authentication(self) -> None:
        """Clear the session and re-login."""
        _LOGGER.debug("Refreshing authentication and session")

        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        self._session_created_at = None
        self._authenticated = False

        await self.async_login()

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #

    async def async_login(self) -> None:
        """Authenticate to the portal.

        Most municipalonlinepayments.com portals use a classic ASP.NET-style
        login form: a GET on the login page returns hidden ``__RequestVerificationToken``
        / ``__VIEWSTATE`` fields, and the POST sends them back along with the
        credentials. The aiohttp ``CookieJar`` will then carry the
        authenticated session cookie for subsequent requests.

        TODO: replace the URL paths and form-field names below with the
        actual values captured from DevTools.
        """
        session = await self._get_session()

        login_page_url = f"https://{self.host}/Login"  # TODO: confirm path
        login_submit_url = f"https://{self.host}/Login"  # TODO: confirm path

        headers = {
            "User-Agent": "HomeAssistant Municipal Water Usage Integration",
        }

        try:
            # Step 1: fetch the login page to get any CSRF / anti-forgery token.
            async with session.get(login_page_url, headers=headers) as response:
                if response.status != 200:
                    raise WaterUsageConnectionError(
                        f"Could not load login page (HTTP {response.status})"
                    )
                _ = await response.text()
                # TODO: extract __RequestVerificationToken (or equivalent)
                # from the HTML and include it in the payload below.
                csrf_token: Optional[str] = None

            # Step 2: POST credentials.
            payload: Dict[str, str] = {
                "Email": self.email,        # TODO: confirm field name
                "Password": self.password,  # TODO: confirm field name
            }
            if csrf_token:
                payload["__RequestVerificationToken"] = csrf_token

            async with session.post(
                login_submit_url,
                headers=headers,
                data=payload,
                allow_redirects=True,
            ) as response:
                body = await response.text()
                _LOGGER.debug("Login response status: %s", response.status)

                if response.status == 401:
                    raise WaterUsageAuthenticationError("Invalid credentials")
                if response.status >= 400:
                    raise WaterUsageConnectionError(
                        f"Login failed with HTTP status: {response.status}"
                    )

                # TODO: confirm a more specific success signal. Many portals
                # redirect to "/Account" or set a known cookie on success and
                # re-render the login form (still HTTP 200) on failure.
                if "Invalid" in body or "incorrect" in body.lower():
                    raise WaterUsageAuthenticationError(
                        "Login form returned an error"
                    )

                self._authenticated = True
                _LOGGER.debug("Successfully authenticated to %s", self.host)

        except ClientError as err:
            raise WaterUsageConnectionError(
                f"Connection error during authentication: {err}"
            ) from err

    # ------------------------------------------------------------------ #
    # Usage data
    # ------------------------------------------------------------------ #

    def parse_usage_series(self, usage_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Normalize a raw usage series into the shape sensor.py expects.

        Output items have the shape::

            {
                "reading_time": <tz-aware datetime>,
                "consumption": <float gallons>,
                "raw_timestamp": <int epoch ms>,
            }

        Sub-hour readings are folded into the previous top-of-hour bucket so
        the importer always sees hour-aligned timestamps, which is what
        Home Assistant's statistics engine requires.
        """
        parsed_data: List[Dict[str, Any]] = []
        _LOGGER.debug("First 10 entries of usage data: %s", usage_data[:10])

        for usage in usage_data:
            raw_epoch_ms = usage.get("x")
            if raw_epoch_ms is None:
                continue

            event_time = parse_epoch_set_timezone(
                raw_epoch_ms / 1000.0, ZoneInfo(self.timezone)
            )

            # If the first reading isn't aligned to top-of-hour, prepend a
            # zero entry at the top of the hour so downstream consumers stay
            # aligned with Home Assistant's hourly buckets.
            if event_time.minute != 0 and not parsed_data:
                _LOGGER.warning(
                    "Initial usage data is not aligned with top of the hour, "
                    "inserting a 0 entry at 0 minutes: event_time:%s, %s",
                    event_time,
                    raw_epoch_ms,
                )
                zero_time = event_time.replace(minute=0)
                parsed_data.append(
                    {
                        "reading_time": zero_time,
                        "consumption": 0.0,
                        "raw_timestamp": int(
                            zero_time.replace(tzinfo=timezone.utc).timestamp() * 1000
                        ),
                    }
                )

            if (
                parsed_data
                and parsed_data[-1]["reading_time"].hour != event_time.hour
                and event_time.minute != 0
            ):
                _LOGGER.warning(
                    "Usage data is not aligned with top of the hour, "
                    "inserting a 0 entry at 0 minutes: event_time:%s, %s",
                    event_time,
                    raw_epoch_ms,
                )
                zero_time = event_time.replace(minute=0)
                parsed_data.append(
                    {
                        "reading_time": zero_time,
                        "consumption": 0.0,
                        "raw_timestamp": int(
                            zero_time.replace(tzinfo=timezone.utc).timestamp() * 1000
                        ),
                    }
                )

            consumption = max(0.0, float(usage.get("y", 0.0)))

            if event_time.minute != 0 and parsed_data:
                _LOGGER.debug(
                    "Consolidating sub-hour reading: %s, %s + %s",
                    event_time,
                    parsed_data[-1]["consumption"],
                    consumption,
                )
                parsed_data[-1]["consumption"] += consumption
                continue

            parsed_data.append(
                {
                    "reading_time": event_time,
                    "consumption": consumption,
                    "raw_timestamp": int(raw_epoch_ms),
                }
            )

        return parsed_data

    def parse_usage(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Parse a raw portal response into ``{"USAGE": [...], METER_NAME: str}``.

        The municipalonlinepayments portal's response shape is not yet
        confirmed. This default implementation looks for a top-level
        ``data`` array of ``{x, y}`` points (the most common shape used by
        Highcharts-driven utility dashboards). Adjust once the real response
        is known.
        """
        if not isinstance(data, dict):
            raise WaterUsageDataError("Invalid data format: expected dictionary")

        try:
            # TODO: confirm the actual JSON layout returned by the portal.
            series = data.get("data") or data.get("series") or []
            if isinstance(series, dict):
                series = series.get("data", [])

            usage_points = self.parse_usage_series(series)
            meter_name = data.get("meterName") or data.get("meter") or "water_meter"

            return {
                "USAGE": usage_points,
                METER_NAME: meter_name,
            }
        except WaterUsageDataError:
            raise
        except Exception as err:
            _LOGGER.error("Error parsing usage data: %s", data)
            raise WaterUsageDataError(f"Error parsing usage data: {err}") from err

    async def async_get_usage(
        self,
        aggregation: Aggregation,
        start_datetime: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Retrieve water usage data for the given aggregation.

        Returns a dict shaped like ``{"USAGE": [...], "meter_name": str}``,
        matching what the coordinator's statistics importer expects.

        TODO: wire up the real portal endpoint, query parameters, and
        response handling. The retry / 401-refresh / session-refresh
        scaffolding below is ready to use as-is.
        """
        usage_url = f"https://{self.host}/Usage"  # TODO: confirm path

        now = datetime.now()
        end_datetime = now.replace(minute=0, second=0, microsecond=0)
        if start_datetime is None:
            start_datetime = end_datetime - timedelta(days=30)

        start_timestamp_ms = int(start_datetime.timestamp() * 1000)
        end_timestamp_ms = int(end_datetime.timestamp() * 1000)

        params: Dict[str, str] = {
            # TODO: confirm parameter names with the live portal.
            "accountNumber": self.account_id,
            "aggregation": aggregation.value,
            "startDateTime": str(start_timestamp_ms),
            "endDateTime": str(end_timestamp_ms),
        }

        headers = {
            "Accept": "application/json",
            "User-Agent": "HomeAssistant Municipal Water Usage Integration",
        }

        _LOGGER.debug("Requesting %s water usage from: %s", aggregation.label, usage_url)

        reauth_attempted = False

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if not self._authenticated:
                    await self._refresh_authentication()
                    reauth_attempted = True

                session = await self._get_session()
                async with session.get(
                    usage_url, headers=headers, params=params
                ) as response:
                    _LOGGER.debug(
                        "Attempt %d: usage response status: %s", attempt, response.status
                    )

                    if response.status == 401 or response.status == 403:
                        if not reauth_attempted:
                            _LOGGER.info(
                                "Session expired (HTTP %s), refreshing authentication...",
                                response.status,
                            )
                            await self._refresh_authentication()
                            reauth_attempted = True
                            continue
                        raise WaterUsageAuthenticationError(
                            "Authentication failed after re-login"
                        )

                    if response.status != 200:
                        error_text = await response.text()
                        _LOGGER.warning(
                            "HTTP error %d fetching usage: %s",
                            response.status,
                            error_text,
                        )
                        raise WaterUsageConnectionError(
                            f"HTTP error {response.status}: {error_text}"
                        )

                    try:
                        response_json = await response.json(content_type=None)
                    except Exception as err:
                        raise WaterUsageDataError(
                            f"Invalid JSON response: {err}"
                        ) from err

                    return self.parse_usage(response_json)

            except WaterUsageAuthenticationError:
                raise
            except ClientError as err:
                if attempt < MAX_RETRIES:
                    _LOGGER.warning(
                        "Attempt %d failed with connection error: %s, retrying...",
                        attempt,
                        err,
                    )
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                raise WaterUsageConnectionError(
                    f"Connection failed after {MAX_RETRIES} attempts: {err}"
                ) from err

        raise WaterUsageError(
            f"Failed to retrieve usage data after {MAX_RETRIES} attempts"
        )
