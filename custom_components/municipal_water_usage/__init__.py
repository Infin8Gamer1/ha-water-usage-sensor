"""Municipal Water Usage custom integration for Home Assistant.

Pulls hourly water consumption data from a municipalonlinepayments.com
utility portal (default: City of Bastrop, TX) and imports it as long-term
statistics so it appears in the Home Assistant Energy dashboard's Water
section.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError

from .api import MunicipalWaterAPI
from .const import (
    CONF_ACCOUNT_ID,
    CONF_EMAIL,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_POLL_INTERVAL,
    CONF_TIMEZONE,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TIMEZONE,
    DOMAIN,
)
from .sensor import WaterUsageCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Municipal Water Usage from a config entry."""
    config = entry.data

    required_fields = [CONF_EMAIL, CONF_PASSWORD, CONF_ACCOUNT_ID, CONF_HOST]
    missing_fields = [field for field in required_fields if not config.get(field)]
    if missing_fields:
        _LOGGER.error(
            "Missing required configuration fields: %s", missing_fields
        )
        raise ConfigEntryError(
            f"Missing configuration fields: {missing_fields}"
        )

    api = MunicipalWaterAPI(
        email=config[CONF_EMAIL],
        password=config[CONF_PASSWORD],
        account_id=config[CONF_ACCOUNT_ID],
        host=config[CONF_HOST],
        timezone=config.get(CONF_TIMEZONE, DEFAULT_TIMEZONE),
    )

    try:
        await api.async_login()
        _LOGGER.info("Successfully connected to %s", api.host)
    except Exception as err:
        _LOGGER.error("Failed to connect to municipal water portal: %s", err)
        await api.close()
        raise ConfigEntryError(
            f"Cannot connect to municipal water portal: {err}"
        ) from err

    poll_minutes = config.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
    coordinator = WaterUsageCoordinator(
        hass=hass,
        api=api,
        update_interval=timedelta(minutes=poll_minutes),
        config_entry=entry,
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: WaterUsageCoordinator | None = getattr(
            entry, "runtime_data", None
        )
        if coordinator is not None and getattr(coordinator, "api", None) is not None:
            await coordinator.api.close()

        if DOMAIN in hass.data:
            hass.data.pop(DOMAIN, None)

    return unload_ok
