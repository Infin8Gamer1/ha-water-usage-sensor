# Municipal Water Usage Integration

A Home Assistant custom integration that connects to a municipal utility portal hosted on `municipalonlinepayments.com` (defaults to the City of Bastrop, TX) and imports hourly water consumption into Home Assistant.

## Features

- Energy dashboard compatible: imports long-term statistics so usage appears in the **Energy → Water** section
- Hourly + daily statistics for back-dated history (90 days by default)
- Automatically handles the multi-step OpenID Connect login and the short-lived Tyler Smart Meters JWT
- Converts the meter's native HGAL (hundreds of gallons) readings to gallons before storing
- Configurable polling interval (15-1440 minutes)
- Secure credential storage via Home Assistant's config flow
- Robust retry / session-refresh logic

## Installation

1. Add this repository to HACS as a custom repository
2. Install the "Municipal Water Usage" integration
3. Restart Home Assistant
4. Add the integration through the UI (Settings → Devices & Services → Add Integration)

## Configuration

You'll need:

- Email address used to log in to the portal
- Portal password
- Account ID as it appears in the consumption page URL (e.g. `14-6402-01`)
- Portal host (e.g. `bastroptx.municipalonlinepayments.com`)
- Local timezone (default: `America/Chicago`)

## Support

Visit the [GitHub repository](https://github.com/jacobholyfield/ha-water-usage-sensor) for documentation, issues, and updates.
