"""Exceptions for the Municipal Water Usage integration."""


class WaterUsageError(Exception):
    """Base class for Municipal Water Usage errors."""


class WaterUsageConfigError(WaterUsageError):
    """Configuration error."""


class WaterUsageConnectionError(WaterUsageError):
    """Connection error."""


class WaterUsageAuthenticationError(WaterUsageError):
    """Authentication error."""


class WaterUsageDataError(WaterUsageError):
    """Data parsing error."""


class WaterUsageTimeoutError(WaterUsageError):
    """Request timeout error."""
