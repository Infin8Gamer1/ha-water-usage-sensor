"""Utility functions for the Municipal Water Usage integration."""
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Home Assistant statistic IDs must match VALID_STATISTIC_ID in
# homeassistant/components/recorder/statistics.py — lowercase slug only.
_STATISTIC_SLUG_INVALID_RE = re.compile(r"[^a-z0-9_]+")


def sanitize_host(host: str) -> str:
    """Sanitize host: remove protocol and trailing slashes."""
    if not host:
        return ""
    return host.split("://")[-1].rstrip("/")


def sanitize_statistic_id_slug(value: str) -> str:
    """Convert a label (e.g. account ID) into a valid HA statistic slug segment.

    Statistic IDs must be ``<domain>:<slug>`` where each part is only
    ``[a-z0-9_]`` (no hyphens). Account numbers like ``14-6402-01`` become
    ``14_6402_01``.
    """
    slug = _STATISTIC_SLUG_INVALID_RE.sub("_", value.lower().strip())
    while "__" in slug:
        slug = slug.replace("__", "_")
    slug = slug.strip("_")
    return slug or "unknown"


def water_usage_statistic_id(account_id: str, suffix: str = "") -> str:
    """Build the external statistic_id for water usage (hourly or daily)."""
    from .const import DOMAIN

    slug = sanitize_statistic_id_slug(account_id)
    return f"{DOMAIN}:water_usage{suffix}_{slug}"


def parse_epoch_set_timezone(epoch: float, target_tz: ZoneInfo) -> datetime:
    """Return a datetime object based on a provided epoch as if that epoch was set in a specific timezone."""
    utc_datetime = datetime.fromtimestamp(epoch, tz=timezone.utc)
    zone_datetime = utc_datetime.replace(tzinfo=target_tz)
    return zone_datetime
