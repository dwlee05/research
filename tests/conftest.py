from __future__ import annotations

import pytest

from mungchi import config

MUNGCHI_ENV_VARS = (
    "MUNGCHI_MODEL",
    "MUNGCHI_STATE_FILE",
    "LOOKBACK_DAYS",
    "TIMEZONE",
    "DROPBOX_ACCESS_TOKEN",
    "DROPBOX_REFRESH_TOKEN",
    "DROPBOX_APP_KEY",
    "DROPBOX_APP_SECRET",
    "DROPBOX_ROOT_FOLDER",
    "CALENDAR_ICS_URLS",
    "CALENDAR_SOURCE",
    "MACOS_CALENDARS",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_UPDATE_BOT_TOKEN",
    "SLACK_UPDATE_APP_TOKEN",
    "SLACK_SCHEDULE_BOT_TOKEN",
    "SLACK_SCHEDULE_APP_TOKEN",
    "SLACK_ALLOWED_USER_IDS",
    "SLACK_BRIEF_CHANNEL",
    "SLACK_MAX_CONCURRENT",
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """No real credentials leak into tests and no state is written to the repo.

    Tests also behave the same on a Mac: unless a test passes ``platform``
    itself, the platform is not macOS, so the real Calendar app (EventKit) is
    never touched.
    """
    for name in MUNGCHI_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "current_platform", lambda: "linux")
    monkeypatch.chdir(tmp_path)
    yield
