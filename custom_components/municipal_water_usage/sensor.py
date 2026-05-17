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
    INCREMENTAL_HOURLY_DAYS,
    METER_LAST_REPORTED_KEY,
    METER_NAME,
    METER_REGISTER_READ_KEY,
    WATER_SENSOR_KEY,
)
from .exceptions import (
    WaterUsageAuthenticationError,
    WaterUsageError,
)
from .utils import sanitize_statistic_id_slug

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
        [
            WaterUsageSensor(
                coordinator=coordinator, config_entry=config_entry
            ),
            MeterLastReportedSensor(
                coordinator=coordinator, config_entry=config_entry
            ),
            MeterRegisterReadSensor(
                coordinator=coordinator, config_entry=config_entry
            ),
        ]
    )
    _LOGGER.debug("Municipal Water Usage sensors added successfully")


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

            for aggregation in (Aggregation.HOURLY, Aggregation.DAILY):
                await self._insert_statistics(aggregation)

            # Daily chart returns the current billing cycle in one request.
            tz = ZoneInfo(self.api.timezone)
            now_local = datetime.now(tz)
            data = await self.api.async_get_usage(
                aggregation=Aggregation.DAILY,
                start_datetime=now_local,
                end_datetime=now_local,
            )

            record = self._build_coordinator_record(data)

            usage = data.get("USAGE") if data else None
            if not usage:
                _LOGGER.warning(
                    "No recent daily water usage data received for account %s",
                    self.account_id,
                )
                record[WATER_SENSOR_KEY] = 0
                record[ATTR_LAST_READING_TIME] = now_local.replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                return {self.account_id: record}

            last_reading = usage[-1]
            _LOGGER.debug(
                "Latest daily reading: %s for account %s",
                last_reading,
                self.account_id,
            )

            record[WATER_SENSOR_KEY] = last_reading["consumption"]
            record[ATTR_LAST_READING_TIME] = last_reading["reading_time"]
            return {self.account_id: record}

        except WaterUsageAuthenticationError as err:
            _LOGGER.error("Authentication error fetching water data: %s", err)
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except WaterUsageError as err:
            _LOGGER.error("Error fetching data from water API: %s", err)
            raise UpdateFailed(f"Error communicating with water API: {err}") from err
        except Exception as err:
            _LOGGER.exception("Unexpected error fetching water data: %s", err)
            raise UpdateFailed(f"Unexpected error: {err}") from err

    @staticmethod
    def _build_coordinator_record(
        data: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Merge meter metadata from the latest TSM response into coordinator data."""
        record: Dict[str, Any] = {
            METER_NAME: data.get(METER_NAME) if data else None,
            METER_LAST_REPORTED_KEY: (
                data.get(METER_LAST_REPORTED_KEY) if data else None
            ),
            METER_REGISTER_READ_KEY: (
                data.get(METER_REGISTER_READ_KEY) if data else None
            ),
        }
        return record

    # Modeled on https://github.com/tronikos/opower/ for hourly statistics
    # backfill when realtime values aren't available.
    async def _insert_statistics(self, aggregation: Aggregation) -> None:
        """Retrieve usage data and append it to Home Assistant's long-term statistics."""
        account_slug = sanitize_statistic_id_slug(self.account_id)
        consumption_statistic_id = (
            f"{DOMAIN}:water_usage{aggregation.suffix}_{account_slug}"
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

        tz = ZoneInfo(self.api.timezone)
        now_local = datetime.now(tz)

        if not last_stat:
            _LOGGER.info(
                "Importing %s statistics for the first time (%s days)",
                aggregation.label,
                HISTORICAL_IMPORT_DAYS
                if aggregation == Aggregation.HOURLY
                else "current billing cycle",
            )
            consumption_sum = 0.0
            last_stats_time: Optional[float] = None

            if aggregation == Aggregation.HOURLY:
                range_start = now_local - timedelta(days=HISTORICAL_IMPORT_DAYS)
                usage_data = await self.api.async_get_hourly_usage_range(
                    range_start, now_local
                )
            else:
                usage_data = await self.api.async_get_usage(
                    aggregation=aggregation,
                    start_datetime=now_local,
                    end_datetime=now_local,
                )
        else:
            if aggregation == Aggregation.HOURLY:
                range_start = now_local - timedelta(days=INCREMENTAL_HOURLY_DAYS)
                _LOGGER.debug(
                    "Refreshing hourly statistics from %s", range_start.date()
                )
                usage_data = await self.api.async_get_hourly_usage_range(
                    range_start, now_local
                )
            else:
                range_start = datetime.fromtimestamp(
                    last_stat[consumption_statistic_id][0]["start"],
                    tz=timezone.utc,
                ).astimezone(tz) - timedelta(days=2)
                usage_data = await self.api.async_get_usage(
                    aggregation=aggregation,
                    start_datetime=range_start,
                    end_datetime=now_local,
                )

            if not usage_data or not usage_data.get("USAGE"):
                _LOGGER.warning(
                    "No data received from water API to populate %s stats",
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
                if stats and stats.get(consumption_statistic_id):
                    break
                if end:
                    _LOGGER.debug(
                        "Not found. Trying to find the oldest statistic after %s",
                        start,
                    )

            def _safe_get_sum(records: list[Any]) -> float:
                if records and "sum" in records[0]:
                    return float(records[0]["sum"])
                return 0.0

            if stats and stats.get(consumption_statistic_id):
                consumption_sum = _safe_get_sum(stats[consumption_statistic_id])
                last_stats_time = stats[consumption_statistic_id][0]["start"]
                _LOGGER.info(
                    "Updating %s statistics since %s",
                    aggregation.label,
                    last_stats_time,
                )
            else:
                _LOGGER.warning(
                    "No prior %s statistic at %s; rebuilding sum from zero",
                    aggregation.label,
                    start,
                )
                consumption_sum = 0.0
                last_stats_time = None

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

        if not consumption_statistics:
            _LOGGER.warning(
                "No %s statistics to import for %s",
                aggregation.label,
                consumption_statistic_id,
            )
            return

        _LOGGER.info(
            "Adding %s %s statistics for %s",
            len(consumption_statistics),
            aggregation.label,
            consumption_statistic_id,
        )
        async_add_external_statistics(
            self.hass, consumption_metadata, consumption_statistics
        )


class _MunicipalWaterEntity(CoordinatorEntity, SensorEntity):
    """Shared base for all municipal water usage sensor entities."""

    def __init__(
        self,
        coordinator: WaterUsageCoordinator,
        config_entry: ConfigEntry,
        *,
        unique_id_suffix: str,
        name: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._config = config_entry.data
        self.account_id: str = self._config.get("account_id", "Unknown")
        self._attr_unique_id = (
            f"{config_entry.unique_id or config_entry.entry_id}_"
            f"{self.account_id}_{unique_id_suffix}"
        )
        self._attr_name = name

    def _account_record(self) -> Dict[str, Any]:
        if not self.coordinator.data:
            return {}
        return self.coordinator.data.get(self.account_id, {})

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


class WaterUsageSensor(_MunicipalWaterEntity):
    """Latest daily water consumption from the municipal portal."""

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
        super().__init__(
            coordinator,
            config_entry,
            unique_id_suffix="water",
            name=f"Municipal Water Daily Usage - {config_entry.data.get('account_id', 'Unknown')}",
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
        value = self._account_record().get(WATER_SENSOR_KEY)
        if value is None:
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

        record = self._account_record()
        last_reading = record.get(ATTR_LAST_READING_TIME)
        if last_reading:
            attributes[ATTR_LAST_READING_TIME] = last_reading

        meter_name = record.get(METER_NAME)
        if meter_name:
            attributes[METER_NAME] = meter_name

        return attributes


class MeterLastReportedSensor(_MunicipalWaterEntity):
    """When the utility meter last communicated with Tyler Smart Meters."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-check-outline"

    def __init__(
        self,
        coordinator: WaterUsageCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            coordinator,
            config_entry,
            unique_id_suffix="meter_last_reported",
            name=(
                f"Municipal Water Meter Last Reported - "
                f"{config_entry.data.get('account_id', 'Unknown')}"
            ),
        )

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return (
            self.coordinator.last_update_success
            and self.native_value is not None
        )

    @property
    def native_value(self) -> Optional[datetime]:
        """Return the last-reported timestamp from the portal."""
        value = self._account_record().get(METER_LAST_REPORTED_KEY)
        if isinstance(value, datetime):
            return value
        return None


class MeterRegisterReadSensor(_MunicipalWaterEntity):
    """Cumulative register read shown on the portal billing sidebar."""

    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.GALLONS
    _attr_icon = "mdi:water-pump"

    def __init__(
        self,
        coordinator: WaterUsageCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            coordinator,
            config_entry,
            unique_id_suffix="meter_register_read",
            name=(
                f"Municipal Water Meter Read - "
                f"{config_entry.data.get('account_id', 'Unknown')}"
            ),
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
        """Return the cumulative register read in gallons."""
        value = self._account_record().get(METER_REGISTER_READ_KEY)
        if value is None:
            return None

        try:
            return float(value)
        except (ValueError, TypeError) as err:
            _LOGGER.warning(
                "Could not convert meter register read '%s' to float: %s",
                value,
                err,
            )
            return None
