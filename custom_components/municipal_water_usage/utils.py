"""Utility functions for the Municipal Water Usage integration."""
from zoneinfo import ZoneInfo
from datetime import datetime, timezone


def sanitize_host(host: str) -> str:
    """Sanitize host: remove protocol and trailing slashes."""
    if not host:
        return ""
    return host.split("://")[-1].rstrip("/")


def parse_epoch_set_timezone(epoch: float, target_tz: ZoneInfo) -> datetime:
    """Return a datetime object based on a provided epoch as if that epoch was set in a specific timezone."""
    utc_datetime = datetime.fromtimestamp(epoch, tz=timezone.utc)
    zone_datetime = utc_datetime.replace(tzinfo=target_tz)
    return zone_datetime
