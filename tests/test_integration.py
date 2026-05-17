"""Test basic Municipal Water Usage integration functionality."""
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.municipal_water_usage import async_setup_entry
from custom_components.municipal_water_usage.api import MunicipalWaterAPI
from custom_components.municipal_water_usage.const import DOMAIN
from custom_components.municipal_water_usage.exceptions import WaterUsageDataError


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
            "account_id": "123456",
            "host": "bastroptx.municipalonlinepayments.com",
            "poll_interval": 60,
            "timezone": "America/Chicago",
        },
        unique_id="test@example.com_bastroptx.municipalonlinepayments.com_123456",
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
    return MunicipalWaterAPI(
        email="test@example.com",
        password="testpass",
        account_id="123456",
        host="bastroptx.municipalonlinepayments.com",
        timezone="America/Chicago",
    )


def test_parse_usage_valid_data(api_instance):
    """Test parsing valid usage data."""
    test_data = {
        "data": [
            {"x": 1640995200000, "y": 100.5},
            {"x": 1641081600000, "y": 150.2},
        ],
        "meterName": "WM-123",
    }

    result = api_instance.parse_usage(test_data)

    assert result is not None
    assert "USAGE" in result
    assert len(result["USAGE"]) == 2
    assert result["USAGE"][1]["consumption"] == 150.2
    assert result["meter_name"] == "WM-123"


def test_parse_usage_offset_hourly(api_instance):
    """Sub-hour samples should be folded into the same top-of-hour bucket."""
    test_data = {
        "data": [
            {"x": 1762215300000, "y": 1.1},
            {"x": 1762216200000, "y": 10.2},
            {"x": 1762217100000, "y": 100.3},
            {"x": 1762218900000, "y": 1.1},
        ]
    }

    result = api_instance.parse_usage(test_data)

    assert result is not None
    assert "USAGE" in result
    assert len(result["USAGE"]) == 2
    assert result["USAGE"][0]["consumption"] == pytest.approx(111.6)
    assert result["USAGE"][1]["consumption"] == pytest.approx(1.1)
    # 1762218900000 (:15 past) should be normalized to 1762218000000 (top of hour)
    assert result["USAGE"][1]["raw_timestamp"] == 1762218000000


def test_parse_usage_offset_start(api_instance):
    """First sample that starts off the hour should be aligned to top of hour."""
    test_data = {
        "data": [
            {"x": 1762215300000, "y": 1.1},
            {"x": 1762216200000, "y": 10.2},
            {"x": 1762217100000, "y": 100.3},
            {"x": 1762218000000, "y": 1.1},
        ]
    }

    result = api_instance.parse_usage(test_data)

    assert result is not None
    assert "USAGE" in result
    assert len(result["USAGE"]) == 2
    assert result["USAGE"][0]["consumption"] == pytest.approx(111.6)
    assert result["USAGE"][1]["consumption"] == pytest.approx(1.1)
    assert result["USAGE"][1]["raw_timestamp"] == 1762218000000


def test_parse_usage_fifteen_min(api_instance):
    """All four samples inside one hour should collapse into one bucket."""
    test_data = {
        "data": [
            {"x": 1762218000000, "y": 1.1},
            {"x": 1762218900000, "y": 10.2},
            {"x": 1762219800000, "y": 100.3},
            {"x": 1762220700000, "y": 1000.4},
        ]
    }

    result = api_instance.parse_usage(test_data)

    assert result is not None
    assert "USAGE" in result
    assert len(result["USAGE"]) == 1
    assert result["USAGE"][0]["consumption"] == pytest.approx(1112.0)
    assert result["USAGE"][0]["raw_timestamp"] == 1762218000000


def test_parse_usage_no_data(api_instance):
    """An empty data array yields an empty USAGE list."""
    test_data = {"data": []}

    result = api_instance.parse_usage(test_data)

    assert result is not None
    assert result["USAGE"] == []


def test_parse_usage_invalid_data(api_instance):
    """Passing a non-dict payload raises a WaterUsageDataError."""
    with pytest.raises(WaterUsageDataError):
        api_instance.parse_usage("invalid_data")


@pytest.mark.asyncio
async def test_async_setup_entry_success(mock_hass, mock_config_entry):
    """Test successful setup of a config entry."""
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
    """Test setup failure due to login error surfaces as ConfigEntryError."""
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
    """Test basic MunicipalWaterAPI attribute wiring."""
    api = MunicipalWaterAPI(
        email="test@example.com",
        password="testpass",
        account_id="123456",
        host="bastroptx.municipalonlinepayments.com",
        timezone="America/Chicago",
    )

    assert api.email == "test@example.com"
    assert api.account_id == "123456"
    assert api.timezone == "America/Chicago"
    assert api.host == "bastroptx.municipalonlinepayments.com"
    assert api._authenticated is False
