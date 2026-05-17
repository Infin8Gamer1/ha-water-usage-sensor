"""End-to-end style tests for the Municipal Water Usage integration."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.municipal_water_usage import async_setup_entry
from custom_components.municipal_water_usage.api import MunicipalWaterAPI
from custom_components.municipal_water_usage.const import (
    DOMAIN,
    HGAL_TO_GALLONS,
)


@pytest.fixture
def mock_config_entry():
    """Create a mock config entry."""
    return MockConfigEntry(
        version=1,
        domain=DOMAIN,
        title="Municipal Water Usage Test",
        data={
            "email": "test@example.com",
            "password": "testpass",
            "account_id": "14-6402-01",
            "host": "bastroptx.municipalonlinepayments.com",
            "poll_interval": 60,
            "timezone": "America/Chicago",
        },
        unique_id=(
            "test@example.com_bastroptx.municipalonlinepayments.com_14-6402-01"
        ),
    )


@pytest.fixture
def mock_hass():
    """Create a mock Home Assistant instance."""
    hass = Mock(spec=HomeAssistant)
    hass.data = {}
    return hass


@pytest.fixture
def api_instance():
    """Create an API instance for testing."""
    api = MunicipalWaterAPI(
        email="test@example.com",
        password="testpass",
        account_id="14-6402-01",
        host="bastroptx.municipalonlinepayments.com",
        timezone="America/Chicago",
    )
    api._meter_name = "MIU 131356596"
    return api


def test_parse_tsm_html_converts_hgal_to_gallons(api_instance):
    """Values from series0.push are HGAL and must be multiplied by 100."""
    html = """
    <script>
    series0.push(['05/14/2026 00:00:00', 0.84]);
    series0.push(['05/14/2026 01:00:00', 0.23]);
    </script>
    """

    result = api_instance._parse_tsm_html(html)

    assert len(result["USAGE"]) == 2
    assert result["USAGE"][0]["consumption"] == pytest.approx(0.84 * HGAL_TO_GALLONS)
    assert result["USAGE"][1]["consumption"] == pytest.approx(0.23 * HGAL_TO_GALLONS)
    assert result["meter_name"] == "MIU 131356596"


def test_parse_tsm_html_attaches_configured_timezone(api_instance):
    """Reading_time must be a timezone-aware datetime in the configured zone."""
    html = "<script>series0.push(['05/14/2026 13:00:00', 1.0]);</script>"

    result = api_instance._parse_tsm_html(html)

    reading = result["USAGE"][0]
    assert reading["reading_time"].tzinfo is not None
    assert reading["reading_time"].year == 2026
    assert reading["reading_time"].month == 5
    assert reading["reading_time"].day == 14
    assert reading["reading_time"].hour == 13


def test_parse_tsm_html_clamps_negative_values(api_instance):
    """Negative consumption readings should be clamped to zero."""
    html = "<script>series0.push(['05/14/2026 00:00:00', -0.5]);</script>"

    result = api_instance._parse_tsm_html(html)

    assert result["USAGE"][0]["consumption"] == 0.0


def test_parse_tsm_html_handles_daily_format(api_instance):
    """Daily samples all use 00:00:00 wall-clock and should still parse."""
    html = """
    <script>
    series0.push(['03/29/2026 00:00:00', 2.82]);
    series0.push(['03/30/2026 00:00:00', 0.00]);
    series0.push(['03/31/2026 00:00:00', 2.46]);
    </script>
    """

    result = api_instance._parse_tsm_html(html)

    assert len(result["USAGE"]) == 3
    consumptions = [r["consumption"] for r in result["USAGE"]]
    assert consumptions == pytest.approx(
        [2.82 * HGAL_TO_GALLONS, 0.0, 2.46 * HGAL_TO_GALLONS]
    )


def test_parse_tsm_html_sorts_chronologically(api_instance):
    """If TSM returns out-of-order rows, we sort them."""
    html = """
    <script>
    series0.push(['05/14/2026 02:00:00', 0.10]);
    series0.push(['05/14/2026 00:00:00', 0.84]);
    series0.push(['05/14/2026 01:00:00', 0.23]);
    </script>
    """

    result = api_instance._parse_tsm_html(html)

    hours = [r["reading_time"].hour for r in result["USAGE"]]
    assert hours == [0, 1, 2]


def test_parse_tsm_html_no_data(api_instance):
    """An HTML response with no series0.push lines yields an empty list."""
    result = api_instance._parse_tsm_html("<html><body>nothing</body></html>")
    assert result["USAGE"] == []


@pytest.mark.asyncio
async def test_async_setup_entry_success(mock_hass, mock_config_entry):
    """Setup creates an API client, logs in, and stores the coordinator."""
    with patch(
        "custom_components.municipal_water_usage.MunicipalWaterAPI"
    ) as mock_api_class:
        mock_api = Mock()
        mock_api.async_login = AsyncMock(return_value=None)
        mock_api.close = AsyncMock()
        mock_api.host = "bastroptx.municipalonlinepayments.com"
        mock_api_class.return_value = mock_api

        with patch(
            "custom_components.municipal_water_usage.WaterUsageCoordinator"
        ) as mock_coordinator_cls:
            mock_coordinator = mock_coordinator_cls.return_value
            mock_coordinator.async_config_entry_first_refresh = AsyncMock()

            mock_hass.config_entries.async_forward_entry_setups = AsyncMock()

            result = await async_setup_entry(mock_hass, mock_config_entry)

            assert result is True
            assert hasattr(mock_config_entry, "runtime_data")


@pytest.mark.asyncio
async def test_async_setup_entry_connection_failure(mock_hass, mock_config_entry):
    """A login failure must surface as a ConfigEntryError."""
    from homeassistant.exceptions import ConfigEntryError

    with patch(
        "custom_components.municipal_water_usage.MunicipalWaterAPI"
    ) as mock_api_class:
        mock_api = Mock()
        mock_api.async_login = AsyncMock(side_effect=Exception("Connection failed"))
        mock_api.close = AsyncMock()
        mock_api_class.return_value = mock_api

        with pytest.raises(ConfigEntryError):
            await async_setup_entry(mock_hass, mock_config_entry)


def test_api_basic_attributes():
    """MunicipalWaterAPI attribute wiring."""
    api = MunicipalWaterAPI(
        email="test@example.com",
        password="testpass",
        account_id="14-6402-01",
        host="bastroptx.municipalonlinepayments.com",
        timezone="America/Chicago",
    )

    assert api.email == "test@example.com"
    assert api.account_id == "14-6402-01"
    assert api.timezone == "America/Chicago"
    assert api.host == "bastroptx.municipalonlinepayments.com"
    assert api._authenticated is False
    assert api._jwt is None
