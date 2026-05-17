"""Tests for the MunicipalWaterAPI client."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.municipal_water_usage.api import MunicipalWaterAPI


def _make_context_response(status: int = 200, text: str = "") -> MagicMock:
    """Build an aiohttp context-manager response mock."""
    response = AsyncMock()
    response.status = status
    response.text = AsyncMock(return_value=text)
    response.json = AsyncMock(return_value={})

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.mark.parametrize(
    "password",
    [
        "simplepassword",
        "password with spaces",
        "password#with#hashes",
        "password&with&ampersands",
        "password?with?questions",
        "password%with%percents",
        "password+with+plus",
        "complex!@#$%^&*()_+-=[]{}|;':\",./<>?password",
    ],
)
@pytest.mark.asyncio
async def test_login_sends_credentials_in_body(password):
    """async_login must POST credentials in the form body, not the URL."""
    email = "test+user@example.com"

    api = MunicipalWaterAPI(
        email=email,
        password=password,
        account_id="123456",
        host="bastroptx.municipalonlinepayments.com",
        timezone="America/Chicago",
    )

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=_make_context_response(200, ""))
    mock_session.post = MagicMock(
        return_value=_make_context_response(200, "Welcome back")
    )

    with patch.object(
        MunicipalWaterAPI, "_get_session", return_value=mock_session
    ):
        await api.async_login()

    assert api._authenticated is True

    args, kwargs = mock_session.post.call_args
    assert "data" in kwargs, (
        "Credentials should be sent in the request body (data), not URL parameters"
    )
    assert "params" not in kwargs or not kwargs["params"], (
        "Credentials should not be sent as URL parameters"
    )

    sent_payload = kwargs["data"]
    assert sent_payload["Password"] == password
    assert sent_payload["Email"] == email


@pytest.mark.asyncio
async def test_login_raises_on_invalid_credentials():
    """A 401 response from the login POST surfaces as an auth error."""
    from custom_components.municipal_water_usage.exceptions import (
        WaterUsageAuthenticationError,
    )

    api = MunicipalWaterAPI(
        email="test@example.com",
        password="wrong",
        account_id="123456",
        host="bastroptx.municipalonlinepayments.com",
        timezone="America/Chicago",
    )

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=_make_context_response(200, ""))
    mock_session.post = MagicMock(
        return_value=_make_context_response(401, "Unauthorized")
    )

    with patch.object(
        MunicipalWaterAPI, "_get_session", return_value=mock_session
    ):
        with pytest.raises(WaterUsageAuthenticationError):
            await api.async_login()
