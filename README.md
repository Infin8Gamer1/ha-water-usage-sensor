# Home Assistant Municipal Water Usage Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)
[![License](https://img.shields.io/github/license/jacobholyfield/ha-water-usage-sensor)](LICENSE)

A Home Assistant custom integration that pulls hourly **water consumption** data from a `municipalonlinepayments.com`-hosted utility portal (default target: City of Bastrop, TX) and imports it as long-term statistics so it shows up in Home Assistant's **Energy dashboard → Water** section.

This integration is forked from [`gagata/ha-smarthub-energy-sensor`](https://github.com/gagata/ha-smarthub-energy-sensor) and reuses its battle-tested statistics-import, coordinator, and config-flow patterns.

## Features

- **Energy Dashboard Integration** - hourly statistics back-fill into the Water section of the Energy dashboard
- **Hourly + Daily + Monthly statistics** - configurable historical backfill (90 days by default)
- **Secure authentication** - credentials stored by Home Assistant; portal login uses cookie-based sessions
- **Configurable polling** - 15 to 1440 minutes
- **Robust error handling** - automatic re-authentication on session expiry, retry with backoff on transient errors

## Installation

### Option 1: HACS (Recommended)

1. Open HACS in your Home Assistant instance
2. Click the three-dot menu and select "Custom repositories"
3. Add this repository URL: `https://github.com/jacobholyfield/ha-water-usage-sensor`
4. Select "Integration" as the category
5. Click "ADD" and then search for "Municipal Water Usage"
6. Click "Download" to install

### Option 2: Manual Installation

1. Download the latest release
2. Extract the `municipal_water_usage` folder to your `custom_components` directory
3. Restart Home Assistant

```
config/
└── custom_components/
    └── municipal_water_usage/
        ├── __init__.py
        ├── api.py
        ├── config_flow.py
        ├── const.py
        ├── exceptions.py
        ├── manifest.json
        ├── sensor.py
        ├── services.yaml
        ├── strings.json
        ├── utils.py
        └── translations/
            └── en.json
```

## Configuration

### Requirements

Before setting up the integration, gather:

1. **Email Address** - login email for the municipal portal
2. **Password** - portal password
3. **Account ID** - your utility account number (printed on your bill)
4. **Host** - portal hostname, e.g. `xxxx.municipalonlinepayments.com`
5. **Timezone** - local timezone of the utility (default: `America/Chicago`)

### Setup Process

1. Go to **Settings** → **Devices & Services**
2. Click **"+ Add Integration"**
3. Search for **"Municipal Water Usage"**
4. Fill in the form and submit. The integration validates by logging in to the portal.

## Energy Dashboard Integration

Once configured, the sensor publishes a water entity with the correct device and state classes for water monitoring.

### Adding to the Energy Dashboard

1. Go to **Settings** → **Dashboards** → **Energy**
2. Scroll to the **Water** section, click **"Add Water Source"**
3. Select the statistic named `municipal_water_usage:water_usage_<account>` (the hourly variant)

### Sensor Details

- **Device Class**: `water`
- **State Class**: `total_increasing`
- **Unit**: `gal` (`UnitOfVolume.GALLONS`)
- **Icon**: `mdi:water`

## Configuration Options

By default the integration polls every 6 hours. You can adjust this when configuring or reconfiguring the integration. Municipal portals typically update hourly with a delay of several hours, so very low polling intervals will not yield more frequent updates.

## Status

The portal-specific login URL, anti-forgery token field, and usage-data endpoint are currently **scaffolded** in `custom_components/municipal_water_usage/api.py` with clear `TODO` markers. Inspect the portal with browser DevTools (Network tab) and fill these in:

- `async_login`: confirm the login page URL, form field names (`Email`/`Password` shown as defaults), and any `__RequestVerificationToken` / `__VIEWSTATE` fields
- `async_get_usage`: confirm the usage-data URL, query parameter names, and JSON response shape (default parser expects `{"data": [{"x": <epoch_ms>, "y": <gallons>}, ...], "meterName": "..."}`)

The retry/session-refresh/statistics-import scaffolding is ready to use as-is.

## Troubleshooting

### Common Issues

**"Cannot Connect" Error**
- Verify the host is correct (no `https://`, no trailing slash)
- Check your internet connection
- Make sure the portal is reachable from your Home Assistant host

**"Invalid Authentication" Error**
- Double-check your email and password
- Try logging into the portal manually to verify
- Some portals temporarily lock out after repeated failures; wait and try again

**"No Data Available"**
- Confirm your account number is correct
- Check that recent usage data is visible on the portal manually
- Many municipal portals update hourly with a delay; back-filled hourly statistics are normal

**"Statistics offset from the right time"**
- Update the timezone to match the utility's local time (reconfigure the integration)

### Debug Logging

```yaml
# configuration.yaml
logger:
  default: info
  logs:
    custom_components.municipal_water_usage: debug
```

## Security & Privacy

- Credentials are stored by Home Assistant's encrypted configuration store
- All API calls use HTTPS with SSL verification
- Session cookies are handled in-memory only; the integration never writes them to disk

## Credits

- Forked from [`gagata/ha-smarthub-energy-sensor`](https://github.com/gagata/ha-smarthub-energy-sensor); statistics-import architecture inspired by [`tronikos/opower`](https://github.com/tronikos/opower)
- Adapted for municipal water portals by [@jacobholyfield](https://github.com/jacobholyfield)

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
