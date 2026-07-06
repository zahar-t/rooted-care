"""Shared pytest fixtures. No test in this suite makes a live LLM or network call.

The adapter imports ``autopilot`` straight from this repository's ``slice/``
(see settings.SLICE_DIR), so there is no vendoring step to bootstrap.
"""

import json

import pytest

from service import adapter, settings
from service.tests import fixtures


@pytest.fixture
def data_dir(tmp_path):
    """Redirect all service + brain writes into a fresh tmp dir for this test."""
    adapter.configure_data_dir(tmp_path)
    return tmp_path


@pytest.fixture
def fake_llm(monkeypatch):
    """Patch the frozen brain's call_claude with a deterministic marker dispatcher."""

    def fake(system: str, user: str) -> str:
        if system.startswith("You are the intake triage"):
            for marker, record in fixtures.TRIAGE_BY_MARKER.items():
                if marker in user:
                    return json.dumps(record)
            raise AssertionError(f"fake_llm: no triage marker in message: {user!r}")
        # drafting: a specific failing draft if the body asks for one, else a clean reply
        for marker, draft in fixtures.DRAFT_BY_MARKER.items():
            if marker in user:
                return draft
        return fixtures.GOOD_DRAFT

    monkeypatch.setattr(adapter.autopilot, "call_claude", fake)
    return fake


@pytest.fixture
def api_key(monkeypatch):
    key = "test-secret-key-0123456789"
    monkeypatch.setattr(settings, "ROOTED_API_KEY", key)
    return key


@pytest.fixture
def client(data_dir, fake_llm, api_key):
    """FastAPI TestClient wired to the tmp data dir + mocked LLM + known API key."""
    from fastapi.testclient import TestClient
    from service import app as app_module

    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def auth(api_key):
    """Header dict carrying a valid API key."""
    return {"X-Api-Key": api_key}
