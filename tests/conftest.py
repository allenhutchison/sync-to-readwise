from __future__ import annotations

import os

import pytest

# Variables Settings reads. Cleared so tests start from a known empty
# environment regardless of whatever Doppler/.env happens to be loaded
# in the developer's shell.
_SETTINGS_ENV_VARS = (
    "READWISE_TOKEN",
    "YOUTUBE_OAUTH_CLIENT_ID",
    "YOUTUBE_OAUTH_CLIENT_SECRET",
    "GITHUB_TOKEN",
    "SYNCRW_LOG_LEVEL",
    "SYNCRW_DATA_DIR",
)


@pytest.fixture(autouse=True)
def _isolate_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # Block accidental .env discovery — Settings has env_file=".env" and the
    # repo root contains an .env.example. If a developer copies that to .env
    # for local runs we don't want it leaking into tests.
    monkeypatch.chdir(os.path.dirname(__file__))
