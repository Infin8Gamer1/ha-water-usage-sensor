"""Tests for the Municipal Water Usage coordinator (statistics import)."""
from __future__ import annotations

from collections.abc import Generator
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import Recorder, get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.municipal_water_usage.api import (
    Aggregation,
    MunicipalWaterAPI,
)
from custom_components.municipal_water_usage.const import HGAL_TO_GALLONS
from custom_components.municipal_water_usage.sensor import WaterUsageCoordinator
from custom_components.municipal_water_usage.utils import water_usage_statistic_id


# Synthetic TSM responses for hourly + daily intervals.
HOURLY_TSM_HTML = """
<script>
series0.push(['05/14/2026 00:00:00', 0.50]);
series0.push(['05/14/2026 01:00:00', 0.25]);
series0.push(['05/14/2026 02:00:00', 1.00]);
series0.push(['05/14/2026 03:00:00', 0.00]);
</script>
"""

DAILY_TSM_HTML = """
<script>
series0.push(['05/14/2026 00:00:00', 1.75]);
series0.push(['05/15/2026 00:00:00', 2.10]);
</script>
"""


def _fake_get_usage(self, aggregation, start_datetime, end_datetime=None):
    """Replace network calls with the in-process HTML parser."""
    html = HOURLY_TSM_HTML if aggregation == Aggregation.HOURLY else DAILY_TSM_HTML
    return self._parse_tsm_html(html)


@pytest.fixture
def mock_water_api() -> Generator[MunicipalWaterAPI]:
    """Return a real API object whose network methods are stubbed out."""
    api = MunicipalWaterAPI(
        email="test@example.com",
        password="testpass",
        account_id="14-6402-01",
        host="bastroptx.municipalonlinepayments.com",
        timezone="America/Chicago",
    )
    api._authenticated = True
    api._jwt = "fake.jwt.token"
    api._meter_name = "MIU 131356596"

    async def fake_async_get_usage(aggregation, start_datetime, end_datetime=None):
        return _fake_get_usage(api, aggregation, start_datetime, end_datetime)

    async def fake_async_get_hourly_usage_range(
        start_datetime, end_datetime=None
    ):
        return _fake_get_usage(api, Aggregation.HOURLY, start_datetime, end_datetime)

    with patch.object(
        api, "async_get_usage", side_effect=fake_async_get_usage
    ), patch.object(
        api,
        "async_get_hourly_usage_range",
        side_effect=fake_async_get_hourly_usage_range,
    ):
        yield api


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
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
        unique_id="test@example.com_bastroptx.municipalonlinepayments.com_14-6402-01",
    )


async def test_coordinator_first_run_imports_hourly_water_stats(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_water_api: MunicipalWaterAPI,
) -> None:
    """Verify a fresh coordinator imports hourly TSM data as long-term stats."""
    coordinator = WaterUsageCoordinator(
        hass,
        api=mock_water_api,
        update_interval=timedelta(minutes=720),
        config_entry=mock_config_entry,
    )

    await coordinator._async_update_data()
    await _async_wait_recording_done(hass)

    statistic_id = water_usage_statistic_id("14-6402-01")
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )

    assert statistic_id in stats
    # Cumulative sum across the four hourly readings (HGAL × 100).
    sums = [point["sum"] for point in stats[statistic_id]]
    assert sums[0] == pytest.approx(0.50 * HGAL_TO_GALLONS)
    assert sums[-1] == pytest.approx(
        (0.50 + 0.25 + 1.00 + 0.00) * HGAL_TO_GALLONS
    )


async def test_coordinator_also_imports_daily_stats(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_water_api: MunicipalWaterAPI,
) -> None:
    """Verify daily statistics get a separate statistic_id."""
    coordinator = WaterUsageCoordinator(
        hass,
        api=mock_water_api,
        update_interval=timedelta(minutes=720),
        config_entry=mock_config_entry,
    )

    await coordinator._async_update_data()
    await _async_wait_recording_done(hass)

    daily_id = water_usage_statistic_id("14-6402-01", "_daily")
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {daily_id},
        "day",
        None,
        {"state", "sum"},
    )

    assert daily_id in stats
    sums = [point["sum"] for point in stats[daily_id]]
    assert sums[-1] == pytest.approx((1.75 + 2.10) * HGAL_TO_GALLONS)


async def _async_wait_recording_done(hass: HomeAssistant) -> None:
    """Async wait until the recorder has flushed pending writes."""
    await hass.async_block_till_done()
    get_instance(hass)._async_commit(dt_util.utcnow())
    await hass.async_block_till_done()
    await hass.async_add_executor_job(get_instance(hass).block_till_done)
    await hass.async_block_till_done()
