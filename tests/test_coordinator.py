"""Tests for the Municipal Water Usage coordinator (statistics import)."""
from collections.abc import Generator
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import Recorder, get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.municipal_water_usage.api import MunicipalWaterAPI
from custom_components.municipal_water_usage.const import DOMAIN
from custom_components.municipal_water_usage.sensor import WaterUsageCoordinator


@pytest.fixture
def mock_water_api(hass) -> Generator[AsyncMock]:
    """Mock MunicipalWaterAPI with the real parser bound to it."""
    real_api = MunicipalWaterAPI(
        email="test@example.com",
        password="testpass",
        account_id="123456",
        host="bastroptx.municipalonlinepayments.com",
        timezone="America/Chicago",
    )

    with patch(
        "custom_components.municipal_water_usage.api.MunicipalWaterAPI",
        autospec=True,
    ) as mock_api:
        mock_api.timezone = "America/Chicago"
        mock_api._authenticated = True
        mock_api.parse_usage = real_api.parse_usage
        mock_api.parse_usage_series = real_api.parse_usage_series
        mock_api.async_get_usage = AsyncMock(return_value={})
        yield mock_api


@pytest.fixture()
def mock_config_entry(hass) -> MockConfigEntry:
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


async def test_coordinator_first_run_imports_hourly_water_stats(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_water_api: AsyncMock,
) -> None:
    """Verify a fresh coordinator imports the parsed hourly usage as stats."""
    raw_payload = {
        "data": [
            {"x": 1762215300000, "y": 1},
            {"x": 1762216200000, "y": 10},
            {"x": 1762217100000, "y": 100},
            {"x": 1762218900000, "y": 1},
        ],
        "meterName": "WM-test",
    }

    mock_water_api.async_get_usage.return_value = mock_water_api.parse_usage(
        raw_payload
    )

    coordinator = WaterUsageCoordinator(
        hass,
        api=mock_water_api,
        update_interval=timedelta(minutes=720),
        config_entry=mock_config_entry,
    )

    await coordinator._async_update_data()

    await async_wait_recording_done(hass)

    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {f"{DOMAIN}:water_usage_123456"},
        "hour",
        None,
        {"state", "sum"},
    )

    statistic_id = f"{DOMAIN}:water_usage_123456"
    assert statistic_id in stats
    # The first hour folds 1 + 10 + 100 = 111 gallons
    assert stats[statistic_id][0]["sum"] == 111.0
    # The next hour adds the trailing 1-gallon reading
    assert stats[statistic_id][1]["sum"] == 112.0


async def async_wait_recording_done(hass) -> None:
    """Async wait until the recorder has flushed pending writes."""
    await hass.async_block_till_done()
    get_instance(hass)._async_commit(dt_util.utcnow())
    await hass.async_block_till_done()
    await hass.async_add_executor_job(get_instance(hass).block_till_done)
    await hass.async_block_till_done()
