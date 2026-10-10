"""``python -m mungchi --calendar-setup``: connect the macOS Calendar app once, in Terminal.

Shows the calendar permission, asks macOS for access if it has not been
decided yet (the system dialog appears once), then lists the calendars by
account and the events of today and tomorrow that the '일정' agent would read
under the current ``MACOS_CALENDARS`` and ``CALENDAR_EXCLUDE`` settings, and
the calendars events from a pasted note can be added to (the categories of
``CALENDAR_CATEGORIES``, or without them ``CALENDAR_WRITE_TARGET``).

``python -m mungchi --calendars`` (``run_calendar_list``) is the read-only
diagnostic: every calendar EventKit sees, with its account, type, whether it
takes new events, whether ``CALENDAR_EXCLUDE`` (or ``MACOS_CALENDARS``) keeps
it out, and how many events it has in the next 7 days. It never asks for
access. Neither command makes a Claude API call.
"""

from __future__ import annotations

import sys
from datetime import datetime, time, timedelta
from typing import Mapping, TextIO

from . import config
from .agents import WEEKDAYS_KO
from .tools import calendar_tool, event_proposals, macos_calendar
from .tools.calendar_tool import AdapterFactory
from .tools.common import safe_error

# The user has to notice the dialog and click, so wait longer than the tool would.
SETUP_REQUEST_TIMEOUT_SECONDS = 180
MAX_SAMPLE_EVENTS = 8
NO_SOURCE = "(계정 이름 없음)"
LIST_COMMAND = "python -m mungchi --calendars"
# --calendars counts the events of this many days, today included.
LIST_DAYS = 7
EXCLUDE_HINT = ".env에 CALENDAR_EXCLUDE=중국 공휴일 추가 후 python -m mungchi service restart (여러 개는 쉼표로 구분)"

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
    categories = config.get_calendar_categories(env)
    if categories:
        offered, missing = event_proposals.available_categories(categories, writable)
        shown = ", ".join(
            f"{c['label']}" + (f"({c['calendar']})" if c["label"] != c["calendar"] else "") for c in offered
        )
        lines.append(f"카테고리 (CALENDAR_CATEGORIES, 일정을 추가할 때 고릅니다): {shown or '(없음)'}")
        if missing:
            quoted = ", ".join(f"'{c.calendar}'" for c in missing)
            lines.append(f"[경고] Mac 캘린더에 {quoted} 캘린더가 없어요. {event_proposals.CATEGORY_FIX_HINT}")
        return lines
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
    skip = macos_calendar.exclusion_keys(cfg.excluded_calendars)
    by_source: dict[str, list[str]] = {}
    for calendar in calendars:
        by_source.setdefault(calendar.get("source") or NO_SOURCE, []).append(calendar.get("name") or "")
    say()
    say(f"캘린더 {len(calendars)}개 (계정별):")
    for source, names in by_source.items():
        say(f"  {source}")
        for name in names:
            say(f"    - {name}{' (CALENDAR_EXCLUDE로 뺌)' if macos_calendar.is_excluded(name, skip) else ''}")
    say(f"캘린더마다 종류, 쓰기 가능 여부, 7일 일정 수는 {LIST_COMMAND}로 볼 수 있습니다.")

    say()
    if cfg.macos_calendars:
        say(f"MACOS_CALENDARS: {', '.join(cfg.macos_calendars)} (이 캘린더만 읽습니다)")
    else:
        say("MACOS_CALENDARS가 비어 있어 모든 캘린더를 읽습니다.")
    if cfg.excluded_calendars:
        say(f"CALENDAR_EXCLUDE: {', '.join(cfg.excluded_calendars)} (이 캘린더는 읽지도, 일정을 넣지도 않습니다)")
    today = datetime.combine((now or datetime.now(tz)).astimezone(tz).date(), time.min, tzinfo=tz)
    events, warnings = calendar_tool.macos_events(
        adapter, cfg.macos_calendars, tz, today, today + timedelta(days=2), cfg.excluded_calendars
    )
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


# ---------------------------------------------------------------- --calendars


def calendar_list_lines(
    overview: list[dict], cfg: config.CalendarConfig, start: datetime, days: int = LIST_DAYS
) -> list[str]:
    """``--calendars`` body (Korean): every calendar by account, then the settings and the exclude hint.

    Each line: name · type · 쓰기 가능/읽기 전용 · events in ``[start, start + days)``,
    plus why the '일정' agent does not read it (``CALENDAR_EXCLUDE`` first, then ``MACOS_CALENDARS``).
    """
    names = [str(item.get("name") or "") for item in overview]
    skip = macos_calendar.exclusion_keys(cfg.excluded_calendars)
    selected, not_found = macos_calendar.select_calendars(cfg.macos_calendars, names)
    wanted = None if selected is None else {macos_calendar.normalize_name(name) for name in selected}
    last = start + timedelta(days=days - 1)
    by_source: dict[str, list[str]] = {}
    read = 0
    for item in overview:
        name = str(item.get("name") or "")
        parts = [name]
        if item.get("type"):
            parts.append(str(item["type"]))
        parts.append("쓰기 가능" if item.get("writable") else "읽기 전용")
        parts.append(f"일정 {int(item.get('events') or 0)}개")
        if macos_calendar.is_excluded(name, skip):
            parts.append("제외됨(CALENDAR_EXCLUDE)")
        elif wanted is not None and macos_calendar.normalize_name(name) not in wanted:
            parts.append("안 읽음(MACOS_CALENDARS에 없음)")
        else:
            read += 1
        by_source.setdefault(str(item.get("source") or NO_SOURCE), []).append(" · ".join(parts))

    lines = [
        f"캘린더 {len(overview)}개, 계정별 (일정 수는 오늘부터 {days}일: "
        f"{start:%m/%d}({WEEKDAYS_KO[start.weekday()]})–{last:%m/%d}({WEEKDAYS_KO[last.weekday()]})):"
    ]
    if not overview:
        lines.append("  (캘린더가 하나도 없습니다. 캘린더 앱에 계정이 있는지, 접근 권한이 '전체 접근'인지 확인하세요.)")
    for source, entries in by_source.items():
        lines.append(f"  {source}")
        lines.extend(f"    - {entry}" for entry in entries)
    lines.append("")
    lines.append(f"'일정'이 읽는 캘린더 {read}개, 읽지 않는 캘린더 {len(overview) - read}개")
    if cfg.macos_calendars:
        lines.append(f"MACOS_CALENDARS: {', '.join(cfg.macos_calendars)} (이 캘린더만 읽습니다)")
    else:
        lines.append("MACOS_CALENDARS: 비어 있음 (모든 캘린더를 읽습니다)")
    if cfg.excluded_calendars:
        lines.append(f"CALENDAR_EXCLUDE: {', '.join(cfg.excluded_calendars)} (읽지도, 일정을 넣지도 않습니다)")
    else:
        lines.append("CALENDAR_EXCLUDE: 비어 있음 (빼는 캘린더 없음)")
    lines.extend("[경고] " + warning for warning in calendar_tool.missing_calendar_warnings(not_found))
    present = macos_calendar.exclusion_keys(names)
    for name in cfg.excluded_calendars:
        if macos_calendar.normalize_name(name) not in present:
            lines.append(f"[참고] CALENDAR_EXCLUDE의 '{name}' 캘린더는 캘린더 앱에 없습니다(이미 지웠다면 .env에서 빼도 됩니다).")
    if cfg.source == config.CALENDAR_SOURCE_ICS:
        lines.append("[참고] 지금은 .env의 CALENDAR_ICS_URLS(ICS 주소)를 읽으므로 '일정'은 이 캘린더들을 읽지 않습니다.")
    elif not cfg.configured:
        lines.append("[참고] " + cfg.hint)
    lines.append(f"캘린더를 빼려면: {EXCLUDE_HINT}")
    return lines


def run_calendar_list(
    env: Mapping[str, str] | None = None,
    *,
    adapter_factory: AdapterFactory | None = None,
    platform: str | None = None,
    now: datetime | None = None,
    out: TextIO | None = None,
) -> int:
    """``--calendars``: list every calendar EventKit sees. Read-only; never asks for access."""
    out = out or sys.stdout

    def say(line: str = "") -> None:
        print(line, file=out, flush=True)

    platform = config.current_platform() if platform is None else platform
    tz = config.get_timezone(env)
    cfg = config.load_calendar_config(env, platform=platform)

    say("Mac 캘린더 앱의 캘린더 목록 (EventKit이 보는 그대로, Claude API는 쓰지 않습니다)")
    if platform != "darwin":
        say(NOT_MAC_TEXT)
        return 1
    try:
        adapter = (adapter_factory or macos_calendar.default_adapter)(tz)
    except macos_calendar.EventKitUnavailable:
        say("[오류] " + macos_calendar.EVENTKIT_MISSING_HINT)
        return 1
    status = adapter.authorization_status()
    say(f"캘린더 접근 권한: {macos_calendar.STATUS_LABELS.get(status, status)}")
    if status == macos_calendar.NOT_DETERMINED:
        say("[오류] " + macos_calendar.NOT_DETERMINED_HINT)
        return 1
    if status != macos_calendar.GRANTED:
        say("[오류] " + macos_calendar.permission_hint(status))
        return 1
    today = datetime.combine((now or datetime.now(tz)).astimezone(tz).date(), time.min, tzinfo=tz)
    try:
        overview = adapter.calendar_overview(today, today + timedelta(days=LIST_DAYS))
    except Exception as exc:  # noqa: BLE001 - a clean Korean line, never a traceback
        say(f"[오류] 캘린더를 읽지 못했습니다: {safe_error(exc)}")
        return 1
    say()
    for line in calendar_list_lines(overview, cfg, today):
        say(line)
    return 0
