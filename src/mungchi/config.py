"""Environment-driven configuration.

Every accessor reads ``os.environ`` (or an injected mapping) at call time so
that tests can override values without reloading modules.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from datetime import time, tzinfo
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .personas import MUNGCHI, PERSONA_LABELS, PERSONAS, SCHEDULE, SLACK_HANDLES, UPDATE

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_TIMEZONE = "Asia/Seoul"
# Only used by a briefing without a stored checkpoint (the first one): a daily
# briefing starts with the last 24 hours.
DEFAULT_LOOKBACK_DAYS = 1
DEFAULT_STATE_FILE = ".mungchi_state.json"
SLACK_THREADS_FILE = ".mungchi_slack_threads.json"
DEFAULT_SLACK_MAX_CONCURRENT = 2
# Dropbox folder checked when DROPBOX_ROOT_FOLDER is unset or empty.
DEFAULT_DROPBOX_ROOT_FOLDER = "/20_연구-진행"


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


# ---------------------------------------------------------------- Claude auth

DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"

# Claude settings the bundled Claude Code CLI reads from the environment it
# inherits. Empty ones (e.g. ``ANTHROPIC_API_KEY=`` copied from .env.example)
# are removed before the CLI starts; see ``main.drop_empty_claude_env``.
CLAUDE_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)

# One-line hints appended to Claude errors (run errors and --list-models).
MODEL_HINT = "→ .env의 MUNGCHI_MODEL과 ANTHROPIC_BASE_URL을 확인하세요 (모델 목록: python -m mungchi --list-models)"
AUTH_HINT = (
    "→ .env의 ANTHROPIC_API_KEY(Anthropic 키) 또는 ANTHROPIC_AUTH_TOKEN(게이트웨이 키)을 확인하세요. "
    "게이트웨이를 쓸 땐 ANTHROPIC_API_KEY를 비우세요."
)


def get_anthropic_base_url(env: Mapping[str, str] | None = None) -> str:
    """API root without a trailing slash (``ANTHROPIC_BASE_URL`` or Anthropic's API)."""
    return (_get(env, "ANTHROPIC_BASE_URL") or DEFAULT_ANTHROPIC_BASE_URL).rstrip("/")


def get_anthropic_api_key(env: Mapping[str, str] | None = None) -> str:
    return _get(env, "ANTHROPIC_API_KEY")


def get_anthropic_auth_token(env: Mapping[str, str] | None = None) -> str:
    """Gateway key (sent as ``Authorization: Bearer``)."""
    return _get(env, "ANTHROPIC_AUTH_TOKEN")


# ---------------------------------------------------------------- Chat KHU credits

# Below this share of the total credits left, the running Slack bots DM the
# owner once per renewal period. Unset: this default; empty or 0: off.
DEFAULT_CREDIT_ALERT_PERCENT = 10.0


def get_credits_api_base(env: Mapping[str, str] | None = None) -> str:
    """``CREDITS_API_BASE`` without a trailing slash ("" when unset)."""
    return _get(env, "CREDITS_API_BASE").rstrip("/")


def get_credit_alert_percent(env: Mapping[str, str] | None = None) -> float:
    """``CREDIT_ALERT_PERCENT`` as a number from 0 (off) to 100.

    Unset means the default (10); an empty value or 0 turns the alert off.
    A value that is not a number keeps the default rather than silently
    switching the alert off. A trailing ``%`` is accepted.
    """
    raw = _env(env).get("CREDIT_ALERT_PERCENT")
    if raw is None:
        return DEFAULT_CREDIT_ALERT_PERCENT
    raw = raw.strip().rstrip("%").strip()
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_CREDIT_ALERT_PERCENT
    if value != value:  # NaN
        return DEFAULT_CREDIT_ALERT_PERCENT
    return min(max(value, 0.0), 100.0)


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


def get_state_path(env: Mapping[str, str] | None = None, base_dir: Path | None = None) -> Path:
    """``MUNGCHI_STATE_FILE``, else ``.mungchi_state.json`` in the working directory.

    ``base_dir`` stands in for the working directory (e.g. the repository the
    background service runs in, when ``service status`` is run elsewhere).
    """
    raw = _get(env, "MUNGCHI_STATE_FILE")
    base = base_dir if base_dir is not None else Path.cwd()
    if not raw:
        return base / DEFAULT_STATE_FILE
    path = Path(raw).expanduser()
    return path if path.is_absolute() or base_dir is None else base_dir / path


def get_slack_threads_path(env: Mapping[str, str] | None = None) -> Path:
    """Slack thread -> session map, stored next to the state file."""
    return get_state_path(env).with_name(SLACK_THREADS_FILE)


# ---------------------------------------------------------------- Dropbox


@dataclass
class DropboxConfig:
    access_token: str = ""
    refresh_token: str = ""
    app_key: str = ""
    app_secret: str = ""
    root_folder: str = DEFAULT_DROPBOX_ROOT_FOLDER
    missing: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        """Only auth is required; the folder falls back to the default."""
        return not self.missing


def load_dropbox_config(env: Mapping[str, str] | None = None) -> DropboxConfig:
    cfg = DropboxConfig(
        access_token=_get(env, "DROPBOX_ACCESS_TOKEN"),
        refresh_token=_get(env, "DROPBOX_REFRESH_TOKEN"),
        app_key=_get(env, "DROPBOX_APP_KEY"),
        app_secret=_get(env, "DROPBOX_APP_SECRET"),
        root_folder=_get(env, "DROPBOX_ROOT_FOLDER") or DEFAULT_DROPBOX_ROOT_FOLDER,
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
    return cfg


def dropbox_hint(missing: list[str]) -> str:
    names = ", ".join(missing)
    return (
        f"Dropbox 설정 누락: {names} — dropbox.com/developers/apps에서 앱을 만들고 "
        "(권한: files.metadata.read, sharing.read, account_info.read) "
        "토큰을 발급해 .env에 넣으세요 (DROPBOX_ACCESS_TOKEN 하나 또는 "
        "DROPBOX_REFRESH_TOKEN+DROPBOX_APP_KEY+DROPBOX_APP_SECRET). "
        f"확인할 폴더는 DROPBOX_ROOT_FOLDER(기본 {DEFAULT_DROPBOX_ROOT_FOLDER})입니다."
    )


# ---------------------------------------------------------------- Calendar

# CALENDAR_SOURCE values. "auto": the ICS addresses if CALENDAR_ICS_URLS is
# set, otherwise the macOS Calendar app when running on a Mac.
CALENDAR_SOURCE_AUTO = "auto"
CALENDAR_SOURCE_MACOS = "macos"
CALENDAR_SOURCE_ICS = "ics"
CALENDAR_SOURCES = (CALENDAR_SOURCE_AUTO, CALENDAR_SOURCE_MACOS, CALENDAR_SOURCE_ICS)
CALENDAR_SETUP_COMMAND = "python -m mungchi --calendar-setup"


def current_platform() -> str:
    """``sys.platform`` ("darwin" on a Mac); tests replace this function."""
    return sys.platform


@dataclass
class CalendarConfig:
    urls: list[str] = field(default_factory=list)
    timezone_name: str = DEFAULT_TIMEZONE
    # CALENDAR_SOURCE as written (lower-cased) and the source it resolves to
    # here: "ics", "macos", or "" when no calendar can be read.
    requested_source: str = CALENDAR_SOURCE_AUTO
    source: str = ""
    # MACOS_CALENDARS: calendar names to read from the Calendar app (empty = all).
    macos_calendars: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    # Korean explanation when not configured.
    hint: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.source)


_WEBCAL_RE = re.compile(r"^webcals?://", re.IGNORECASE)


def ics_fetch_url(url: str) -> str:
    """The https:// address to fetch for a calendar subscription URL.

    iCloud's public calendar link (and other subscription links) use
    ``webcal://`` or ``webcals://``, which are plain HTTPS feeds. Other URLs
    are returned unchanged (apart from surrounding whitespace).
    """
    url = (url or "").strip()
    return _WEBCAL_RE.sub("https://", url, count=1)


def load_calendar_config(env: Mapping[str, str] | None = None, platform: str | None = None) -> CalendarConfig:
    """Calendar settings and the source to read.

    ``CALENDAR_SOURCE`` (default ``auto``): ``ics`` reads CALENDAR_ICS_URLS,
    ``macos`` reads the Calendar app (Mac only), ``auto`` picks ICS when
    CALENDAR_ICS_URLS is set and otherwise the Calendar app on a Mac.
    """
    platform = current_platform() if platform is None else platform
    raw_source = _get(env, "CALENDAR_SOURCE")
    requested = raw_source.lower() or CALENDAR_SOURCE_AUTO
    cfg = CalendarConfig(
        urls=[ics_fetch_url(url) for url in split_csv(_get(env, "CALENDAR_ICS_URLS"))],
        timezone_name=get_timezone_name(env),
        requested_source=requested,
        macos_calendars=split_csv(_get(env, "MACOS_CALENDARS")),
    )
    on_mac = platform == "darwin"
    if requested not in CALENDAR_SOURCES:
        cfg.hint = (
            f"CALENDAR_SOURCE 값 '{raw_source}'은(는) 쓸 수 없습니다. "
            "auto(기본), macos, ics 가운데 하나를 쓰거나 비워 두세요."
        )
    elif requested == CALENDAR_SOURCE_ICS or (requested == CALENDAR_SOURCE_AUTO and cfg.urls):
        if cfg.urls:
            cfg.source = CALENDAR_SOURCE_ICS
        else:
            cfg.missing.append("CALENDAR_ICS_URLS")
            cfg.hint = calendar_hint(cfg.missing)
    elif on_mac:
        cfg.source = CALENDAR_SOURCE_MACOS
    else:
        cfg.missing.append("CALENDAR_ICS_URLS")
        cfg.hint = (
            not_mac_hint(cfg.missing) if requested == CALENDAR_SOURCE_MACOS else calendar_hint(cfg.missing, mac_note=True)
        )
    return cfg


_ICS_HOW = (
    "Google 캘린더 설정 > 내 캘린더의 설정 > 캘린더 통합의 'iCal 형식의 비공개 주소', "
    "macOS 캘린더 앱 iCloud 캘린더의 '캘린더 공유… > 공개 캘린더' 주소(webcal://), "
    "Outlook의 ICS 주소 가운데 쓰는 것을 복사해 쉼표로 구분해 .env의 CALENDAR_ICS_URLS에 넣으세요."
)


def calendar_hint(missing: list[str], mac_note: bool = False) -> str:
    names = ", ".join(missing)
    note = (
        f"Mac에서 실행하면 캘린더 앱을 바로 읽을 수 있습니다(터미널에서 {CALENDAR_SETUP_COMMAND} 한 번 실행). "
        "이 컴퓨터는 macOS가 아니므로 "
        if mac_note
        else ""
    )
    return f"캘린더 설정 누락: {names} — {note}{_ICS_HOW}"


def not_mac_hint(missing: list[str]) -> str:
    names = ", ".join(missing)
    return (
        f"캘린더 설정 누락: {names} — CALENDAR_SOURCE=macos(Mac 캘린더 앱 읽기)는 macOS에서만 쓸 수 있습니다. "
        f"이 컴퓨터에서는 CALENDAR_SOURCE를 비우고 {_ICS_HOW}"
    )


# ---------------------------------------------------------------- Slack

# Member ids start with U (or W on Enterprise Grid); channel ids with C/G,
# DM ids with D. A member id as the briefing target posts to the app's DM.
_SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{2,}$")
_SLACK_CHANNEL_ID_RE = re.compile(r"^[CGDUW][A-Z0-9]{2,}$")
SLACK_README_HINT = "설정 방법은 README의 'Slack에서 부르기'를 보세요."

# One Slack app per persona: (bot token env, app-level token env). Every pair
# is optional; 고뭉치 keeps the original names for backward compatibility.
SLACK_BOT_ENV: dict[str, tuple[str, str]] = {
    MUNGCHI: ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"),
    UPDATE: ("SLACK_UPDATE_BOT_TOKEN", "SLACK_UPDATE_APP_TOKEN"),
    SCHEDULE: ("SLACK_SCHEDULE_BOT_TOKEN", "SLACK_SCHEDULE_APP_TOKEN"),
}


@dataclass
class SlackBotConfig:
    """One Slack app (bot): the persona it speaks as and its token pair."""

    persona: str
    bot_env: str
    app_env: str
    # Tokens stay out of repr() so a logged config never leaks them.
    bot_token: str = field(default="", repr=False)
    app_token: str = field(default="", repr=False)

    @property
    def label(self) -> str:
        return PERSONA_LABELS[self.persona]

    @property
    def handle(self) -> str:
        return SLACK_HANDLES[self.persona]

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.app_token)

    @property
    def partial(self) -> bool:
        return bool(self.bot_token) != bool(self.app_token)


@dataclass
class SlackConfig:
    # 고뭉치's tokens (SLACK_BOT_TOKEN / SLACK_APP_TOKEN), also used by --brief --slack.
    bot_token: str = field(default="", repr=False)
    app_token: str = field(default="", repr=False)
    allowed_user_ids: frozenset[str] = frozenset()
    invalid_user_ids: list[str] = field(default_factory=list)
    brief_channel: str = ""
    max_concurrent: int = DEFAULT_SLACK_MAX_CONCURRENT
    # Every persona's bot in PERSONAS order, configured or not.
    bots: list[SlackBotConfig] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.bots:  # built by hand: only 고뭉치's tokens are known
            self.bots = [
                SlackBotConfig(
                    persona=persona,
                    bot_env=SLACK_BOT_ENV[persona][0],
                    app_env=SLACK_BOT_ENV[persona][1],
                    bot_token=self.bot_token if persona == MUNGCHI else "",
                    app_token=self.app_token if persona == MUNGCHI else "",
                )
                for persona in PERSONAS
            ]

    def bot(self, persona: str) -> SlackBotConfig:
        for bot in self.bots:
            if bot.persona == persona:
                return bot
        raise KeyError(persona)

    @property
    def configured_bots(self) -> list[SlackBotConfig]:
        return [bot for bot in self.bots if bot.configured]


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
    bots = [
        SlackBotConfig(
            persona=persona,
            bot_env=SLACK_BOT_ENV[persona][0],
            app_env=SLACK_BOT_ENV[persona][1],
            bot_token=_get(env, SLACK_BOT_ENV[persona][0]),
            app_token=_get(env, SLACK_BOT_ENV[persona][1]),
        )
        for persona in PERSONAS
    ]
    return SlackConfig(
        bot_token=bots[0].bot_token,
        app_token=bots[0].app_token,
        allowed_user_ids=frozenset(allowed),
        invalid_user_ids=invalid,
        brief_channel=_get(env, "SLACK_BRIEF_CHANNEL"),
        max_concurrent=max_concurrent if max_concurrent > 0 else DEFAULT_SLACK_MAX_CONCURRENT,
        bots=bots,
    )


def _token_shape_problems(bot_env: str, bot_token: str, app_env: str, app_token: str) -> list[str]:
    problems: list[str] = []
    if bot_token and not bot_token.startswith("xoxb-"):
        swapped = f" (xapp-로 시작하는 토큰은 {app_env}에 넣으세요)" if bot_token.startswith("xapp-") else ""
        problems.append(f"{bot_env} 값은 xoxb-로 시작하는 Bot User OAuth Token이어야 합니다{swapped}.")
    if app_token and not app_token.startswith("xapp-"):
        swapped = f" (xoxb-로 시작하는 토큰은 {bot_env}에 넣으세요)" if app_token.startswith("xoxb-") else ""
        problems.append(
            f"{app_env} 값은 xapp-로 시작하는 App-Level Token(connections:write 권한)이어야 합니다{swapped}."
        )
    return problems


def _bot_token_problems(cfg: SlackConfig) -> list[str]:
    return _token_shape_problems("SLACK_BOT_TOKEN", cfg.bot_token, "SLACK_APP_TOKEN", "")


def slack_bot_problems(cfg: SlackConfig) -> list[str]:
    """Korean problem lines that keep ``python -m mungchi slack`` from starting.

    Each bot (고뭉치, 업뎃, 일정) is optional, but at least one must have both
    tokens and none may have only one. An empty allow-list is a hard error:
    the bots read private Dropbox and calendar data and must never
    answer anyone but their owner.
    """
    bots = cfg.bots
    missing = [bot.app_env if bot.bot_token else bot.bot_env for bot in bots if bot.partial]
    if not (cfg.allowed_user_ids or cfg.invalid_user_ids):
        missing.append("SLACK_ALLOWED_USER_IDS")
    problems = [f"빠진 환경변수: {', '.join(missing)}"] if missing else []
    if not any(bot.bot_token or bot.app_token for bot in bots):
        pairs = ", ".join(f"{bot.label} 봇 {bot.bot_env}+{bot.app_env}" for bot in bots)
        problems.append(f"Slack 봇 토큰이 하나도 없습니다. 쓰려는 봇마다 토큰 두 개를 넣으세요(봇 하나 이상): {pairs}.")
    for bot in bots:
        if bot.partial:
            problems.append(
                f"{bot.label} 봇의 토큰이 하나만 있습니다. {bot.bot_env}과 {bot.app_env}을 둘 다 넣거나 둘 다 비우세요."
            )
        problems += _token_shape_problems(bot.bot_env, bot.bot_token, bot.app_env, bot.app_token)
    used: dict[str, list[str]] = {}
    for bot in bots:
        for name, value in ((bot.bot_env, bot.bot_token), (bot.app_env, bot.app_token)):
            if value:
                used.setdefault(value, []).append(name)
    for names in used.values():
        if len(names) > 1:
            problems.append(
                f"같은 토큰이 여러 변수에 들어 있습니다: {', '.join(names)} — 봇마다 따로 만든 Slack 앱의 토큰을 넣으세요."
            )
    if cfg.invalid_user_ids:
        problems.append(
            "SLACK_ALLOWED_USER_IDS에 멤버 ID가 아닌 값이 있습니다: "
            f"{', '.join(cfg.invalid_user_ids)} — 멤버 ID는 U로 시작하는 영문 대문자·숫자입니다 "
            "(Slack 프로필 → ⋮ → 멤버 ID 복사)."
        )
    if not cfg.allowed_user_ids:
        problems.append(
            "SLACK_ALLOWED_USER_IDS가 비어 있어 봇을 시작하지 않습니다. 고뭉치·업뎃·일정은 Dropbox·캘린더의 "
            "개인 정보를 읽기 때문에, 답해도 되는 사람(보통 나 혼자)의 멤버 ID를 쉼표로 구분해 넣어야 합니다."
        )
    return problems


def slack_brief_problems(cfg: SlackConfig) -> list[str]:
    """Korean problem lines that keep a briefing (고뭉치's bot token) from being posted.

    Used by ``--brief --slack`` and the scheduled morning briefing. The
    briefing goes to ``SLACK_BRIEF_CHANNEL``, or without it as a DM to every
    user in ``SLACK_ALLOWED_USER_IDS`` (see ``brief_destinations``).
    """
    no_destination = not (cfg.brief_channel or cfg.allowed_user_ids or cfg.invalid_user_ids)
    missing = ["SLACK_BOT_TOKEN"] if not cfg.bot_token else []
    if no_destination:
        missing.append("SLACK_BRIEF_CHANNEL(또는 SLACK_ALLOWED_USER_IDS)")
    problems = [f"빠진 환경변수: {', '.join(missing)}"] if missing else []
    if no_destination:
        problems.append(
            "브리핑을 보낼 곳이 없습니다. SLACK_BRIEF_CHANNEL에 채널 ID를 넣거나, 비워 두고 SLACK_ALLOWED_USER_IDS에 "
            "내 멤버 ID를 넣으면 고뭉치 봇이 DM으로 보냅니다."
        )
    problems += _bot_token_problems(cfg)
    if cfg.brief_channel and not _SLACK_CHANNEL_ID_RE.match(cfg.brief_channel):
        problems.append(
            "SLACK_BRIEF_CHANNEL 값은 채널 이름(#general)이 아니라 채널 ID(C로 시작하는 영문 대문자·숫자)여야 합니다."
        )
    if not cfg.brief_channel and cfg.invalid_user_ids:
        problems.append(
            "SLACK_ALLOWED_USER_IDS에 멤버 ID가 아닌 값이 있습니다: "
            f"{', '.join(cfg.invalid_user_ids)} — SLACK_BRIEF_CHANNEL이 비어 있으면 브리핑을 이 사람들에게 DM으로 "
            "보내니, U로 시작하는 멤버 ID만 적으세요."
        )
    return problems


def brief_destinations(cfg: SlackConfig) -> list[str]:
    """Where a briefing goes: ``SLACK_BRIEF_CHANNEL``, else a DM to each user in ``SLACK_ALLOWED_USER_IDS``.

    A member id as the channel posts to the bot's DM with that person.
    """
    if cfg.brief_channel:
        return [cfg.brief_channel]
    return sorted(cfg.allowed_user_ids)


def describe_brief_destination(cfg: SlackConfig) -> str:
    """Korean description of ``brief_destinations``, e.g. ``채널 C0123ABCD`` or ``DM (허용된 사용자 1명)``."""
    if cfg.brief_channel:
        kind = "DM" if cfg.brief_channel[:1] in ("U", "W", "D") else "채널"
        return f"{kind} {cfg.brief_channel}"
    return f"DM (허용된 사용자 {len(cfg.allowed_user_ids)}명)"


# ---------------------------------------------------------------- scheduled morning briefing

BRIEF_DAYS_DAILY = "daily"
BRIEF_DAYS_WEEKDAYS = "weekdays"
_BRIEF_DAYS_VALUES = {
    "daily": BRIEF_DAYS_DAILY,
    "매일": BRIEF_DAYS_DAILY,
    "weekdays": BRIEF_DAYS_WEEKDAYS,
    "평일": BRIEF_DAYS_WEEKDAYS,
}
BRIEF_DAYS_LABELS = {BRIEF_DAYS_DAILY: "매일", BRIEF_DAYS_WEEKDAYS: "평일"}
# A briefing missed at BRIEF_TIME (Mac asleep, bot started late) is still
# sent until this local time; after it the day is skipped.
DEFAULT_BRIEF_CATCHUP_UNTIL = time(12, 0)
BRIEF_OFF_UNSET = "BRIEF_TIME 미설정"
BRIEF_OFF_INVALID = "BRIEF_TIME 값이 잘못됨"
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_hhmm(value: str | None) -> time | None:
    """``"07:00"`` or ``"7:00"`` -> ``time(7, 0)``; None for anything else (24-hour clock)."""
    match = _HHMM_RE.match((value or "").strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


@dataclass(frozen=True)
class BriefSchedule:
    """When the running Slack bots send the morning briefing.

    ``at`` is ``BRIEF_TIME`` (None: off), ``days`` ``daily`` or ``weekdays``,
    ``catchup_until`` the local time after which a missed briefing is skipped
    for the day (None: until midnight). Times are wall-clock times in ``timezone``.
    ``warnings`` are Korean lines about values that could not be used.
    """

    at: time | None = None
    days: str = BRIEF_DAYS_DAILY
    catchup_until: time | None = DEFAULT_BRIEF_CATCHUP_UNTIL
    timezone: tzinfo = field(default_factory=lambda: ZoneInfo(DEFAULT_TIMEZONE))
    off_reason: str = BRIEF_OFF_UNSET
    warnings: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return self.at is not None

    @property
    def timezone_name(self) -> str:
        return str(getattr(self.timezone, "key", "") or self.timezone)

    def describe(self) -> str:
        """``매일 07:00 (Asia/Seoul)``, or ``꺼짐 (BRIEF_TIME 미설정)``."""
        if self.at is None:
            return f"꺼짐 ({self.off_reason})"
        return f"{BRIEF_DAYS_LABELS[self.days]} {self.at:%H:%M} ({self.timezone_name})"

    def catchup_text(self) -> str:
        """Until when a missed briefing is still sent, e.g. ``12:00 전까지``."""
        return f"{self.catchup_until:%H:%M} 전까지" if self.catchup_until is not None else "그날 자정 전까지"


def load_brief_schedule(env: Mapping[str, str] | None = None) -> BriefSchedule:
    """``BRIEF_TIME``, ``BRIEF_DAYS`` and ``BRIEF_CATCHUP_UNTIL``; never raises.

    An empty or unset ``BRIEF_TIME`` turns the briefing off; an invalid one
    turns it off with a warning. An invalid ``BRIEF_DAYS`` or
    ``BRIEF_CATCHUP_UNTIL`` keeps the default with a warning.
    """
    warnings: list[str] = []
    raw_days = _get(env, "BRIEF_DAYS")
    days = _BRIEF_DAYS_VALUES.get(raw_days.lower(), "") if raw_days else BRIEF_DAYS_DAILY
    if not days:
        warnings.append(
            f"BRIEF_DAYS 값 '{raw_days}'은(는) 쓸 수 없어 매일 보냅니다. daily(매일) 또는 weekdays(평일)를 적으세요."
        )
        days = BRIEF_DAYS_DAILY

    raw_catchup = _get(env, "BRIEF_CATCHUP_UNTIL")
    catchup: time | None = DEFAULT_BRIEF_CATCHUP_UNTIL
    if raw_catchup == "24:00":
        catchup = None
    elif raw_catchup:
        parsed = parse_hhmm(raw_catchup)
        if parsed is None:
            warnings.append(
                f"BRIEF_CATCHUP_UNTIL 값 '{raw_catchup}'은(는) 쓸 수 없어 기본값 "
                f"{DEFAULT_BRIEF_CATCHUP_UNTIL:%H:%M}을 씁니다. 12:00처럼 24시간제 HH:MM으로 적으세요."
            )
        else:
            catchup = parsed

    tz = get_timezone(env)
    raw_time = _get(env, "BRIEF_TIME")
    if not raw_time:
        return BriefSchedule(None, days, catchup, tz, BRIEF_OFF_UNSET, tuple(warnings))
    at = parse_hhmm(raw_time)
    if at is None:
        warnings.append(
            f"BRIEF_TIME 값 '{raw_time}'은(는) 쓸 수 없어 아침 브리핑을 끕니다. 07:00처럼 24시간제 HH:MM으로 적으세요."
        )
        return BriefSchedule(None, days, catchup, tz, BRIEF_OFF_INVALID, tuple(warnings))
    if catchup is not None and catchup <= at:
        # e.g. BRIEF_TIME=13:00 with the default 12:00: catching up "before noon" would never send.
        if raw_catchup:
            warnings.append(
                f"BRIEF_CATCHUP_UNTIL({catchup:%H:%M})이 BRIEF_TIME({at:%H:%M})보다 늦지 않아서, "
                "놓친 브리핑은 그날 자정 전까지 보냅니다."
            )
        catchup = None
    return BriefSchedule(at, days, catchup, tz, "", tuple(warnings))


# ---------------------------------------------------------------- Secrets


SECRET_ENV_VARS = (
    "DROPBOX_ACCESS_TOKEN",
    "DROPBOX_REFRESH_TOKEN",
    "DROPBOX_APP_SECRET",
    "DROPBOX_APP_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_UPDATE_BOT_TOKEN",
    "SLACK_UPDATE_APP_TOKEN",
    "SLACK_SCHEDULE_BOT_TOKEN",
    "SLACK_SCHEDULE_APP_TOKEN",
)


def secret_values(env: Mapping[str, str] | None = None) -> list[str]:
    """Every configured secret string that must never appear in output."""
    values: list[str] = []
    for key in SECRET_ENV_VARS:
        value = _get(env, key)
        if value:
            values.append(value)
    # Private (Google) and public (iCloud) ICS addresses embed a secret key
    # in the URL itself, both as configured and as the https:// form fetched.
    for url in split_csv(_get(env, "CALENDAR_ICS_URLS")):
        values.append(url)
        fetched = ics_fetch_url(url)
        if fetched != url:
            values.append(fetched)
        # The same address without its scheme (host/path), as some errors print it.
        _scheme, sep, rest = fetched.partition("://")
        if sep and rest:
            values.append(rest)
    return values
