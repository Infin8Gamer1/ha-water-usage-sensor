"""Tests for utility functions."""
from zoneinfo import ZoneInfo

from custom_components.municipal_water_usage.utils import (
    parse_epoch_set_timezone,
    sanitize_host,
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
