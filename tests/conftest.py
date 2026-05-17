"""Fixtures for Municipal Water Usage tests."""
import pytest


@pytest.fixture(autouse=True)
def base_recorder_fixture(recorder_mock, enable_custom_integrations):
    """Enable custom integrations and the recorder for every test."""
