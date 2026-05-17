"""Constants for the Municipal Water Usage integration."""

DOMAIN = "municipal_water_usage"

# Configuration keys
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_ACCOUNT_ID = "account_id"
CONF_HOST = "host"
CONF_POLL_INTERVAL = "poll_interval"
CONF_TIMEZONE = "timezone"

# Default values
DEFAULT_HOST = "bastroptx.municipalonlinepayments.com"
DEFAULT_TIMEZONE = "America/Chicago"
DEFAULT_POLL_INTERVAL = 360  # 6 hours in minutes
MIN_POLL_INTERVAL = 15  # Minimum 15 minutes
MAX_POLL_INTERVAL = 1440  # Maximum 24 hours

# API constants
DEFAULT_TIMEOUT = 30  # seconds
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds
SESSION_TIMEOUT = 300  # 5 minutes - force session refresh
HISTORICAL_IMPORT_DAYS = 90  # number of days for initial import

# Sensor constants
WATER_SENSOR_KEY = "current_water_usage"
ATTR_LAST_READING_TIME = "last_reading_time"
ATTR_ACCOUNT_ID = "account_id"
METER_NAME = "meter_name"
