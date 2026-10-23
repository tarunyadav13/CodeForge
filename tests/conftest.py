"""Test-wide settings.

The UI has no default API URL (a hosted UI must never fall back to localhost), so tests that render the UI or call the
client against a mocked session get a placeholder one. Skipped when M3_LIVE_API is set so live runs keep the real URL.
"""
import os

import pytest


@pytest.fixture(autouse=True)
def _placeholder_api_base_url(monkeypatch):
    if not os.environ.get("M3_LIVE_API"):
        monkeypatch.setenv("API_BASE_URL", "http://api.test")
    yield
