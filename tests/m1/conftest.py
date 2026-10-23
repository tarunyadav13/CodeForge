"""Member 1 test configuration.

Every test here runs OFFLINE by default: any attempt to open a network connection fails
the test. Tests marked ``live`` (real OpenAI calls) are the only exception and are
skipped unless RUN_LIVE_LLM=1 and OPENAI_API_KEY are set.
"""
import socket

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "live: makes real OpenAI API calls (skipped by default)")


@pytest.fixture(autouse=True)
def _block_network(request, monkeypatch):
    if request.node.get_closest_marker("live"):
        return

    def _refuse(self, *args, **kwargs):
        raise AssertionError("Network access attempted in an offline unit test.")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
