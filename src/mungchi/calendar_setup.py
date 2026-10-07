"""``python -m mungchi --calendar-setup``: connect the macOS Calendar app once, in Terminal.

Shows the calendar permission, asks macOS for access if it has not been
decided yet (the system dialog appears once), then lists the calendars by
account and the events of today and tomorrow that the '일정' agent would read
under the current ``MACOS_CALENDARS`` filter, and the calendars events from a
pasted note can be added to (``CALENDAR_WRITE_TARGET``). No Claude API call is made.
"""

from __future__ import annotations

import sys
from datetime import datetime, time, timedelta
from typing import Mapping, TextIO

from . import config
from .agents import WEEKDAYS_KO
from .tools import calendar_tool, macos_calendar
from .tools.calendar_tool import AdapterFactory
from .tools.common import safe_error

# The user has to notice the dialog and click, so wait longer than the tool would.
SETUP_REQUEST_TIMEOUT_SECONDS = 180
MAX_SAMPLE_EVENTS = 8

NOT_MAC_TEXT = (
    "[오류] Mac 캘린더 앱 연결은 macOS에서만 할 수 있습니다. "
    "이 컴퓨터에서는 .env의 CALENDAR_ICS_URLS에 캘린더 ICS 주소를 넣으세요 (README의 '캘린더' 참고)."
)


def _day_label(day_offset: int, day: datetime) -> str:
    name = ("오늘", "내일")[day_offset]
    return f"{name} ({day.date().isoformat()} {WEEKDAYS_KO[day.weekday()]}요일)"


def _event_line(event: calendar_tool.Event) -> str:
    when = "종일" if event.all_day else f"{event.start:%H:%M}–{event.end:%H:%M}"
    calendar = f" [{event.calendar}]" if event.calendar else ""
    return f"    - {when} {event.title}{calendar}"


def write_target_lines(adapter: macos_calendar.CalendarAdapter, env: Mapping[str, str] | None = None) -> list[str]:
    """Which calendars accept new events and which one a confirmed note goes to (Korean lines)."""
    try:
        writable = adapter.list_writable_calendars()
    except Exception as exc:  # noqa: BLE001 - the rest of the setup still counts
        return [f"[경고] 일정을 추가할 수 있는 캘린더를 확인하지 못했습니다: {safe_error(exc)}"]
    lines = [f"일정을 추가할 수 있는 캘린더 {len(writable)}개 (메모로 일정 추가):"]
    for calendar in writable:
        source = f" ({calendar.get('source')})" if calendar.get("source") else ""
        default = " [기본]" if calendar.get("is_default") else ""
        lines.append(f"    - {calendar.get('name') or ''}{source}{default}")
    target = config.get_calendar_write_target(env)
    name, error = macos_calendar.resolve_write_calendar(writable, None, target)
    if error:
        lines.append(f"[경고] {error}")
    elif target:
        lines.append(f"추가할 캘린더: {name} (CALENDAR_WRITE_TARGET)")
    else:
        lines.append(f"추가할 캘린더: {name or '기본 캘린더'} (CALENDAR_WRITE_TARGET이 비어 있어 기본 캘린더에 넣습니다)")
    return lines


def run_calendar_setup(
    env: Mapping[str, str] | None = None,
    *,
    adapter_factory: AdapterFactory | None = None,
    platform: str | None = None,
    now: datetime | None = None,
    out: TextIO | None = None,
) -> int:
    out = out or sys.stdout

    def say(line: str = "") -> None:
        print(line, file=out, flush=True)

    platform = config.current_platform() if platform is None else platform
    tz = config.get_timezone(env)
    cfg = config.load_calendar_config(env, platform=platform)

    say("Mac 캘린더 앱 연결 확인 (Claude API는 쓰지 않습니다)")
    if platform != "darwin":
        say(NOT_MAC_TEXT)
        return 1
    try:
        adapter = (adapter_factory or macos_calendar.default_adapter)(tz)
    except macos_calendar.EventKitUnavailable:
        say("[오류] " + macos_calendar.EVENTKIT_MISSING_HINT)
        return 1

    status = adapter.authorization_status()
    say(f"지금 캘린더 접근 권한: {macos_calendar.STATUS_LABELS.get(status, status)}")
    if status == macos_calendar.NOT_DETERMINED:
        say()
        say("캘린더 접근을 요청합니다. 곧 macOS 확인 창이 뜨면 '허용'을 눌러 주세요.")
        say(
            "확인 창에는 고뭉치 대신 이 명령을 실행한 터미널 앱 이름(예: '터미널', 'iTerm')이 나올 수 있습니다. "
            "그 앱에 허용하면 됩니다."
        )
        granted = adapter.request_access(timeout=SETUP_REQUEST_TIMEOUT_SECONDS)
        status = macos_calendar.GRANTED if granted else adapter.authorization_status()
        say(f"결과: {macos_calendar.STATUS_LABELS.get(status, status)}")
    if status == macos_calendar.NOT_DETERMINED:
        say("[오류] 확인 창의 응답을 받지 못했습니다(시간 초과). 창이 보이지 않았다면 이 명령을 다시 실행하세요.")
        return 1
    if status != macos_calendar.GRANTED:
        say("[오류] " + macos_calendar.permission_hint(status))
        return 1

    calendars = adapter.list_calendars()
    by_source: dict[str, list[str]] = {}
    for calendar in calendars:
        by_source.setdefault(calendar.get("source") or "(계정 이름 없음)", []).append(calendar.get("name") or "")
    say()
    say(f"캘린더 {len(calendars)}개 (계정별):")
    for source, names in by_source.items():
        say(f"  {source}")
        for name in names:
            say(f"    - {name}")

    say()
    if cfg.macos_calendars:
        say(f"MACOS_CALENDARS: {', '.join(cfg.macos_calendars)} (이 캘린더만 읽습니다)")
    else:
        say("MACOS_CALENDARS가 비어 있어 모든 캘린더를 읽습니다.")
    today = datetime.combine((now or datetime.now(tz)).astimezone(tz).date(), time.min, tzinfo=tz)
    events, warnings = calendar_tool.macos_events(adapter, cfg.macos_calendars, tz, today, today + timedelta(days=2))
    for warning in warnings:
        say("[경고] " + warning)
    events.sort(key=lambda e: (e.start, 0 if e.all_day else 1, e.title))
    for offset in (0, 1):
        day_start = today + timedelta(days=offset)
        day_events = [e for e in events if calendar_tool.overlaps_window(e, day_start, day_start + timedelta(days=1))]
        say(f"{_day_label(offset, day_start)}: 일정 {len(day_events)}개")
        for event in day_events[:MAX_SAMPLE_EVENTS]:
            say(_event_line(event))
        if len(day_events) > MAX_SAMPLE_EVENTS:
            say(f"    … 외 {len(day_events) - MAX_SAMPLE_EVENTS}개")

    say()
    for line in write_target_lines(adapter, env):
        say(line)

    say()
    if cfg.source == config.CALENDAR_SOURCE_MACOS:
        say("연결 완료: '일정' 에이전트가 Mac 캘린더 앱을 읽습니다. 이미 켜 둔 봇이 있으면 다시 시작하세요.")
    elif cfg.source == config.CALENDAR_SOURCE_ICS:
        say(
            "[참고] 지금은 .env의 CALENDAR_ICS_URLS(ICS 주소)를 읽습니다. Mac 캘린더 앱을 읽으려면 "
            "CALENDAR_ICS_URLS를 비우거나 CALENDAR_SOURCE=macos로 바꾼 뒤 봇을 다시 시작하세요."
        )
    else:
        say("[참고] " + cfg.hint)
    return 0
