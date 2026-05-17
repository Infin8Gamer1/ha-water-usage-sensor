"""Municipal Water Usage sensor platform."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util.unit_conversion import VolumeConverter

try:
    from homeassistant.components.recorder.models import StatisticMeanType
except ImportError:
    from enum import Enum

    class StatisticMeanType(str, Enum):
        """Fallback StatisticMeanType for older HA versions."""

        NONE = "none"
        MEAN = "mean"
        MAX = "max"
        MIN = "min"


from .api import Aggregation, MunicipalWaterAPI
from .const import (
    ATTR_ACCOUNT_ID,
    ATTR_LAST_READING_TIME,
    DOMAIN,
    HISTORICAL_IMPORT_DAYS,
    METER_NAME,
    WATER_SENSOR_KEY,
)
from .exceptions import (
    WaterUsageAuthenticationError,
    WaterUsageError,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Municipal Water Usage sensor platform."""
    _LOGGER.debug("Setting up Municipal Water Usage sensor platform")

    coordinator: WaterUsageCoordinator = config_entry.runtime_data
    assert isinstance(coordinator, WaterUsageCoordinator)

    async_add_entities(
        [WaterUsageSensor(coordinator=coordinator, config_entry=config_entry)]
    )
    _LOGGER.debug("Municipal Water Usage sensor added successfully")


class WaterUsageCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Municipal Water Usage data."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: MunicipalWaterAPI,
        update_interval: timedelta,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{config_entry.entry_id}",
            update_interval=update_interval,
        )
        self.api = api
        self.account_id: str = config_entry.data.get("account_id", "unknown")
        self.config_entry = config_entry

    async def _async_update_data(self) -> Dict[str, Any]:
        """Fetch data from the Municipal Water Usage API."""
        try:
            _LOGGER.debug("Fetching data from Municipal Water Usage API")

            # Force re-authentication so we always start from a clean session.
            self.api._authenticated = False

            for aggregation in (
                Aggregation.HOURLY,
                Aggregation.DAILY,
                Aggregation.MONTHLY,
            ):
                await self._insert_statistics(aggregation)

            first_day_of_current_month = datetime.now().replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            data = await self.api.async_get_usage(
                aggregation=Aggregation.MONTHLY,
                start_datetime=first_day_of_current_month,
            )

            usage = data.get("USAGE") if data else None
            if not usage:
                _LOGGER.warning(
                    "No monthly water usage data received for account %s",
                    self.account_id,
                )
                return {
                    self.account_id: {
                        WATER_SENSOR_KEY: 0,
                        ATTR_LAST_READING_TIME: first_day_of_current_month.replace(
                            tzinfo=ZoneInfo(self.api.timezone)
                        ),
                        METER_NAME: data.get(METER_NAME) if data else None,
                    }
                }

            last_reading = usage[-1]
            _LOGGER.debug(
                "Latest monthly reading: %s for account %s",
                last_reading,
                self.account_id,
            )

            return {
                self.account_id: {
                    WATER_SENSOR_KEY: last_reading["consumption"],
                    ATTR_LAST_READING_TIME: last_reading["reading_time"],
                    METER_NAME: data.get(METER_NAME),
                }
            }

        except WaterUsageAuthenticationError as err:
            _LOGGER.error("Authentication error fetching water data: %s", err)
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except WaterUsageError as err:
            _LOGGER.error("Error fetching data from water API: %s", err)
            raise UpdateFailed(f"Error communicating with water API: {err}") from err
        except Exception as err:
            _LOGGER.exception("Unexpected error fetching water data: %s", err)
            raise UpdateFailed(f"Unexpected error: {err}") from err

    # Modeled on https://github.com/tronikos/opower/ for hourly statistics
    # backfill when realtime values aren't available.
    async def _insert_statistics(self, aggregation: Aggregation) -> None:
        """Retrieve usage data and append it to Home Assistant's long-term statistics."""
        consumption_statistic_id = (
            f"{DOMAIN}:water_usage{aggregation.suffix}_{self.account_id}"
        )
        consumption_unit_class = VolumeConverter.UNIT_CLASS
        consumption_unit = UnitOfVolume.GALLONS

        consumption_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=(
                f"Municipal Water {aggregation.label} Usage - "
                f"{self.account_id}"
            ),
            source=DOMAIN,
            statistic_id=consumption_statistic_id,
            unit_class=consumption_unit_class,
            unit_of_measurement=consumption_unit,
        )

        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            consumption_statistic_id,
            True,
            set(),
        )
        _LOGGER.debug("last_stat for %s: %s", aggregation.label, last_stat)

        if not last_stat:
            _LOGGER.debug(
                "Updating %s statistic for the first time", aggregation.label
            )
            consumption_sum = 0.0
            last_stats_time: Optional[float] = None

            start_datetime = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0
            ) - timedelta(days=HISTORICAL_IMPORT_DAYS)

            usage_data = await self.api.async_get_usage(
                aggregation=aggregation, start_datetime=start_datetime
            )
        else:
            start_datetime = datetime.fromtimestamp(
                last_stat[consumption_statistic_id][0]["start"], tz=timezone.utc
            )
            # Always backdate to avoid gaps if the portal back-fills late.
            start_datetime = start_datetime - timedelta(days=2)

            _LOGGER.debug(
                "Fetching %s statistics from %s", aggregation.label, start_datetime
            )
            usage_data = await self.api.async_get_usage(
                aggregation=aggregation, start_datetime=start_datetime
            )

            if not usage_data or not usage_data.get("USAGE"):
                _LOGGER.warning(
                    "No data received from water API to populate historical %s stats",
                    aggregation.label,
                )
                return

            start = usage_data["USAGE"][0]["reading_time"]
            _LOGGER.debug(
                "Getting %s statistics at: %s", aggregation.label, start
            )

            # Mirror opower's idiom: try to find the previous statistic at the
            # exact start, otherwise fall back to the oldest one after it. The
            # subsequent insert overwrites everything from there forward.
            stats = None
            for end in (start + timedelta(seconds=1), None):
                stats = await get_instance(self.hass).async_add_executor_job(
                    statistics_during_period,
                    self.hass,
                    start,
                    end,
                    {consumption_statistic_id},
                    aggregation.period,
                    None,
                    {"sum"},
                )
                if stats:
                    break
                if end:
                    _LOGGER.debug(
                        "Not found. Trying to find the oldest statistic after %s",
                        start,
                    )

            assert stats

            def _safe_get_sum(records: list[Any]) -> float:
                if records and "sum" in records[0]:
                    return float(records[0]["sum"])
                return 0.0

            consumption_sum = _safe_get_sum(
                stats.get(consumption_statistic_id, [])
            )
            last_stats_time = stats[consumption_statistic_id][0]["start"]

            _LOGGER.info(
                "Updating %s statistics since %s",
                aggregation.label,
                last_stats_time,
            )

        consumption_statistics: list[StatisticData] = []
        for reading in usage_data.get("USAGE", []):
            start = reading.get("reading_time")
            if last_stats_time is not None and start.timestamp() <= last_stats_time:
                continue

            consumption_state = max(0.0, float(reading.get("consumption", 0.0)))
            consumption_sum += consumption_state

            consumption_statistics.append(
                StatisticData(
                    start=start, state=consumption_state, sum=consumption_sum
                )
            )

        meter_name = usage_data.get(METER_NAME)
        if meter_name:
            consumption_metadata["name"] = (
                f"Municipal Water {aggregation.label} Usage - "
                f"{self.account_id} - {meter_name}"
            )

        _LOGGER.info(
            "Adding %s statistics for %s",
            len(consumption_statistics),
            consumption_statistic_id,
        )
        async_add_external_statistics(
            self.hass, consumption_metadata, consumption_statistics
        )


class WaterUsageSensor(CoordinatorEntity, SensorEntity):
    """Representation of a municipal water usage sensor."""

    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.GALLONS
    _attr_icon = "mdi:water"

    def __init__(
        self,
        coordinator: WaterUsageCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)

        self._config_entry = config_entry
        self._config = config_entry.data
        self.account_id: str = self._config.get("account_id", "Unknown")

        self._attr_unique_id = (
            f"{config_entry.unique_id or config_entry.entry_id}_{self.account_id}_water"
        )
        self._attr_name = (
            f"Municipal Water Monthly Usage - {self.account_id}"
        )

        _LOGGER.debug(
            "Initialized Municipal Water Usage sensor with unique_id: %s",
            self._attr_unique_id,
        )

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return (
            self.coordinator.last_update_success
            and self.native_value is not None
        )

    @property
    def native_value(self) -> Optional[float]:
        """Return the state of the sensor."""
        if not self.coordinator.data:
            return None

        value = (
            self.coordinator.data.get(self.account_id, {}).get(WATER_SENSOR_KEY)
        )
        if value is None:
            _LOGGER.debug("No water usage value found in coordinator data")
            return None

        try:
            return float(value)
        except (ValueError, TypeError) as err:
            _LOGGER.warning(
                "Could not convert water value '%s' to float: %s", value, err
            )
            return None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional state attributes."""
        attributes: Dict[str, Any] = {
            ATTR_ACCOUNT_ID: self.account_id,
        }

        if self.coordinator.data:
            record = self.coordinator.data.get(self.account_id, {})
            last_reading = record.get(ATTR_LAST_READING_TIME)
            if last_reading:
                attributes[ATTR_LAST_READING_TIME] = last_reading

            meter_name = record.get(METER_NAME)
            if meter_name:
                attributes[METER_NAME] = meter_name

        return attributes

    @property
    def device_info(self) -> Dict[str, Any]:
        """Return device information."""
        host = self._config.get("host", "Unknown")
        return {
            "identifiers": {
                (
                    DOMAIN,
                    self._config_entry.unique_id or self._config_entry.entry_id,
                )
            },
            "name": f"Municipal Water Usage ({self.account_id})",
            "manufacturer": "Municipal Online Payments",
            "model": "Water Meter",
            "configuration_url": f"https://{host}",
        }
