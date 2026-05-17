"""Tests for utility functions."""
from zoneinfo import ZoneInfo

from custom_components.municipal_water_usage.utils import (
    parse_epoch_set_timezone,
    sanitize_host,
    sanitize_statistic_id_slug,
    water_usage_statistic_id,
)


def test_sanitize_host():
    """Test that the host is correctly sanitized."""
    test_cases = [
        ("bastroptx.municipalonlinepayments.com/", "bastroptx.municipalonlinepayments.com"),
        ("https://bastroptx.municipalonlinepayments.com", "bastroptx.municipalonlinepayments.com"),
        ("https://bastroptx.municipalonlinepayments.com/", "bastroptx.municipalonlinepayments.com"),
        ("bastroptx.municipalonlinepayments.com", "bastroptx.municipalonlinepayments.com"),
        ("http://bastroptx.municipalonlinepayments.com", "bastroptx.municipalonlinepayments.com"),
        ("", ""),
        (None, ""),
    ]

    for input_host, expected_host in test_cases:
        assert sanitize_host(input_host) == expected_host


def test_sanitize_statistic_id_slug():
    """Account IDs with hyphens must become valid HA statistic slugs."""
    assert sanitize_statistic_id_slug("14-6402-01") == "14_6402_01"
    assert sanitize_statistic_id_slug("  ABC-123  ") == "abc_123"
    assert sanitize_statistic_id_slug("") == "unknown"


def test_water_usage_statistic_id():
    assert water_usage_statistic_id("14-6402-01") == (
        "municipal_water_usage:water_usage_14_6402_01"
    )
    assert water_usage_statistic_id("14-6402-01", "_daily") == (
        "municipal_water_usage:water_usage_daily_14_6402_01"
    )


def test_parse_epoch_set_timezone():
    """Test that the epoch parser keeps the wall-clock time but swaps the zone."""
    test_cases = [
        (1770285600, ZoneInfo("America/Los_Angeles"), "2026-02-05T10:00:00-08:00"),
        (1770285600.000, ZoneInfo("America/Los_Angeles"), "2026-02-05T10:00:00-08:00"),
        (1770285600.000, ZoneInfo("America/New_York"), "2026-02-05T10:00:00-05:00"),
        (1770285600.000, ZoneInfo("America/Chicago"), "2026-02-05T10:00:00-06:00"),
    ]

    for input_epoch, input_tz, expected_iso in test_cases:
        assert (
            parse_epoch_set_timezone(input_epoch, input_tz).isoformat()
            == expected_iso
        )
