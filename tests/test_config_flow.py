"""Test the Municipal Water Usage config flow."""
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.municipal_water_usage.const import DOMAIN
from custom_components.municipal_water_usage.exceptions import (
    WaterUsageAuthenticationError,
    WaterUsageConnectionError,
    WaterUsageDataError,
)


SAMPLE_INPUT = {
    "email": "test@example.com",
    "password": "test-password",
    "account_id": "14-6402-01",
    "host": "bastroptx.municipalonlinepayments.com",
    "timezone": "America/Chicago",
    "poll_interval": 360,
}


def _patch_api(login_side_effect=None, chart_side_effect=None):
    """Patch every MunicipalWaterAPI method exercised by the config flow."""
    return (
        patch(
            "custom_components.municipal_water_usage.config_flow."
            "MunicipalWaterAPI.async_login",
            side_effect=login_side_effect,
            return_value=None if login_side_effect is None else None,
        ),
        patch(
            "custom_components.municipal_water_usage.config_flow."
            "MunicipalWaterAPI.async_get_chart_context",
            side_effect=chart_side_effect,
            return_value=None if chart_side_effect is None else None,
        ),
        patch(
            "custom_components.municipal_water_usage.config_flow."
            "MunicipalWaterAPI.close",
            return_value=None,
        ),
    )


async def test_form(hass: HomeAssistant) -> None:
    """Test we get the form and create an entry on success."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {}

    login_patch, chart_patch, close_patch = _patch_api()
    with login_patch, chart_patch, close_patch, patch(
        "custom_components.municipal_water_usage.async_setup_entry",
        return_value=True,
    ) as mock_setup_entry:
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            SAMPLE_INPUT,
        )
        await hass.async_block_till_done()

    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert result2["title"] == "Municipal Water Usage"
    assert result2["data"] == SAMPLE_INPUT
    assert len(mock_setup_entry.mock_calls) == 1


async def test_form_invalid_auth(hass: HomeAssistant) -> None:
    """Test we handle invalid auth."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    login_patch, chart_patch, close_patch = _patch_api(
        login_side_effect=WaterUsageAuthenticationError
    )
    with login_patch, chart_patch, close_patch:
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            SAMPLE_INPUT,
        )

    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "invalid_auth"}


async def test_form_cannot_connect(hass: HomeAssistant) -> None:
    """Test we handle cannot connect error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    login_patch, chart_patch, close_patch = _patch_api(
        login_side_effect=WaterUsageConnectionError
    )
    with login_patch, chart_patch, close_patch:
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            SAMPLE_INPUT,
        )

    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "cannot_connect"}


async def test_form_invalid_account(hass: HomeAssistant) -> None:
    """A WaterUsageDataError from chart fetch must surface as invalid_account."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    login_patch, chart_patch, close_patch = _patch_api(
        chart_side_effect=WaterUsageDataError
    )
    with login_patch, chart_patch, close_patch:
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            SAMPLE_INPUT,
        )

    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "invalid_account"}


async def test_form_unknown_exception(hass: HomeAssistant) -> None:
    """Test we handle unknown exceptions."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    login_patch, chart_patch, close_patch = _patch_api(
        login_side_effect=Exception
    )
    with login_patch, chart_patch, close_patch:
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            SAMPLE_INPUT,
        )

    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "unknown"}
