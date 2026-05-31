"""Reproduction tests for the end-of-month hourly data gap.

Symptom (reported): hourly usage for 5/29, 5/30 and 5/31 is missing from the
Home Assistant Energy dashboard even though the municipal portal clearly shows
those hours.

Hypothesis: the *in-progress* (NotBilledYet) billing cycle that we advertise to
Tyler Smart Meters is computed in ``_current_bill_cycle`` and its end date is
hard-capped to at most the 28th of the month::

    cycle_end = cycle_start + timedelta(days=31)
    cycle_end = cycle_end.replace(day=min(cycle_start.day, 28))

Every per-day hourly POST re-sends this cycle context (``date_ranges`` +
``original_end_date``). When "today" is later than the advertised cycle end,
the requested day falls outside every range we describe to the server, so TSM
injects no series data for it and the hour silently disappears.

These tests pin "now" to 2026-05-31 with a most-recent bill that ended on
2026-04-29 (so the buggy cap produces an in-progress cycle ending 2026-05-28)
and assert the *correct* behaviour: the advertised in-progress cycle must cover
the recent days.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


def _load_api_module():
    """Load ``api.py`` without importing the HA-dependent package __init__.

    The integration's ``__init__.py`` imports ``homeassistant`` which is not
    installed on native Windows. ``api.py`` itself has no HA dependency, so we
    hand-load it (mirroring ``scripts/test_api_live.py``) to keep this
    reproduction runnable everywhere.
    """
    if "homeassistant" in sys.modules or _ha_importable():
        import custom_components.municipal_water_usage.api as mod  # type: ignore

        return mod

    repo_root = Path(__file__).resolve().parent.parent
    pkg_dir = repo_root / "custom_components" / "municipal_water_usage"
    pkg = types.ModuleType("municipal_water_usage")
    pkg.__path__ = [str(pkg_dir)]
    sys.modules["municipal_water_usage"] = pkg
    for submodule in ("const", "exceptions", "utils", "api"):
        spec = importlib.util.spec_from_file_location(
            f"municipal_water_usage.{submodule}", pkg_dir / f"{submodule}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"municipal_water_usage.{submodule}"] = module
        spec.loader.exec_module(module)
    return sys.modules["municipal_water_usage.api"]


def _ha_importable() -> bool:
    return importlib.util.find_spec("homeassistant") is not None


api_module = _load_api_module()
Aggregation = api_module.Aggregation
MunicipalWaterAPI = api_module.MunicipalWaterAPI

# The most recent BILLED cycle ended on the 29th. The in-progress cycle starts
# here; "today" is two days past where the buggy 28th-cap lands.
LAST_BILL_END = "2026-04-29T00:00:00"
FROZEN_NOW = datetime(2026, 5, 31, 14, 0, 0)


class _FrozenDatetime(datetime):
    """``datetime`` subclass with a pinned ``now()`` for deterministic tests."""

    @classmethod
    def now(cls, tz=None):  # noqa: D102 - matches datetime.now signature
        if tz is not None:
            return FROZEN_NOW.replace(tzinfo=tz)
        return FROZEN_NOW


def _make_api() -> MunicipalWaterAPI:
    api = MunicipalWaterAPI(
        email="user@example.com",
        password="hunter2",
        account_id="14-6402-01",
        host="bastroptx.municipalonlinepayments.com",
        timezone="America/Chicago",
    )
    api._meter_name = "MIU 131356596"
    api._monthly_bills_json = json.dumps(
        [
            {
                "Month": 4,
                "Year": 2026,
                "Amount": 77,
                "StartDate": "2026-03-29T00:00:00",
                "EndDate": LAST_BILL_END,
                "TotalServiceCharge": 61.04,
            }
        ]
    )
    api._jwt = "header.payload.sig"
    return api


def _in_progress_end_date(date_ranges_json: str) -> datetime:
    """Return the EndDate of the NotBilledYet range we advertise to TSM."""
    ranges = json.loads(date_ranges_json)
    in_progress = [r for r in ranges if r.get("NotBilledYet")]
    assert in_progress, "expected an in-progress (NotBilledYet) cycle range"
    return datetime.fromisoformat(in_progress[-1]["EndDate"])


def test_current_bill_cycle_end_covers_today():
    """The ongoing cycle must extend through 'today', not stop at the 28th."""
    api = _make_api()

    with patch.object(api_module, "datetime", _FrozenDatetime):
        _month, _year, cycle_start, cycle_end = api._current_bill_cycle()

    assert cycle_start.date() == datetime(2026, 4, 29).date()
    assert cycle_end.date() >= FROZEN_NOW.date(), (
        f"in-progress cycle ends {cycle_end.date()} which is before today "
        f"{FROZEN_NOW.date()} - recent days will be omitted by TSM"
    )


def test_advertised_date_ranges_cover_recent_days():
    """date_ranges JSON sent to TSM must include 5/29, 5/30 and 5/31."""
    api = _make_api()

    with patch.object(api_module, "datetime", _FrozenDatetime):
        date_ranges_json, _series = api._build_cycle_payloads()

    end_date = _in_progress_end_date(date_ranges_json)
    for day in (29, 30, 31):
        target = datetime(2026, 5, day)
        assert target <= end_date, (
            f"5/{day} is outside the advertised in-progress cycle "
            f"(ends {end_date}); TSM will return no hourly data for it"
        )


def test_hourly_form_keeps_recent_days_inside_cycle():
    """A per-day hourly POST for 5/31 must advertise a cycle that covers it."""
    api = _make_api()

    with patch.object(api_module, "datetime", _FrozenDatetime):
        body = api._build_tsm_form(
            Aggregation.HOURLY,
            start_datetime=datetime(2026, 5, 31),
            end_datetime=datetime(2026, 5, 31),
        )

    end_date = _in_progress_end_date(body["date_ranges"])
    assert datetime(2026, 5, 31) <= end_date, (
        f"hourly request for 5/31 advertises a cycle ending {end_date}; "
        "the day falls outside it and TSM drops the readings"
    )
