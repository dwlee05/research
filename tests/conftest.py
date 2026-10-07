from __future__ import annotations

import httpx
import pytest

from mungchi import config, version

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
    "CALENDAR_WRITE_TARGET",
    "CALENDAR_CATEGORIES",
    "CALENDAR_CATEGORY_ALIASES",
    "DEFAULT_EVENT_MINUTES",
    "WHISPER_MODEL",
    "WHISPER_LANGUAGE",
    "VOICE_MAX_SECONDS",
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
    "CREDITS_API_BASE",
    "CREDIT_ALERT_PERCENT",
    "BRIEF_TIME",
    "BRIEF_DAYS",
    "BRIEF_CATCHUP_UNTIL",
    "BRIEF_WEATHER",
    "WEATHER_LABEL",
    "WEATHER_LAT",
    "WEATHER_LON",
)


def _no_network(self, request):
    raise httpx.ConnectError("tests never use the network", request=request)


async def _no_network_async(self, request):
    raise httpx.ConnectError("tests never use the network", request=request)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """No real credentials leak into tests and no state is written to the repo.

    Tests also behave the same on a Mac: unless a test passes ``platform``
    itself, the platform is not macOS, so the real Calendar app (EventKit) is
    never touched.

    The briefing's weather line (on by default) is off here so the briefing
    tests see the header, the answer and the credits only; the weather tests
    turn it on themselves. A real httpx request, sync or async (one not given
    a ``MockTransport``), fails as if offline instead of reaching the internet.
    git is never run either: the running-version lookup sees no git (the
    package version) unless a test gives it a fake git.
    """
    for name in MUNGCHI_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BRIEF_WEATHER", "off")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _no_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _no_network_async)
    monkeypatch.setattr(version, "run_git", lambda args, **kwargs: None)
    monkeypatch.setattr(config, "current_platform", lambda: "linux")
    monkeypatch.chdir(tmp_path)
    yield
