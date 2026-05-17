# Home Assistant Municipal Water Usage Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)
[![License](https://img.shields.io/github/license/Infin8Gamer1/ha-water-usage-sensor)](LICENSE)

A Home Assistant custom integration that pulls hourly **water consumption** data from a `municipalonlinepayments.com`-hosted utility portal (default target: City of Bastrop, TX) and imports it as long-term statistics so it shows up in Home Assistant's **Energy dashboard → Water** section.

This integration is forked from [`gagata/ha-smarthub-energy-sensor`](https://github.com/gagata/ha-smarthub-energy-sensor) and reuses its battle-tested statistics-import, coordinator, and config-flow patterns.

## Features

- **Energy Dashboard Integration** - hourly statistics back-fill into the Water section of the Energy dashboard
- **Hourly + Daily statistics** - configurable historical backfill (90 days by default)
- **Automatic OIDC login** - handles the full `account.municipalonlinepayments.com` OpenID Connect flow and the short-lived Tyler Smart Meters JWT in the background
- **HGAL → gallons** - readings come from the meter in Hundreds of Gallons; this integration converts them to gallons before storing
- **Configurable polling** - 15 to 1440 minutes
- **Robust error handling** - automatic JWT refresh, re-authentication on session expiry, retry with backoff on transient errors

## Installation

### Option 1: HACS (Recommended)

1. Open HACS in your Home Assistant instance
2. Click the three-dot menu and select "Custom repositories"
3. Add this repository URL: `https://github.com/Infin8Gamer1/ha-water-usage-sensor`
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
3. **Account ID** - your utility account number as it appears in the consumption page URL, e.g. `14-6402-01`
4. **Host** - tenant host portion of the portal URL, e.g. `bastroptx.municipalonlinepayments.com`
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

## How it works

The portal is a thin SaaS frontend (Municipal Online Payments) that delegates auth to a shared identity provider and reads meter data from Tyler Smart Meters:

1. **Login** (`async_login`) — drives the full OpenID Connect flow against `account.municipalonlinepayments.com`, including scraping the anti-forgery token from the login form and replaying the `signin-oidc` form_post callback that browsers normally auto-submit.
2. **Token acquisition** (`async_get_chart_context`) — fetches the per-account consumption page on the tenant host and extracts a ~30-minute JWT plus meter metadata from the `<script src="https://www.tylersmartmeters.com/charts.js" data-…>` block.
3. **Data fetch** (`async_get_usage`) — POSTs a minimal form (JWT + interval + date window + meter info) to `https://www.tylersmartmeters.com/`. The response is HTML with inline JS containing `series0.push(['MM/DD/YYYY HH:MM:SS', value])` lines.
4. **Unit conversion** — the meter reports in HGAL (hundreds of gallons), so every value is multiplied by 100 to produce gallons before storage.

JWTs are refreshed automatically when they expire; full re-login happens if the chart context is also rejected.

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
- Confirm the account ID matches the value in the consumption page URL (e.g. `14-6402-01`)
- Check that recent usage data is visible on the portal manually
- Many municipal portals update hourly with a delay; back-filled hourly statistics are normal

**"Account ID not found on this portal"**
- The portal accepted your credentials but the consumption page for that account ID does not exist. Double-check the number in `https://<host>/bastroptx/utilities/accounts/consumption/<this-part>`

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

## Local Development

You can develop and validate the API scraping layer **without a running Home Assistant instance**. The live smoke runner drives the real portal against your credentials and prints the parsed hourly/daily readings to the console.

### Initial setup

Windows (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip wheel
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

Copy-Item .env.example .env
# Edit .env and fill in MWU_EMAIL / MWU_PASSWORD / MWU_ACCOUNT_ID / MWU_HOST
```

macOS / Linux / WSL:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip wheel
./.venv/bin/python -m pip install -r requirements-dev.txt

cp .env.example .env
# Edit .env and fill in MWU_EMAIL / MWU_PASSWORD / MWU_ACCOUNT_ID / MWU_HOST
```

### Live API smoke runner

`scripts/test_api_live.py` is the primary local feedback loop. It loads `.env`, drives the real portal, and prints what it scrapes:

```powershell
# Run all four stages: login -> chart context -> hourly fetch -> daily fetch
.\.venv\Scripts\python.exe scripts/test_api_live.py

# Just login + JWT extraction (fast, useful when iterating on the OIDC flow)
.\.venv\Scripts\python.exe scripts/test_api_live.py --steps login,chart-context

# DEBUG-level logging shows every HTTP request, redirect, retry
.\.venv\Scripts\python.exe scripts/test_api_live.py -v

# Print every reading instead of the first 24
.\.venv\Scripts\python.exe scripts/test_api_live.py --steps fetch-hourly --full
```

The script ignores the integration's HA `__init__.py` and only imports `api.py` + its dependencies, so it works on native Windows without WSL.

If you use VS Code / Cursor, `.vscode/launch.json` ships with debug configurations for each stage — set breakpoints in `api.py` and run "Live API smoke (login only)" from the Run/Debug panel to step through the OIDC flow.

### Tests

The pure-Python unit tests (parser, formatters, OIDC HTML scraping, JWT decoding) run anywhere:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_api.py tests/test_utils.py -p "no:homeassistant"
```

The full integration tests (`test_config_flow.py`, `test_coordinator.py`, `test_integration.py`) exercise Home Assistant's recorder, which imports `fcntl` and therefore only runs on Linux / macOS / WSL. CI runs the full suite via [`.github/workflows/tests.yml`](.github/workflows/tests.yml).

To run the full suite locally on WSL or Linux:

```bash
./.venv/bin/python -m pytest tests/
```

### Deploying to Home Assistant

Once the smoke runner reports good data, copy the integration to your HA instance:

```bash
# From the repo root on your dev machine
scp -r custom_components/municipal_water_usage \
    pi@homeassistant.local:/config/custom_components/

# On the HA host, restart Home Assistant Core
ssh pi@homeassistant.local "ha core restart"
```

Or use the HACS "Reinstall" flow from the HA UI once this repo is published.

## Credits

- Forked from [`gagata/ha-smarthub-energy-sensor`](https://github.com/gagata/ha-smarthub-energy-sensor); statistics-import architecture inspired by [`tronikos/opower`](https://github.com/tronikos/opower)
- Adapted for municipal water portals by [@Infin8Gamer1](https://github.com/infin8gamer1)

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
