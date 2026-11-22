"""Shared pytest fixtures for fast, deterministic offline test runs."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass wall-clock retry backoff sleeps unless a test supplies its own sleep_fn."""
    monkeypatch.setattr("src.clients.retry._default_sleep", lambda _seconds: None)
