"""Config flow for the Municipal Water Usage integration."""
from __future__ import annotations

import logging
import zoneinfo
from types import MappingProxyType
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import MunicipalWaterAPI
from .const import (
    CONF_ACCOUNT_ID,
    CONF_EMAIL,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_POLL_INTERVAL,
    CONF_TIMEZONE,
    DEFAULT_HOST,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TIMEZONE,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)
from .exceptions import (
    WaterUsageAuthenticationError,
    WaterUsageConnectionError,
)

_LOGGER = logging.getLogger(__name__)


class MunicipalWaterUsageConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Municipal Water Usage integration."""

    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await self._validate_input(user_input)
            except WaterUsageAuthenticationError:
                errors["base"] = "invalid_auth"
            except WaterUsageConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception during config validation")
                errors["base"] = "unknown"
            else:
                if self.source == config_entries.SOURCE_RECONFIGURE:
                    return self.async_update_reload_and_abort(
                        self._get_reconfigure_entry(), data_updates=user_input
                    )
                return self.async_create_entry(
                    title="Municipal Water Usage",
                    data=user_input,
                )

        schema_values: dict[str, Any] | MappingProxyType[str, Any] = {}
        if self.source == config_entries.SOURCE_RECONFIGURE:
            schema_values = self._get_reconfigure_entry().data

        timezones = await self.hass.async_add_executor_job(
            zoneinfo.available_timezones
        )

        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Required(CONF_ACCOUNT_ID): str,
                vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
                vol.Required(CONF_TIMEZONE, default=DEFAULT_TIMEZONE): SelectSelector(
                    SelectSelectorConfig(
                        options=list(timezones),
                        mode=SelectSelectorMode.DROPDOWN,
                        sort=True,
                    )
                ),
                vol.Required(
                    CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL),
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, schema_values),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle a reconfiguration config flow initialized by the user."""
        return await self.async_step_user(user_input)

    async def _validate_input(self, data: dict[str, Any]) -> None:
        """Validate the user input by attempting a portal login."""
        api = MunicipalWaterAPI(
            email=data[CONF_EMAIL],
            password=data[CONF_PASSWORD],
            account_id=data[CONF_ACCOUNT_ID],
            host=data[CONF_HOST],
            timezone=data[CONF_TIMEZONE],
        )

        try:
            await api.async_login()
        finally:
            await api.close()
