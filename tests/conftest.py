"""Fixtures for Municipal Water Usage tests.

Home Assistant's pytest plugin assumes a POSIX environment (it imports
``fcntl``) and can't load on native Windows. To keep the pure-Python unit
tests (parser, formatters, OIDC scraping) usable on Windows for local
iteration, the recorder/HA autouse fixture is only registered when the
``recorder_mock`` fixture is actually importable.

The full integration test suite (anything that touches the HA recorder)
still requires WSL / Linux / Docker — CI handles those.
"""
from __future__ import annotations

import pytest

try:
    # Just importing this module triggers ``homeassistant.runner`` which
    # depends on ``fcntl``. On Windows the import fails, so we fall back to
    # registering no autouse fixture and let the affected tests be skipped
    # or run in CI.
    import pytest_homeassistant_custom_component.plugins  # noqa: F401

    _HA_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    _HA_AVAILABLE = False


if _HA_AVAILABLE:
    @pytest.fixture(autouse=True)
    def base_recorder_fixture(recorder_mock, enable_custom_integrations):
        """Enable custom integrations and the recorder for every test."""
