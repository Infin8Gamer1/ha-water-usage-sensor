"""Tests for the MunicipalWaterAPI client."""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.municipal_water_usage.api import (
    Aggregation,
    MunicipalWaterAPI,
    _format_tsm_end_date,
    _format_tsm_start_date,
)
from custom_components.municipal_water_usage.const import (
    HGAL_TO_GALLONS,
    METER_LAST_REPORTED_KEY,
    METER_REGISTER_READ_KEY,
)
from custom_components.municipal_water_usage.exceptions import (
    WaterUsageAuthenticationError,
    WaterUsageDataError,
)


# --------------------------------------------------------------------------- #
# Test fixtures (anonymized but structurally identical to captured traffic)
# --------------------------------------------------------------------------- #


def _make_jwt(exp_ts: int) -> str:
    """Build a minimal JWT-shaped string whose payload contains ``exp``."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"account": "14-6402-01", "exp": exp_ts}).encode()
    ).rstrip(b"=")
    signature = base64.urlsafe_b64encode(b"signature").rstrip(b"=")
    return b".".join([header, payload, signature]).decode()


LOGIN_FORM_HTML = """
<html><body>
<form method="post" action="/Account/Login">
    <input type="hidden" name="ReturnUrl" value="/connect/authorize/callback?client_id=bastroptx" />
    <input type="hidden" name="__RequestVerificationToken" value="CSRF-TOKEN-VALUE" />
    <input name="Email" />
    <input name="Password" />
    <button name="button" value="login">Sign in</button>
</form>
</body></html>
"""

LOGIN_BAD_CREDS_HTML = """
<html><body>
<form method="post" action="/Account/Login">
    <input type="hidden" name="ReturnUrl" value="/foo" />
    <input type="hidden" name="__RequestVerificationToken" value="CSRF-TOKEN-VALUE" />
    <input name="Email" />
    <input name="Password" />
</form>
<div>Invalid credentials</div>
</body></html>
"""

# Real OIDC form_post pages from this portal use SINGLE quotes on attrs.
OIDC_CALLBACK_HTML = """
<html><head><meta http-equiv='X-UA-Compatible' content='IE=edge' /></head>
<body onload='document.forms[0].submit()'>
<form method='post' action='https://bastroptx.municipalonlinepayments.com/signin-oidc'>
    <input type='hidden' name='code' value='AUTH-CODE-123' />
    <input type='hidden' name='id_token' value='ID-TOKEN-456' />
    <input type='hidden' name='scope' value='openid profile' />
    <input type='hidden' name='state' value='STATE-789' />
    <input type='hidden' name='session_state' value='SESSION-STATE' />
</form>
</body></html>
"""


def _build_consumption_html(jwt: str) -> str:
    """Build a minimal consumption page that mirrors the real structure."""
    return f"""
    <html><body>
    <script src="https://www.tylersmartmeters.com/charts.js"
            data-token="{jwt}"
            data-start-date="1/1/0001 12:00:00 AM"
            data-end-date="1/1/0001 12:00:00 AM"
            data-interval-type="D"
            data-tsm-chart-info="{{&quot;CostPerConsumptionUnit&quot;:0.79,&quot;BillingFrequency&quot;:&quot;M&quot;}}"
            data-tsm-smartmeter-info="{{&quot;SmartMeterInfo&quot;:[{{&quot;MIUID&quot;:&quot;131356596&quot;,&quot;ServiceType&quot;:&quot;W&quot;}}]}}"
            data-monthly-bills="[]"
            data-account-start-date="6/14/2024 12:00:00 AM">
    </script>
    </body></html>
    """


TSM_METER_STATUS_HTML = """
<forge-label-value>
    <span slot="label">Meter last reported</span>
    <span slot="value" aria-label="May 16th 2026 - 12:00 AM">
        05/16/2026 - 12:00 AM <br />
    </span>
    <span slot="value" aria-label="Read: 163776.70">
        Read: 163776.70
    </span>
</forge-label-value>
"""

TSM_HOURLY_HTML = f"""
<html><body>
{TSM_METER_STATUS_HTML}
<script>
var series0 = [], reads0 = [], ticks = [];
series0.push(['05/14/2026 00:00:00', 0.84]);
reads0.push(['05/14/2026 00:00:00', [0, 0]]);
series0.push(['05/14/2026 01:00:00', 0.23]);
reads0.push(['05/14/2026 01:00:00', [0, 0]]);
series0.push(['05/14/2026 02:00:00', 0.00]);
</script>
</body></html>
"""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _ctx(status: int = 200, text: str = "", url: str = "https://example/") -> MagicMock:
    """Build an aiohttp context-manager response mock."""
    response = AsyncMock()
    response.status = status
    response.url = url
    response.text = AsyncMock(return_value=text)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_cookie_jar(*cookie_names: str) -> list:
    """Return an iterable of cookie-like objects the API client can inspect."""
    return [SimpleNamespace(key=name, value="x") for name in cookie_names]


def _make_api(**overrides) -> MunicipalWaterAPI:
    defaults = {
        "email": "user@example.com",
        "password": "hunter2",
        "account_id": "14-6402-01",
        "host": "bastroptx.municipalonlinepayments.com",
        "timezone": "America/Chicago",
    }
    defaults.update(overrides)
    return MunicipalWaterAPI(**defaults)


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_async_login_runs_full_oidc_flow():
    """async_login must POST credentials in body + manually submit the OIDC form."""
    api = _make_api()

    mock_session = MagicMock()
    mock_session.get = MagicMock(
        return_value=_ctx(
            200, LOGIN_FORM_HTML, "https://account.municipalonlinepayments.com/Account/Login?ReturnUrl=x"
        )
    )

    posts = [
        # First POST: submit credentials. Response is the OIDC callback HTML.
        _ctx(200, OIDC_CALLBACK_HTML, "https://account.municipalonlinepayments.com/cb"),
        # Second POST: signin-oidc with hidden form fields.
        _ctx(200, "<html>Welcome</html>", "https://bastroptx.municipalonlinepayments.com/"),
    ]
    mock_session.post = MagicMock(side_effect=posts)
    # Tenant session cookie must be present after the callback POST for
    # the login sanity check to pass.
    mock_session.cookie_jar = _mock_cookie_jar(
        ".AspNet.Cookies", "ASP.NET_SessionId"
    )

    with patch.object(MunicipalWaterAPI, "_get_session", return_value=mock_session):
        await api.async_login()

    assert api._authenticated is True

    # First post is the credential submission.
    cred_call = mock_session.post.call_args_list[0]
    payload = cred_call.kwargs["data"]
    assert payload["Email"] == "user@example.com"
    assert payload["Password"] == "hunter2"
    assert payload["__RequestVerificationToken"] == "CSRF-TOKEN-VALUE"
    assert payload["button"] == "login"

    # Second post is the form_post callback to signin-oidc.
    oidc_call = mock_session.post.call_args_list[1]
    assert oidc_call.args[0] == (
        "https://bastroptx.municipalonlinepayments.com/signin-oidc"
    )
    oidc_payload = oidc_call.kwargs["data"]
    assert oidc_payload["code"] == "AUTH-CODE-123"
    assert oidc_payload["id_token"] == "ID-TOKEN-456"
    assert oidc_payload["state"] == "STATE-789"


@pytest.mark.asyncio
async def test_async_login_detects_rejected_credentials():
    """If the IDP returns the login form again, treat it as a bad password."""
    api = _make_api(password="wrong")

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=_ctx(200, LOGIN_FORM_HTML))
    mock_session.post = MagicMock(return_value=_ctx(200, LOGIN_BAD_CREDS_HTML))
    mock_session.cookie_jar = _mock_cookie_jar()

    with patch.object(MunicipalWaterAPI, "_get_session", return_value=mock_session):
        with pytest.raises(WaterUsageAuthenticationError):
            await api.async_login()


@pytest.mark.asyncio
async def test_async_login_fails_when_tenant_cookie_is_missing():
    """If the OIDC form_post silently fails, raise an auth error instead of
    silently continuing — otherwise the very next request bounces back into
    the OIDC dance and we get a confusing chart-context error.
    """
    api = _make_api()

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=_ctx(200, LOGIN_FORM_HTML))
    posts = [
        _ctx(200, OIDC_CALLBACK_HTML, "https://account.municipalonlinepayments.com/cb"),
        # Imagine the callback POST returned 200 but didn't actually set
        # the tenant session cookie (e.g. wrong code).
        _ctx(200, "<html>oops</html>", "https://bastroptx.municipalonlinepayments.com/"),
    ]
    mock_session.post = MagicMock(side_effect=posts)
    mock_session.cookie_jar = _mock_cookie_jar(
        ".AspNetCore.Identity.Application"  # IDP only, no tenant cookie.
    )

    with patch.object(MunicipalWaterAPI, "_get_session", return_value=mock_session):
        with pytest.raises(WaterUsageAuthenticationError, match="AspNet.Cookies"):
            await api.async_login()


@pytest.mark.asyncio
async def test_async_login_parses_single_quoted_oidc_form_post():
    """Regression: real OIDC pages from this portal use single-quoted attrs."""
    api = _make_api()

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=_ctx(200, LOGIN_FORM_HTML))
    posts = [
        _ctx(200, OIDC_CALLBACK_HTML),
        _ctx(200, "<html>ok</html>"),
    ]
    mock_session.post = MagicMock(side_effect=posts)
    mock_session.cookie_jar = _mock_cookie_jar(".AspNet.Cookies")

    with patch.object(MunicipalWaterAPI, "_get_session", return_value=mock_session):
        await api.async_login()

    oidc_call = mock_session.post.call_args_list[1]
    assert oidc_call.args[0] == (
        "https://bastroptx.municipalonlinepayments.com/signin-oidc"
    )
    payload = oidc_call.kwargs["data"]
    assert payload["code"] == "AUTH-CODE-123"
    assert payload["id_token"] == "ID-TOKEN-456"
    assert payload["state"] == "STATE-789"
    assert payload["scope"] == "openid profile"
    assert payload["session_state"] == "SESSION-STATE"


@pytest.mark.asyncio
async def test_async_login_raises_when_login_form_is_missing_csrf():
    """Defensive: a missing CSRF token must produce a data error, not a crash."""
    api = _make_api()

    bad_html = "<html><body>No form here</body></html>"
    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=_ctx(200, bad_html))
    mock_session.post = MagicMock(return_value=_ctx(200, ""))
    mock_session.cookie_jar = _mock_cookie_jar()

    with patch.object(MunicipalWaterAPI, "_get_session", return_value=mock_session):
        with pytest.raises(WaterUsageDataError):
            await api.async_login()


# --------------------------------------------------------------------------- #
# Chart-context extraction
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_async_get_chart_context_extracts_jwt_and_meter():
    """Page scraping must populate jwt + meter info from data-* attributes."""
    api = _make_api()
    api._authenticated = True

    jwt = _make_jwt(int(time.time()) + 1800)
    html = _build_consumption_html(jwt)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=_ctx(200, html))
    mock_session.cookie_jar = _mock_cookie_jar(".AspNet.Cookies")

    with patch.object(MunicipalWaterAPI, "_get_session", return_value=mock_session):
        await api.async_get_chart_context()

    assert api._jwt == jwt
    assert api._jwt_exp is not None and api._jwt_exp > time.time()
    assert "131356596" in (api._meter_info_json or "")
    assert "CostPerConsumptionUnit" in (api._chart_info_json or "")
    assert api._account_start_date == "6/14/2024 12:00:00 AM"
    assert api._meter_name == "MIU 131356596"


@pytest.mark.asyncio
async def test_async_get_chart_context_raises_when_token_missing():
    """A consumption page with no charts.js script means the account is bad."""
    api = _make_api()
    api._authenticated = True

    mock_session = MagicMock()
    mock_session.get = MagicMock(
        return_value=_ctx(200, "<html><body>No charts here</body></html>")
    )
    mock_session.cookie_jar = _mock_cookie_jar(".AspNet.Cookies")

    with patch.object(MunicipalWaterAPI, "_get_session", return_value=mock_session):
        with pytest.raises(WaterUsageDataError):
            await api.async_get_chart_context()


# --------------------------------------------------------------------------- #
# TSM POST + HTML parsing
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_async_get_usage_sends_minimal_body_and_parses_series():
    """End-to-end: build form, POST to TSM, parse series0.push lines."""
    api = _make_api()
    api._authenticated = True
    api._jwt = _make_jwt(int(time.time()) + 1800)
    api._jwt_exp = int(time.time()) + 1800
    api._meter_info_json = (
        '{"SmartMeterInfo":[{"MIUID":"131356596","ServiceType":"W"}]}'
    )
    api._chart_info_json = '{"BillingFrequency":"M"}'
    api._account_start_date = "6/14/2024 12:00:00 AM"
    api._meter_name = "MIU 131356596"
    # Minimal monthly_bills so _build_tsm_form can derive a current cycle.
    api._monthly_bills_json = json.dumps(
        [
            {
                "Month": 5,
                "Year": 2026,
                "Amount": 77,
                "StartDate": "2026-03-29T00:00:00",
                "EndDate": "2026-04-29T00:00:00",
                "TotalServiceCharge": 61.04,
            }
        ]
    )

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=_ctx(200, TSM_HOURLY_HTML))

    with patch.object(MunicipalWaterAPI, "_get_session", return_value=mock_session):
        result = await api.async_get_usage(
            aggregation=Aggregation.HOURLY,
            start_datetime=datetime(2026, 5, 14, 0, 0, 0),
            end_datetime=datetime(2026, 5, 14, 23, 0, 0),
        )

    # Body assertions.
    post_call = mock_session.post.call_args
    assert post_call.args[0] == "https://www.tylersmartmeters.com/"
    body = post_call.kwargs["data"]
    assert body["token"] == api._jwt
    assert body["interval_type"] == "H"
    assert body["start_date"] == "5/14/2026"
    assert body["end_date"] == "5/14/2026 11:00pm"
    assert body["amiData_Type"] == "Consumption"
    assert body["netMetering_Type"] == "None"
    # WeatherSelection must mirror the captured working request — when set
    # to ``false`` the server returns an empty chart shell.
    assert body["WeatherSelection"] == "true"
    assert body["view_type"] == "Current"
    assert "tsm_smartmeter_info" in body
    assert "131356596" in body["tsm_smartmeter_info"]
    # Cycle context must be present, derived from monthly_bills.
    assert body["month"] == "5"  # hourly drill-down → calendar month of target day
    assert body["year"] == "2026"
    assert body["hourly_date_range"].startswith("5/14/2026 12:00:00 AM|")
    assert "5/14/2026 11:00:00 PM" in body["hourly_date_range"]
    assert body["daily_date_range"].endswith("|6|2026")  # next cycle = 6/2026
    assert body["original_interval_type"] == "H"
    assert body["date_ranges"] != "[]"
    assert body["default_series"] != "[]"

    # Headers should mark this as a TSM origin to pass server-side checks.
    headers = post_call.kwargs["headers"]
    assert headers["Origin"] == "https://www.tylersmartmeters.com"
    assert headers["Referer"] == "https://www.tylersmartmeters.com/"

    # Parsing assertions: HGAL → gallons (x100), in order.
    usage = result["USAGE"]
    assert len(usage) == 3
    assert usage[0]["consumption"] == pytest.approx(0.84 * HGAL_TO_GALLONS)
    assert usage[1]["consumption"] == pytest.approx(0.23 * HGAL_TO_GALLONS)
    assert usage[2]["consumption"] == 0.0
    assert result["meter_name"] == "MIU 131356596"
    reported = result[METER_LAST_REPORTED_KEY]
    assert reported.year == 2026
    assert reported.month == 5
    assert reported.day == 16
    assert reported.hour == 0
    assert result[METER_REGISTER_READ_KEY] == pytest.approx(163776.70)


def test_parse_tsm_html_extracts_meter_status():
    """Billing sidebar fields are parsed from TSM HTML."""
    api = _make_api()
    api._meter_name = "MIU 131356596"

    result = api._parse_tsm_html(TSM_HOURLY_HTML)

    reported = result[METER_LAST_REPORTED_KEY]
    assert reported.tzinfo is not None
    assert reported.strftime("%m/%d/%Y %I:%M %p") == "05/16/2026 12:00 AM"
    assert result[METER_REGISTER_READ_KEY] == pytest.approx(163776.70)


@pytest.mark.asyncio
async def test_async_get_hourly_usage_range_merges_days():
    """Each day in the range triggers a separate hourly TSM request."""
    api = _make_api()
    api._authenticated = True
    api._jwt = _make_jwt(int(time.time()) + 1800)
    api._jwt_exp = int(time.time()) + 1800
    api._meter_name = "MIU 1"
    api._monthly_bills_json = json.dumps(
        [
            {
                "Month": 5,
                "Year": 2026,
                "Amount": 1,
                "StartDate": "2026-03-29T00:00:00",
                "EndDate": "2026-04-29T00:00:00",
                "TotalServiceCharge": 1.0,
            }
        ]
    )

    call_days: list[str] = []

    async def fake_get_usage(aggregation, start_datetime, end_datetime=None):
        call_days.append(start_datetime.strftime("%Y-%m-%d"))
        return api._parse_tsm_html(TSM_HOURLY_HTML)

    with patch.object(api, "async_get_usage", side_effect=fake_get_usage):
        tz = ZoneInfo("America/Chicago")
        start = datetime(2026, 5, 14, tzinfo=tz)
        end = datetime(2026, 5, 16, tzinfo=tz)
        result = await api.async_get_hourly_usage_range(start, end)

    assert call_days == ["2026-05-14", "2026-05-15", "2026-05-16"]
    assert len(result["USAGE"]) == 3 * 3  # 3 readings per day × 3 days


def test_parse_tsm_html_meter_status_optional():
    """Usage parsing still works when the billing sidebar is absent."""
    api = _make_api()
    api._meter_name = "MIU 1"

    html = """
    <script>
    series0.push(['05/14/2026 00:00:00', 1.0]);
    </script>
    """
    result = api._parse_tsm_html(html)

    assert len(result["USAGE"]) == 1
    assert METER_LAST_REPORTED_KEY not in result
    assert METER_REGISTER_READ_KEY not in result


@pytest.mark.asyncio
async def test_async_get_usage_refreshes_context_on_401():
    """A 401 from TSM should trigger one chart-context refresh and a retry."""
    api = _make_api()
    api._authenticated = True
    api._jwt = _make_jwt(int(time.time()) + 1800)
    api._jwt_exp = int(time.time()) + 1800
    api._meter_info_json = '{"SmartMeterInfo":[]}'

    posts = [
        _ctx(401, "Unauthorized"),
        _ctx(200, TSM_HOURLY_HTML),
    ]
    mock_session = MagicMock()
    mock_session.post = MagicMock(side_effect=posts)

    new_jwt = _make_jwt(int(time.time()) + 3600)

    async def fake_refresh(*_args, **_kwargs) -> None:
        api._jwt = new_jwt
        api._jwt_exp = int(time.time()) + 3600

    with patch.object(
        MunicipalWaterAPI, "_get_session", return_value=mock_session
    ), patch.object(
        MunicipalWaterAPI,
        "async_get_chart_context",
        new=AsyncMock(side_effect=fake_refresh),
    ) as mock_ctx:
        result = await api.async_get_usage(
            aggregation=Aggregation.DAILY,
            start_datetime=datetime(2026, 5, 1),
            end_datetime=datetime(2026, 5, 14, 23, 0, 0),
        )

    assert mock_ctx.await_count == 1  # one refresh
    assert mock_session.post.call_count == 2
    assert result["USAGE"]


@pytest.mark.asyncio
async def test_parse_tsm_html_handles_empty_response():
    """A response with no series0.push lines yields an empty USAGE list."""
    api = _make_api()
    api._meter_name = "MIU 1"

    result = api._parse_tsm_html("<html><body>no data</body></html>")

    assert result["USAGE"] == []
    assert result["meter_name"] == "MIU 1"


# --------------------------------------------------------------------------- #
# Helper formatters
# --------------------------------------------------------------------------- #


def test_format_tsm_start_date():
    assert _format_tsm_start_date(datetime(2026, 5, 1)) == "5/1/2026"
    assert _format_tsm_start_date(datetime(2026, 12, 31)) == "12/31/2026"


def test_format_tsm_end_date():
    # Hourly mode always queries midnight → 11 PM regardless of the
    # ``end_datetime`` passed in, mirroring the captured wire format.
    assert (
        _format_tsm_end_date(datetime(2026, 5, 14, 23, 0, 0))
        == "5/14/2026 11:00pm"
    )
    assert (
        _format_tsm_end_date(datetime(2026, 5, 14, 0, 0, 0))
        == "5/14/2026 11:00pm"
    )
    assert (
        _format_tsm_end_date(datetime(2026, 12, 31, 9, 0, 0))
        == "12/31/2026 11:00pm"
    )


def test_aggregation_interval_type():
    assert Aggregation.HOURLY.interval_type == "H"
    assert Aggregation.DAILY.interval_type == "D"


def test_jwt_exp_decoding():
    expected_exp = 1_900_000_000
    jwt = _make_jwt(expected_exp)
    assert MunicipalWaterAPI._decode_jwt_exp(jwt) == expected_exp


def test_jwt_exp_decoding_handles_garbage():
    assert MunicipalWaterAPI._decode_jwt_exp("not.a.jwt") is None
