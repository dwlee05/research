"""Environment-driven configuration.

Every accessor reads ``os.environ`` (or an injected mapping) at call time so
that tests can override values without reloading modules.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_TIMEZONE = "Asia/Seoul"
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_STATE_FILE = ".mungchi_state.json"
SLACK_THREADS_FILE = ".mungchi_slack_threads.json"
DEFAULT_OVERLEAF_CACHE = Path("~/.cache/mungchi/overleaf")
DEFAULT_SLACK_MAX_CONCURRENT = 2

# Overleaf project ids are hex strings; be a little lenient but never allow
# characters that could escape the URL path or the cache directory.
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _get(env: Mapping[str, str] | None, key: str) -> str:
    return (_env(env).get(key) or "").strip()


def split_csv(value: str | None) -> list[str]:
    """Split a comma-separated env value, dropping blanks."""
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def get_model(env: Mapping[str, str] | None = None) -> str:
    return _get(env, "MUNGCHI_MODEL") or DEFAULT_MODEL


def get_timezone_name(env: Mapping[str, str] | None = None) -> str:
    return _get(env, "TIMEZONE") or DEFAULT_TIMEZONE


def get_timezone(env: Mapping[str, str] | None = None) -> ZoneInfo:
    name = get_timezone_name(env)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def get_lookback_days(env: Mapping[str, str] | None = None) -> int:
    raw = _get(env, "LOOKBACK_DAYS")
    try:
        days = int(raw) if raw else DEFAULT_LOOKBACK_DAYS
    except ValueError:
        days = DEFAULT_LOOKBACK_DAYS
    return days if days > 0 else DEFAULT_LOOKBACK_DAYS


def get_state_path(env: Mapping[str, str] | None = None) -> Path:
    raw = _get(env, "MUNGCHI_STATE_FILE")
    return Path(raw).expanduser() if raw else Path.cwd() / DEFAULT_STATE_FILE


def get_slack_threads_path(env: Mapping[str, str] | None = None) -> Path:
    """Slack thread -> session map, stored next to the state file."""
    return get_state_path(env).with_name(SLACK_THREADS_FILE)


def get_my_names(env: Mapping[str, str] | None = None) -> list[str]:
    return split_csv(_get(env, "MY_NAMES"))


def get_my_emails(env: Mapping[str, str] | None = None) -> list[str]:
    return split_csv(_get(env, "MY_EMAILS"))


# ---------------------------------------------------------------- Dropbox


@dataclass
class DropboxConfig:
    access_token: str = ""
    refresh_token: str = ""
    app_key: str = ""
    app_secret: str = ""
    root_folder: str = ""
    missing: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return not self.missing


def load_dropbox_config(env: Mapping[str, str] | None = None) -> DropboxConfig:
    cfg = DropboxConfig(
        access_token=_get(env, "DROPBOX_ACCESS_TOKEN"),
        refresh_token=_get(env, "DROPBOX_REFRESH_TOKEN"),
        app_key=_get(env, "DROPBOX_APP_KEY"),
        app_secret=_get(env, "DROPBOX_APP_SECRET"),
        root_folder=_get(env, "DROPBOX_ROOT_FOLDER"),
    )
    if not cfg.access_token:
        trio = {
            "DROPBOX_REFRESH_TOKEN": cfg.refresh_token,
            "DROPBOX_APP_KEY": cfg.app_key,
            "DROPBOX_APP_SECRET": cfg.app_secret,
        }
        if not any(trio.values()):
            # Neither auth style is configured: the simplest one is reported.
            cfg.missing.append("DROPBOX_ACCESS_TOKEN")
        else:
            cfg.missing.extend(name for name, value in trio.items() if not value)
    if not cfg.root_folder:
        cfg.missing.append("DROPBOX_ROOT_FOLDER")
    return cfg


def dropbox_hint(missing: list[str]) -> str:
    names = ", ".join(missing)
    return (
        f"Dropbox 설정 누락: {names} — dropbox.com/developers/apps에서 앱을 만들고 "
        "(권한: files.metadata.read, files.content.read, sharing.read, account_info.read) "
        "토큰을 발급해 .env에 넣으세요 (DROPBOX_ACCESS_TOKEN 하나 또는 "
        "DROPBOX_REFRESH_TOKEN+DROPBOX_APP_KEY+DROPBOX_APP_SECRET, 그리고 DROPBOX_ROOT_FOLDER)."
    )


# ---------------------------------------------------------------- Overleaf


@dataclass
class OverleafProject:
    name: str
    project_id: str


@dataclass
class OverleafConfig:
    token: str = ""
    projects: list[OverleafProject] = field(default_factory=list)
    invalid_entries: list[str] = field(default_factory=list)
    my_names: list[str] = field(default_factory=list)
    my_emails: list[str] = field(default_factory=list)
    cache_dir: Path = DEFAULT_OVERLEAF_CACHE
    missing: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return not self.missing


def parse_overleaf_projects(raw: str) -> tuple[list[OverleafProject], list[str]]:
    """Parse ``name=project_id`` or bare ``project_id`` entries.

    Returns ``(projects, invalid_entries)``.
    """
    projects: list[OverleafProject] = []
    invalid: list[str] = []
    for entry in split_csv(raw):
        if "=" in entry:
            name, _, pid = entry.partition("=")
            name, pid = name.strip(), pid.strip()
        else:
            name, pid = entry, entry
        if not _PROJECT_ID_RE.match(pid):
            invalid.append(entry)
            continue
        projects.append(OverleafProject(name=name or pid, project_id=pid))
    return projects, invalid


def load_overleaf_config(env: Mapping[str, str] | None = None) -> OverleafConfig:
    projects, invalid = parse_overleaf_projects(_get(env, "OVERLEAF_PROJECTS"))
    cache_raw = _get(env, "OVERLEAF_CACHE_DIR")
    cfg = OverleafConfig(
        token=_get(env, "OVERLEAF_GIT_TOKEN"),
        projects=projects,
        invalid_entries=invalid,
        my_names=get_my_names(env),
        my_emails=get_my_emails(env),
        cache_dir=(Path(cache_raw) if cache_raw else DEFAULT_OVERLEAF_CACHE).expanduser(),
    )
    if not cfg.token:
        cfg.missing.append("OVERLEAF_GIT_TOKEN")
    if not cfg.projects:
        cfg.missing.append("OVERLEAF_PROJECTS")
    if not cfg.my_names and not cfg.my_emails:
        # Without these we cannot tell the user's own commits apart, and the
        # report must never attribute the user's work to a co-author.
        cfg.missing.extend(["MY_NAMES", "MY_EMAILS"])
    return cfg


def overleaf_hint(missing: list[str]) -> str:
    names = ", ".join(missing)
    return (
        f"Overleaf 설정 누락: {names} — Overleaf 계정 설정 > Git 연동에서 토큰을 만들고 "
        "(Git 연동이 되는 유료 플랜 필요), OVERLEAF_PROJECTS에 '이름=프로젝트ID'를, "
        "MY_NAMES 또는 MY_EMAILS(둘 중 하나 이상)에 내 이름/이메일을 .env에 넣으세요."
    )


# ---------------------------------------------------------------- Calendar


@dataclass
class CalendarConfig:
    urls: list[str] = field(default_factory=list)
    timezone_name: str = DEFAULT_TIMEZONE
    missing: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return not self.missing


def load_calendar_config(env: Mapping[str, str] | None = None) -> CalendarConfig:
    cfg = CalendarConfig(
        urls=split_csv(_get(env, "CALENDAR_ICS_URLS")),
        timezone_name=get_timezone_name(env),
    )
    if not cfg.urls:
        cfg.missing.append("CALENDAR_ICS_URLS")
    return cfg


def calendar_hint(missing: list[str]) -> str:
    names = ", ".join(missing)
    return (
        f"캘린더 설정 누락: {names} — Google 캘린더 설정 > 내 캘린더의 설정 > 캘린더 통합의 "
        "'iCal 형식의 비공개 주소'(Outlook·iCloud의 ICS 주소도 가능)를 복사해 쉼표로 구분해 .env에 넣으세요."
    )


# ---------------------------------------------------------------- Slack

# Member ids start with U (or W on Enterprise Grid); channel ids with C/G,
# DM ids with D. A member id as the briefing target posts to the app's DM.
_SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{2,}$")
_SLACK_CHANNEL_ID_RE = re.compile(r"^[CGDUW][A-Z0-9]{2,}$")
SLACK_README_HINT = "설정 방법은 README의 'Slack에서 뭉치 부르기'를 보세요."


@dataclass
class SlackConfig:
    # Tokens stay out of repr() so a logged config never leaks them.
    bot_token: str = field(default="", repr=False)
    app_token: str = field(default="", repr=False)
    allowed_user_ids: frozenset[str] = frozenset()
    invalid_user_ids: list[str] = field(default_factory=list)
    brief_channel: str = ""
    max_concurrent: int = DEFAULT_SLACK_MAX_CONCURRENT


def load_slack_config(env: Mapping[str, str] | None = None) -> SlackConfig:
    allowed: set[str] = set()
    invalid: list[str] = []
    for entry in split_csv(_get(env, "SLACK_ALLOWED_USER_IDS")):
        if _SLACK_USER_ID_RE.match(entry):
            allowed.add(entry)
        else:
            invalid.append(entry)
    raw_max = _get(env, "SLACK_MAX_CONCURRENT")
    try:
        max_concurrent = int(raw_max) if raw_max else DEFAULT_SLACK_MAX_CONCURRENT
    except ValueError:
        max_concurrent = DEFAULT_SLACK_MAX_CONCURRENT
    return SlackConfig(
        bot_token=_get(env, "SLACK_BOT_TOKEN"),
        app_token=_get(env, "SLACK_APP_TOKEN"),
        allowed_user_ids=frozenset(allowed),
        invalid_user_ids=invalid,
        brief_channel=_get(env, "SLACK_BRIEF_CHANNEL"),
        max_concurrent=max_concurrent if max_concurrent > 0 else DEFAULT_SLACK_MAX_CONCURRENT,
    )


def _bot_token_problems(cfg: SlackConfig) -> list[str]:
    if cfg.bot_token and not cfg.bot_token.startswith("xoxb-"):
        swapped = " (xapp-로 시작하는 토큰은 SLACK_APP_TOKEN에 넣으세요)" if cfg.bot_token.startswith("xapp-") else ""
        return [f"SLACK_BOT_TOKEN 값은 xoxb-로 시작하는 Bot User OAuth Token이어야 합니다{swapped}."]
    return []


def slack_bot_problems(cfg: SlackConfig) -> list[str]:
    """Korean problem lines that keep ``python -m mungchi slack`` from starting.

    An empty allow-list is a hard error: 뭉치 reads private Dropbox, Overleaf
    and calendar data and must never answer anyone but its owner.
    """
    missing = [
        name
        for name, value in (
            ("SLACK_BOT_TOKEN", cfg.bot_token),
            ("SLACK_APP_TOKEN", cfg.app_token),
            ("SLACK_ALLOWED_USER_IDS", cfg.allowed_user_ids or cfg.invalid_user_ids),
        )
        if not value
    ]
    problems = [f"빠진 환경변수: {', '.join(missing)}"] if missing else []
    problems += _bot_token_problems(cfg)
    if cfg.app_token and not cfg.app_token.startswith("xapp-"):
        swapped = " (xoxb-로 시작하는 토큰은 SLACK_BOT_TOKEN에 넣으세요)" if cfg.app_token.startswith("xoxb-") else ""
        problems.append(
            f"SLACK_APP_TOKEN 값은 xapp-로 시작하는 App-Level Token(connections:write 권한)이어야 합니다{swapped}."
        )
    if cfg.invalid_user_ids:
        problems.append(
            "SLACK_ALLOWED_USER_IDS에 멤버 ID가 아닌 값이 있습니다: "
            f"{', '.join(cfg.invalid_user_ids)} — 멤버 ID는 U로 시작하는 영문 대문자·숫자입니다 "
            "(Slack 프로필 → ⋮ → 멤버 ID 복사)."
        )
    if not cfg.allowed_user_ids:
        problems.append(
            "SLACK_ALLOWED_USER_IDS가 비어 있어 봇을 시작하지 않습니다. 뭉치는 Dropbox·Overleaf·캘린더의 "
            "개인 정보를 읽기 때문에, 답해도 되는 사람(보통 나 혼자)의 멤버 ID를 쉼표로 구분해 넣어야 합니다."
        )
    return problems


def slack_brief_problems(cfg: SlackConfig) -> list[str]:
    """Korean problem lines that keep ``--brief --slack`` from posting."""
    missing = [
        name
        for name, value in (("SLACK_BOT_TOKEN", cfg.bot_token), ("SLACK_BRIEF_CHANNEL", cfg.brief_channel))
        if not value
    ]
    problems = [f"빠진 환경변수: {', '.join(missing)}"] if missing else []
    problems += _bot_token_problems(cfg)
    if cfg.brief_channel and not _SLACK_CHANNEL_ID_RE.match(cfg.brief_channel):
        problems.append(
            "SLACK_BRIEF_CHANNEL 값은 채널 이름(#general)이 아니라 채널 ID(C로 시작하는 영문 대문자·숫자)여야 합니다."
        )
    return problems


# ---------------------------------------------------------------- Secrets


SECRET_ENV_VARS = (
    "DROPBOX_ACCESS_TOKEN",
    "DROPBOX_REFRESH_TOKEN",
    "DROPBOX_APP_SECRET",
    "DROPBOX_APP_KEY",
    "OVERLEAF_GIT_TOKEN",
    "ANTHROPIC_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
)


def secret_values(env: Mapping[str, str] | None = None) -> list[str]:
    """Every configured secret string that must never appear in output."""
    import base64

    values: list[str] = []
    for key in SECRET_ENV_VARS:
        value = _get(env, key)
        if value:
            values.append(value)
    token = _get(env, "OVERLEAF_GIT_TOKEN")
    if token:
        values.append(base64.b64encode(f"git:{token}".encode()).decode())
    # Private ICS addresses embed a secret key in the URL itself.
    for url in split_csv(_get(env, "CALENDAR_ICS_URLS")):
        values.append(url)
        if url.lower().startswith("webcal://"):
            values.append("https://" + url[len("webcal://"):])
    return values
