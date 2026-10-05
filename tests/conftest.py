from __future__ import annotations

import pytest

MUNGCHI_ENV_VARS = (
    "MUNGCHI_MODEL",
    "MUNGCHI_STATE_FILE",
    "LOOKBACK_DAYS",
    "TIMEZONE",
    "MY_NAMES",
    "MY_EMAILS",
    "DROPBOX_ACCESS_TOKEN",
    "DROPBOX_REFRESH_TOKEN",
    "DROPBOX_APP_KEY",
    "DROPBOX_APP_SECRET",
    "DROPBOX_ROOT_FOLDER",
    "OVERLEAF_GIT_TOKEN",
    "OVERLEAF_PROJECTS",
    "OVERLEAF_CACHE_DIR",
    "CALENDAR_ICS_URLS",
    "ANTHROPIC_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_ALLOWED_USER_IDS",
    "SLACK_BRIEF_CHANNEL",
    "SLACK_MAX_CONCURRENT",
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """No real credentials leak into tests and no state is written to the repo."""
    for name in MUNGCHI_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    yield
