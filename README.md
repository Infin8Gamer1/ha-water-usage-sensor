# Home Assistant Municipal Water Usage Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)
[![License](https://img.shields.io/github/license/Infin8Gamer1/ha-water-usage-sensor)](LICENSE)

A Home Assistant custom integration that pulls **hourly and daily water consumption** from a `municipalonlinepayments.com`-hosted utility portal (default target: City of Bastrop, TX) and imports it as long-term statistics so it shows up in Home Assistant's **Energy dashboard → Water** section.

This integration is forked from [`gagata/ha-smarthub-energy-sensor`](https://github.com/gagata/ha-smarthub-energy-sensor) and reuses its statistics-import, coordinator, and config-flow patterns.

## Features

- **Energy Dashboard integration** — hourly statistics back-fill into the Water section of the Energy dashboard
- **Hourly + daily statistics** — configurable historical backfill (90 days by default)
- **Automatic OIDC login** — full `account.municipalonlinepayments.com` OpenID Connect flow plus short-lived Tyler Smart Meters JWT handling
- **HGAL → gallons** — chart usage values are in Hundreds of Gallons; converted to gallons before storage
- **Meter telemetry sensors** — portal “Meter last reported” time and cumulative register read
- **Configurable polling** — 15 to 1440 minutes (default 6 hours)
- **Robust error handling** — JWT refresh, re-authentication on session expiry, retries with backoff

## Installation

### Option 1: HACS (Recommended)

1. Open HACS in your Home Assistant instance
2. Click the three-dot menu and select **Custom repositories**
3. Add this repository URL: `https://github.com/Infin8Gamer1/ha-water-usage-sensor`
4. Select **Integration** as the category
5. Click **ADD**, then search for **Municipal Water Usage**
6. Click **Download** to install

### Option 2: Manual Installation

1. Copy the `municipal_water_usage` folder into `config/custom_components/`
2. Restart Home Assistant

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

Before setup, gather:

1. **Email** — portal login email
2. **Password** — portal password
3. **Account ID** — utility account number from the consumption page URL (e.g. `14-6402-01`)
4. **Host** — tenant host only, e.g. `bastroptx.municipalonlinepayments.com` (no `https://`)
5. **Timezone** — utility local time (default: `America/Chicago`)

### Setup

1. **Settings** → **Devices & Services** → **Add Integration**
2. Search for **Municipal Water Usage**
3. Enter credentials and account details. The integration validates by logging in and loading the consumption page for your account ID.

The first coordinator refresh can take several minutes while up to 90 days of hourly and daily statistics are imported.

## Entities

Each configured account creates one device with three sensors:

| Entity | What it shows |
|--------|----------------|
| **Municipal Water Daily Usage** | Gallons consumed on the most recent day returned in the daily chart (live snapshot, not the Energy dashboard source) |
| **Municipal Water Meter Last Reported** | When the utility meter last communicated (`device_class: timestamp`) — matches the portal billing sidebar |
| **Municipal Water Meter Read** | Cumulative register read in gallons (`device_class: water`, `state_class: total_increasing`) — matches **Read:** on the portal |

The daily usage sensor also exposes attributes:

- `account_id` — configured account number
- `last_reading_time` — timestamp of that daily usage bar (not the same as “Meter last reported”)
- `meter_name` — e.g. `MIU 131356596`

## Energy Dashboard Integration

Use the **hourly** long-term statistic for the Energy dashboard, not the live daily sensor state.

1. **Settings** → **Dashboards** → **Energy**
2. Under **Water**, click **Add Water Source**
3. Select the **hourly** statistic for your account (no `_daily_` in the name)

Statistic IDs use a slug derived from your account ID (hyphens become underscores). For account `14-6402-01`:

| Statistic | Statistic ID |
|-----------|----------------|
| Hourly (Energy dashboard) | `municipal_water_usage:water_usage_14_6402_01` |
| Daily | `municipal_water_usage:water_usage_daily_14_6402_01` |

### First import timing

Tyler Smart Meters returns **one day of hourly data per API request**. On first setup the integration loops over the last **90 calendar days** (about 90 requests). That can take **several minutes**; watch **Settings → System → Logs** for `Fetching hourly usage for YYYY-MM-DD` progress lines. The Energy dashboard stays empty until this finishes.

Each later poll refreshes the last **14 days** of hourly data so late portal backfill is picked up.

## Configuration Options

Default polling is every **6 hours**. Municipal portals typically post hourly usage with a delay of several hours; polling more often does not pull new data faster.

Reconfigure via **Settings** → **Devices & Services** → your integration → **Configure**.

## How it works

The portal (Municipal Online Payments) authenticates via a shared identity provider and loads charts from Tyler Smart Meters:

1. **Login** (`async_login`) — OpenID Connect against `account.municipalonlinepayments.com`, including the `signin-oidc` form_post callback (single- or double-quoted HTML attributes).
2. **Chart context** (`async_get_chart_context`) — Loads the account consumption page and extracts a ~30-minute JWT, meter metadata, and billing history from the `charts.js` script tag.
3. **Usage fetch** (`async_get_usage` / `async_get_hourly_usage_range`) — POSTs the full chart view-state to `https://www.tylersmartmeters.com/`. Hourly mode returns one calendar day per request, so historical import loops day-by-day. The HTML response includes `series0.push([...])` usage lines and the billing sidebar (“Meter last reported”, register read).
4. **Unit conversion** — chart values are HGAL; multiplied by 100 for gallons. Register read from the sidebar is already shown in gallons on the portal.

JWTs refresh automatically; full re-login runs if the tenant session or chart context is rejected.

## Troubleshooting

**Cannot connect**

- Host must be hostname only (no `https://`, no path)
- Confirm the portal is reachable from the Home Assistant host

**Invalid authentication**

- Verify email/password in the portal UI
- Wait if the account was locked after repeated failures

**No data / empty Energy chart**

- Wait for the **first hourly backfill** to finish (see [First import timing](#first-import-timing)); the chart only fills after ~90 daily requests complete
- Confirm usage appears on the portal for the same account ID
- Hourly data often lags by several hours
- If you installed an older build that only imported a single day, remove the integration, clear the statistic under **Developer tools → Statistics**, then add the integration again
- Check logs with debug logging enabled (below)

**Account ID not found**

- Credentials worked but the consumption URL for that account ID does not exist

**Statistics offset in time**

- Set timezone to the utility’s local zone (reconfigure the integration)

**Meter Last Reported / Meter Read unavailable**

- These are parsed from the Tyler chart HTML. If the portal layout changes or the billing card is hidden, the sensors may stay unavailable while usage statistics still work.

### Debug logging

```yaml
# configuration.yaml
logger:
  default: info
  logs:
    custom_components.municipal_water_usage: debug
```

## Security & Privacy

- Credentials are stored in Home Assistant’s encrypted config store
- HTTPS with certificate verification
- Session cookies stay in memory only

## Local Development

You can validate scraping **without** a running Home Assistant instance.

### Setup

Windows (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip wheel
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
# Edit .env: MWU_EMAIL, MWU_PASSWORD, MWU_ACCOUNT_ID, MWU_HOST
```

macOS / Linux / WSL:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip wheel
./.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env
```

### Live API smoke runner

```powershell
# All stages: login → chart context → hourly → daily
.\.venv\Scripts\python.exe scripts/test_api_live.py

# Login + JWT only
.\.venv\Scripts\python.exe scripts/test_api_live.py --steps login,chart-context

.\.venv\Scripts\python.exe scripts/test_api_live.py -v
```

VS Code / Cursor: use **Live API smoke** configurations in `.vscode/launch.json`.

### Unit tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_api.py tests/test_utils.py -p "no:homeassistant"
```

Full HA integration tests need Linux / macOS / WSL (recorder / `fcntl`). CI runs the complete suite in [`.github/workflows/tests.yml`](.github/workflows/tests.yml).

### Deploy to Home Assistant

```bash
scp -r custom_components/municipal_water_usage \
    user@homeassistant.local:/config/custom_components/
# Restart Home Assistant Core
```

Or reinstall via HACS after publishing the repo.

## Credits

- Forked from [`gagata/ha-smarthub-energy-sensor`](https://github.com/gagata/ha-smarthub-energy-sensor); statistics architecture inspired by [`tronikos/opower`](https://github.com/tronikos/opower)
- Adapted for municipal water portals by [@Infin8Gamer1](https://github.com/infin8gamer1)

## License

MIT License — see [LICENSE](LICENSE).
