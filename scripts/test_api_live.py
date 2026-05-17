#!/usr/bin/env python3
"""Live end-to-end smoke runner for ``MunicipalWaterAPI``.

This script drives the real portal using the credentials in your local
``.env`` file (see ``.env.example``). It never touches Home Assistant, so
it works on Windows / macOS / Linux without WSL.

Stages run by default:

1. **login** — full OpenID Connect dance.
2. **chart-context** — fetch the consumption page and extract the JWT +
   meter metadata.
3. **fetch-hourly** — POST to Tyler Smart Meters for the last N days of
   hourly data (``MWU_HOURLY_DAYS``, default 2).
4. **fetch-daily** — POST to Tyler Smart Meters for the last N days of
   daily data (``MWU_DAILY_DAYS``, default 14).

You can run a subset with ``--steps``::

    python scripts/test_api_live.py --steps login,chart-context
    python scripts/test_api_live.py --steps fetch-hourly

Output is pretty-printed in tables. Add ``-v`` for DEBUG-level logging
from the API client (login redirects, retries, etc.).
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import os
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

# --------------------------------------------------------------------------- #
# Bootstrap: load the integration's api.py without dragging Home Assistant in.
# --------------------------------------------------------------------------- #


REPO_ROOT = Path(__file__).resolve().parent.parent


def _bootstrap_api_module() -> Any:
    """Import ``municipal_water_usage.api`` as a standalone subpackage.

    The integration package ``__init__.py`` pulls in ``homeassistant.*``
    which only works in a real HA environment. For the live smoke test we
    only need the API client, so we hand-load the four modules that have
    no HA dependency.
    """
    pkg_dir = REPO_ROOT / "custom_components" / "municipal_water_usage"

    pkg = types.ModuleType("municipal_water_usage")
    pkg.__path__ = [str(pkg_dir)]
    sys.modules["municipal_water_usage"] = pkg

    for submodule in ("const", "exceptions", "utils", "api"):
        spec = importlib.util.spec_from_file_location(
            f"municipal_water_usage.{submodule}",
            pkg_dir / f"{submodule}.py",
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"municipal_water_usage.{submodule}"] = module
        spec.loader.exec_module(module)

    from municipal_water_usage import api as api_module  # type: ignore

    return api_module


api = _bootstrap_api_module()
Aggregation = api.Aggregation
MunicipalWaterAPI = api.MunicipalWaterAPI


# --------------------------------------------------------------------------- #
# Config loading
# --------------------------------------------------------------------------- #


def _load_env() -> None:
    """Load ``.env`` from the repo root if python-dotenv is available."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        print(
            f"ERROR: environment variable {name} is required. "
            "Copy .env.example to .env and fill in your portal credentials.",
            file=sys.stderr,
        )
        sys.exit(2)
    return value


# --------------------------------------------------------------------------- #
# Pretty printing
# --------------------------------------------------------------------------- #


def _hr(title: str) -> None:
    print()
    print("=" * 78)
    print(f" {title}")
    print("=" * 78)


def _print_readings(readings: List[Dict[str, Any]], limit: int = 24) -> None:
    if not readings:
        print("  (no readings returned)")
        return
    total = sum(r["consumption"] for r in readings)
    print(f"  Total readings: {len(readings)}")
    print(f"  Sum of consumption: {total:,.2f} gal")
    print(f"  Earliest:           {readings[0]['reading_time'].isoformat()}")
    print(f"  Latest:             {readings[-1]['reading_time'].isoformat()}")
    print()
    print("  First sample:")
    for reading in readings[: min(limit, len(readings))]:
        ts = reading["reading_time"].strftime("%Y-%m-%d %H:%M %Z")
        print(f"    {ts}   {reading['consumption']:>9,.2f} gal")
    if len(readings) > limit:
        print(f"    ... ({len(readings) - limit} more, use --full to see all)")


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #


async def step_login(client: "MunicipalWaterAPI") -> None:
    _hr("Step 1: OIDC login")
    print(f"  Host:    {client.host}")
    print(f"  Email:   {client.email}")
    await client.async_login()
    print("  Login: OK (session cookies issued)")


async def step_chart_context(client: "MunicipalWaterAPI") -> None:
    _hr("Step 2: Fetch consumption page + extract JWT")
    print(f"  Account: {client.account_id}")
    try:
        await client.async_get_chart_context()
    except Exception as err:
        # Always dump the page so we can see what came back.
        await _dump_consumption_page(client, error=err)
        raise
    print(f"  JWT (first 32 chars): {(client._jwt or '')[:32]}...")
    if client._jwt_exp:
        exp_dt = datetime.fromtimestamp(client._jwt_exp)
        print(f"  JWT expires:          {exp_dt.isoformat()}")
    print(f"  Meter info raw:       {client._meter_info_json}")
    print(f"  Chart info raw:       {client._chart_info_json}")
    print(f"  Account start date:   {client._account_start_date}")
    print(f"  Meter name:           {client._meter_name}")


async def _dump_consumption_page(
    client: "MunicipalWaterAPI",
    error: Exception | None = None,
) -> None:
    """Diagnostic: re-fetch the consumption page and dump its body to disk."""
    session = await client._get_session()
    url = (
        f"https://{client.host}/bastroptx/utilities/accounts/consumption/"
        f"{client.account_id}"
    )
    dump_path = REPO_ROOT / ".debug_consumption.html"
    headers_path = REPO_ROOT / ".debug_consumption.headers.txt"

    print()
    print("-" * 78)
    print(" Diagnostic dump")
    print("-" * 78)
    if error:
        print(f"  Error: {type(error).__name__}: {error}")
    print(f"  Re-fetching: {url}")

    try:
        async with session.get(url, allow_redirects=True) as response:
            body = await response.text()
            final_url = str(response.url)
            status = response.status
            content_type = response.headers.get("Content-Type", "?")
            header_lines = [f"{k}: {v}" for k, v in response.headers.items()]
    except Exception as fetch_err:  # noqa: BLE001
        print(f"  Diagnostic fetch itself failed: {fetch_err}")
        return

    dump_path.write_text(body, encoding="utf-8")
    headers_path.write_text(
        "Status: {}\nFinal URL: {}\n\n{}\n".format(
            status, final_url, "\n".join(header_lines)
        ),
        encoding="utf-8",
    )

    print(f"  HTTP {status}")
    print(f"  Final URL: {final_url}")
    print(f"  Content-Type: {content_type}")
    print(f"  Body length: {len(body):,} chars")
    print(f"  Cookies in jar: {len(session.cookie_jar)}")
    cookie_names = sorted({c.key for c in session.cookie_jar})
    print(f"  Cookie names: {cookie_names}")
    print(f"  Wrote response body to {dump_path}")
    print(f"  Wrote response headers to {headers_path}")
    print()
    print("  Heuristics:")
    lower = body.lower()
    print(f"    'tylersmartmeters'    in body: {'tylersmartmeters' in lower}")
    print(f"    'data-token'          in body: {'data-token' in lower}")
    print(f"    'charts.js'           in body: {'charts.js' in lower}")
    print(f"    '__requestverification' in body (login form): "
          f"{'__requestverification' in lower}")
    print(f"    'name=\"password\"'   in body (login form): "
          f"{'name=\"password\"' in lower}")
    print(f"    'account selection'   in body: {'account selection' in lower}")
    print(f"    '/login' redirect URL: {'/login' in final_url.lower()}")
    print()
    # Show the body where the chart script SHOULD be.
    needle_pos = lower.find("consumption")
    if needle_pos == -1:
        needle_pos = lower.find("<body")
    if needle_pos == -1:
        needle_pos = 0
    start = max(0, needle_pos - 200)
    end = min(len(body), needle_pos + 1500)
    print("  --- Snippet around interesting content ---")
    print(body[start:end])
    print("  --- end snippet ---")


async def step_fetch(
    client: "MunicipalWaterAPI",
    aggregation: "Aggregation",
    days: int,
    full: bool,
) -> None:
    label = aggregation.label
    _hr(f"Step 3/4: Fetch {label.lower()} usage (last {days} days)")
    start = datetime.now() - timedelta(days=days)
    print(f"  Interval:   {aggregation.interval_type}")
    print(f"  Start date: {start.isoformat()}")
    data = await client.async_get_usage(
        aggregation=aggregation,
        start_datetime=start,
    )
    readings = data.get("USAGE", [])
    if not readings:
        await _dump_tsm_response(client, aggregation, start)
    _print_readings(readings, limit=10_000 if full else 24)


async def _dump_tsm_response(
    client: "MunicipalWaterAPI",
    aggregation: "Aggregation",
    start_datetime: datetime,
) -> None:
    """Re-POST the same request with the same body and dump the HTML response.

    Useful when ``async_get_usage`` returns zero readings, which usually means
    either (a) the server returned an error page or (b) our regex is missing
    a real ``series0.push`` shape.
    """
    print()
    print("-" * 78)
    print(" Diagnostic dump: TSM returned 0 parseable readings")
    print("-" * 78)

    end_datetime = datetime.now().replace(minute=0, second=0, microsecond=0)
    try:
        body = client._build_tsm_form(aggregation, start_datetime, end_datetime)
    except Exception as err:  # noqa: BLE001
        print(f"  Could not build TSM form body: {err}")
        return

    session = await client._get_session()
    try:
        async with session.post(
            "https://www.tylersmartmeters.com/",
            data=body,
            headers={
                "Origin": "https://www.tylersmartmeters.com",
                "Referer": "https://www.tylersmartmeters.com/",
            },
        ) as response:
            html = await response.text()
            status = response.status
            content_type = response.headers.get("Content-Type", "?")
            final_url = str(response.url)
    except Exception as fetch_err:  # noqa: BLE001
        print(f"  Diagnostic POST itself failed: {fetch_err}")
        return

    dump_path = REPO_ROOT / f".debug_tsm_{aggregation.interval_type}.html"
    body_path = REPO_ROOT / f".debug_tsm_{aggregation.interval_type}.form.txt"
    dump_path.write_text(html, encoding="utf-8")
    body_path.write_text(
        "\n".join(f"{k}: {v}" for k, v in body.items()),
        encoding="utf-8",
    )

    print(f"  HTTP {status}")
    print(f"  Final URL: {final_url}")
    print(f"  Content-Type: {content_type}")
    print(f"  Body length: {len(html):,} chars")
    print(f"  Wrote response to {dump_path}")
    print(f"  Wrote sent form fields to {body_path}")

    lower = html.lower()
    print()
    print("  Heuristics:")
    print(f"    'series0.push'    in body: {'series0.push' in lower}")
    print(f"    'series0'         in body: {'series0' in lower}")
    print(f"    'error'           in body: {'error' in lower}")
    print(f"    'invalid'         in body: {'invalid' in lower}")
    print(f"    'relog'           in body: {'relog' in lower}")
    print(f"    '<form'           in body: {'<form' in lower}")
    print(f"    '__validation'    in body: {'__validation' in lower}")

    # Echo the first chunk that mentions "series" or "error", whichever we find.
    needle_idx = -1
    for needle in ("series0", "error", "invalid", "exception", "required"):
        needle_idx = lower.find(needle)
        if needle_idx != -1:
            print(f"\n  Snippet around first '{needle}':")
            break
    if needle_idx == -1:
        print("\n  First 1500 chars of response:")
        needle_idx = 0
    start_c = max(0, needle_idx - 300)
    end_c = min(len(html), needle_idx + 1500)
    print(html[start_c:end_c])
    print("  --- end snippet ---")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


STEPS = ("login", "chart-context", "fetch-hourly", "fetch-daily")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--steps",
        default=",".join(STEPS),
        help=(
            "Comma-separated list of steps to run. "
            f"Default: {','.join(STEPS)}"
        ),
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Print every reading instead of just the first 24.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging from the API client.",
    )
    return parser.parse_args()


async def main_async(args: argparse.Namespace) -> int:
    requested = [s.strip() for s in args.steps.split(",") if s.strip()]
    unknown = set(requested) - set(STEPS)
    if unknown:
        print(f"ERROR: unknown steps: {sorted(unknown)}", file=sys.stderr)
        print(f"       valid steps: {STEPS}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # Quiet third-party noise.
    logging.getLogger("asyncio").setLevel(logging.INFO)

    _load_env()
    email = _require_env("MWU_EMAIL")
    password = _require_env("MWU_PASSWORD")
    account_id = _require_env("MWU_ACCOUNT_ID")
    host = _require_env("MWU_HOST")
    tz = os.getenv("MWU_TIMEZONE", "America/Chicago")
    hourly_days = int(os.getenv("MWU_HOURLY_DAYS", "2"))
    daily_days = int(os.getenv("MWU_DAILY_DAYS", "14"))

    client = MunicipalWaterAPI(
        email=email,
        password=password,
        account_id=account_id,
        host=host,
        timezone=tz,
    )

    try:
        # Every later step depends on the earlier ones, so when a step is
        # requested we transparently run its prerequisites too.
        need_login = any(
            s in requested for s in ("login", "chart-context", "fetch-hourly", "fetch-daily")
        )
        need_ctx = any(
            s in requested for s in ("chart-context", "fetch-hourly", "fetch-daily")
        )

        if need_login:
            await step_login(client)
        if need_ctx:
            await step_chart_context(client)
        if "fetch-hourly" in requested:
            await step_fetch(client, Aggregation.HOURLY, hourly_days, args.full)
        if "fetch-daily" in requested:
            await step_fetch(client, Aggregation.DAILY, daily_days, args.full)

        _hr("All requested steps completed successfully")
        return 0

    except Exception as err:  # pragma: no cover - top-level error reporter
        print()
        print("!" * 78)
        print(f" FAILED: {type(err).__name__}: {err}")
        print("!" * 78)
        if args.verbose:
            raise
        print("Re-run with -v to see a full traceback.")
        return 1
    finally:
        await client.close()


def main() -> int:
    args = _parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
