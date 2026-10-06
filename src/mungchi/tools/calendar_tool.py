"""``get_schedule``: events from the macOS Calendar app (EventKit) or from ICS
feeds (Google private address, iCloud public calendar ``webcal://`` link,
Outlook published calendar).

Both sources produce the same ``Event`` list, so the output (events, now,
next_event, overlaps, gaps) is built by the same code; only ``source`` differs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, Callable, Mapping

import httpx
import icalendar
import recurring_ical_events
from claude_agent_sdk import ToolAnnotations, tool

from .. import config
from ..state import utcnow
from . import macos_calendar
from .common import MAX_RESULT_SIZE_CHARS, int_arg, safe_error, tool_result, unconfigured

MAX_DAYS = 14
MIN_GAP_MINUTES = 30
FETCH_TIMEOUT_SECONDS = 20
NO_TITLE = "(제목 없음)"

Fetcher = Callable[[str], bytes]
AdapterFactory = Callable[[tzinfo], macos_calendar.CalendarAdapter]


@dataclass
class Event:
    start: datetime
    end: datetime
    all_day: bool
    title: str
    location: str
    calendar: str

    def to_dict(self) -> dict[str, Any]:
        if self.all_day:
            # ICS all-day DTEND is exclusive; report the last day inclusively.
            last_day = (self.end - timedelta(days=1)).date()
            start, end = self.start.date().isoformat(), max(last_day, self.start.date()).isoformat()
        else:
            start = self.start.isoformat(timespec="minutes")
            end = self.end.isoformat(timespec="minutes")
        return {
            "start": start,
            "end": end,
            "all_day": self.all_day,
            "title": self.title,
            "location": self.location,
            "calendar": self.calendar,
        }


def _as_local(value: date | datetime, tz: tzinfo) -> tuple[datetime, bool]:
    """Return ``(aware local datetime, is_all_day)``."""
    if isinstance(value, datetime):
        if value.tzinfo is None:  # floating time: interpret in the user's zone
            return value.replace(tzinfo=tz), False
        return value.astimezone(tz), False
    return datetime.combine(value, time.min, tzinfo=tz), True


def _component_event(component: Any, calendar_name: str, tz: tzinfo) -> Event | None:
    if str(component.get("STATUS", "")).upper() == "CANCELLED":
        return None
    dtstart = component.get("DTSTART")
    if dtstart is None:
        return None
    start, all_day = _as_local(dtstart.dt, tz)
    dtend = component.get("DTEND")
    duration = component.get("DURATION")
    if dtend is not None:
        end, _ = _as_local(dtend.dt, tz)
    elif duration is not None:
        end = start + duration.dt
    else:
        end = start + (timedelta(days=1) if all_day else timedelta(0))
    if end < start:
        end = start
    return Event(
        start=start,
        end=end,
        all_day=all_day,
        title=str(component.get("SUMMARY", "")).strip() or NO_TITLE,
        location=str(component.get("LOCATION", "")).strip(),
        calendar=calendar_name,
    )


def calendar_name(cal: Any, fallback: str) -> str:
    name = cal.get("X-WR-CALNAME")
    return str(name).strip() if name else fallback


def parse_events(
    ics: bytes | str, fallback_name: str, tz: tzinfo, start_day: date, end_day: date
) -> list[Event]:
    """Expand events (including recurrences) overlapping ``[start_day, end_day)``."""
    cal = icalendar.Calendar.from_ical(ics)
    name = calendar_name(cal, fallback_name)
    events: list[Event] = []
    for component in recurring_ical_events.of(cal).between(start_day, end_day):
        event = _component_event(component, name, tz)
        if event is not None:
            events.append(event)
    return events


def adapter_event(record: Mapping[str, Any], tz: tzinfo) -> Event:
    """An ``Event`` from a Calendar app record (``macos_calendar.event_record`` shape)."""
    start = record["start"].astimezone(tz)
    end = max(record["end"].astimezone(tz), start)
    return Event(
        start=start,
        end=end,
        all_day=bool(record.get("all_day")),
        title=str(record.get("title") or "").strip() or NO_TITLE,
        location=str(record.get("location") or "").strip(),
        calendar=str(record.get("calendar") or ""),
    )


def _sort_key(event: Event) -> tuple[datetime, int, str]:
    return (event.start, 0 if event.all_day else 1, event.title)


def overlaps_window(event: Event, start: datetime, end: datetime) -> bool:
    if event.end == event.start:  # zero-length event
        return start <= event.start < end
    return event.start < end and event.end > start


def find_overlaps(events: list[Event]) -> list[dict[str, Any]]:
    timed = sorted((e for e in events if not e.all_day), key=_sort_key)
    overlaps: list[dict[str, Any]] = []
    for i, first in enumerate(timed):
        for second in timed[i + 1 :]:
            if second.start >= first.end:
                break
            overlaps.append(
                {
                    "events": [first.title, second.title],
                    "from": max(first.start, second.start).isoformat(timespec="minutes"),
                    "to": min(first.end, second.end).isoformat(timespec="minutes"),
                }
            )
    return overlaps


def find_gaps(events: list[Event], min_minutes: int = MIN_GAP_MINUTES) -> list[dict[str, Any]]:
    """Free slots between consecutive timed events on the same day."""
    timed = sorted((e for e in events if not e.all_day), key=_sort_key)
    gaps: list[dict[str, Any]] = []
    current_day: date | None = None
    busy_until: datetime | None = None
    for event in timed:
        day = event.start.date()
        if day != current_day or busy_until is None:
            current_day, busy_until = day, event.end
            continue
        minutes = int((event.start - busy_until).total_seconds() // 60)
        if minutes >= min_minutes and busy_until.date() == day:
            gaps.append(
                {
                    "date": day.isoformat(),
                    "from": busy_until.strftime("%H:%M"),
                    "to": event.start.strftime("%H:%M"),
                    "minutes": minutes,
                }
            )
        busy_until = max(busy_until, event.end)
    return gaps


def build_schedule(
    events: list[Event], tz: tzinfo, start_day: date, days: int, now: datetime
) -> dict[str, Any]:
    """Events in the requested window plus ``now`` / ``next_event`` relative to ``now``."""
    now = now.astimezone(tz)
    window_start = datetime.combine(start_day, time.min, tzinfo=tz)
    window_end = window_start + timedelta(days=days)

    unique: dict[tuple[str, datetime, datetime], Event] = {}
    for event in events:
        unique.setdefault((event.title, event.start, event.end), event)
    ordered = sorted(unique.values(), key=_sort_key)

    in_window = [e for e in ordered if overlaps_window(e, window_start, window_end)]
    happening = [e for e in ordered if e.start <= now < e.end]
    upcoming_timed = [e for e in ordered if not e.all_day and e.start > now]
    upcoming_all_day = [e for e in ordered if e.all_day and e.start > now]
    next_event = (upcoming_timed or upcoming_all_day or [None])[0]

    return {
        "current_time": now.isoformat(timespec="minutes"),
        "range": {
            "start": start_day.isoformat(),
            "end": (start_day + timedelta(days=days - 1)).isoformat(),
            "days": days,
        },
        "events": [e.to_dict() for e in in_window],
        "now": [e.to_dict() for e in happening],
        "next_event": next_event.to_dict() if next_event else None,
        "overlaps": find_overlaps(in_window),
        "gaps": find_gaps(in_window),
    }


def fetch_ics(url: str) -> bytes:
    # webcal:// / webcals:// (iCloud public calendars) are fetched over https://.
    with httpx.Client(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True) as client:
        response = client.get(config.ics_fetch_url(url))
        response.raise_for_status()
        return response.content


def _fetch_error(exc: Exception) -> str:
    # The feed URL is itself a secret, so never echo it (nor redirect targets).
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} 응답 (주소가 바뀌었거나 만료되었을 수 있음)"
    if isinstance(exc, httpx.TimeoutException):
        return "응답 시간 초과"
    return safe_error(exc, redact_urls=True)


def parse_date_arg(value: str, tz: tzinfo, now: datetime) -> date:
    value = (value or "").strip()
    if not value:
        return now.astimezone(tz).date()
    return date.fromisoformat(value)


def expand_window(start_day: date, days: int, now: datetime) -> tuple[date, date]:
    """Days to read so both the requested window and "now / next" can be answered."""
    today = now.date()
    return min(start_day, today), max(start_day + timedelta(days=days), today + timedelta(days=2))


def ics_events(
    urls: list[str], tz: tzinfo, expand_start: date, expand_end: date, fetcher: Fetcher | None = None
) -> tuple[list[Event], list[dict[str, str]]]:
    """Events from every ICS feed, and one error entry per feed that failed."""
    events: list[Event] = []
    errors: list[dict[str, str]] = []
    fetch = fetcher or fetch_ics
    for index, url in enumerate(urls, start=1):
        label = f"캘린더 {index}"
        try:
            ics = fetch(url)
        except Exception as exc:  # noqa: BLE001 - other feeds still count
            errors.append({"calendar": label, "error": _fetch_error(exc)})
            continue
        try:
            events.extend(parse_events(ics, label, tz, expand_start, expand_end))
        except Exception as exc:  # noqa: BLE001
            errors.append({"calendar": label, "error": f"ICS 해석 실패: {safe_error(exc, redact_urls=True)}"})
    return events, errors


def macos_unconfigured(reason: str, hint: str) -> dict[str, Any]:
    return {**unconfigured([], hint), "source": config.CALENDAR_SOURCE_MACOS, "reason": reason}


def missing_calendar_warnings(not_found: list[str]) -> list[str]:
    return [
        f"MACOS_CALENDARS의 '{name}' 캘린더를 Mac 캘린더 앱에서 찾지 못했습니다(이름을 캘린더 앱과 같게 적으세요)."
        for name in not_found
    ]


def macos_events(
    adapter: macos_calendar.CalendarAdapter,
    wanted: list[str],
    tz: tzinfo,
    start: datetime,
    end: datetime,
) -> tuple[list[Event], list[str]]:
    """Events from the Calendar app in ``[start, end)``, limited to ``wanted`` calendars (empty = all).

    Returns ``(events, warnings)``. Wanted names that match no calendar are
    reported, never fatal; if none match, nothing is read (rather than all).
    """
    available = [calendar["name"] for calendar in adapter.list_calendars()]
    selected, not_found = macos_calendar.select_calendars(wanted, available)
    warnings = missing_calendar_warnings(not_found)
    if selected == []:
        return [], warnings
    keys = None if selected is None else {macos_calendar.normalize_name(name) for name in selected}
    events = [
        adapter_event(record, tz)
        for record in adapter.fetch_events(start, end, names=selected)
        if keys is None or macos_calendar.normalize_name(record.get("calendar")) in keys
    ]
    return events, warnings


def _read_macos(
    cfg: config.CalendarConfig,
    tz: tzinfo,
    expand_start: date,
    expand_end: date,
    adapter_factory: AdapterFactory | None,
) -> tuple[list[Event], dict[str, Any], dict[str, Any] | None]:
    """``(events, extra payload fields, early result)``; the early result replaces the schedule."""
    try:
        adapter = (adapter_factory or macos_calendar.default_adapter)(tz)
    except macos_calendar.EventKitUnavailable:
        return [], {}, macos_unconfigured("eventkit_missing", macos_calendar.EVENTKIT_MISSING_HINT)
    status = adapter.authorization_status()
    if status == macos_calendar.NOT_DETERMINED:
        # Never ask from here: the bot may run while nobody is at the Mac.
        return [], {}, macos_unconfigured("permission_not_determined", macos_calendar.NOT_DETERMINED_HINT)
    if status != macos_calendar.GRANTED:
        return [], {}, macos_unconfigured(f"permission_{status}", macos_calendar.permission_hint(status))
    start = datetime.combine(expand_start, time.min, tzinfo=tz)
    end = datetime.combine(expand_end, time.min, tzinfo=tz)
    events, warnings = macos_events(adapter, cfg.macos_calendars, tz, start, end)
    return events, ({"warnings": warnings} if warnings else {}), None


def run_schedule(
    date_str: str = "",
    days: int = 2,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
    fetcher: Fetcher | None = None,
    adapter_factory: AdapterFactory | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    cfg = config.load_calendar_config(env, platform=platform)
    if not cfg.configured:
        return unconfigured(cfg.missing, cfg.hint)

    tz = config.get_timezone(env)
    now = (now or utcnow()).astimezone(tz)
    try:
        start_day = parse_date_arg(date_str, tz, now)
    except ValueError:
        return {"configured": True, "ok": False, "source": cfg.source, "error": "date는 YYYY-MM-DD 형식이어야 합니다."}
    days = int_arg(days, 2, 1, MAX_DAYS)
    expand_start, expand_end = expand_window(start_day, days, now)

    if cfg.source == config.CALENDAR_SOURCE_MACOS:
        try:
            events, extra, early = _read_macos(cfg, tz, expand_start, expand_end, adapter_factory)
        except Exception as exc:  # noqa: BLE001 - reported to the model, never raised
            return {
                "configured": True,
                "ok": False,
                "source": cfg.source,
                "error": f"Mac 캘린더를 읽지 못했습니다: {safe_error(exc)}",
            }
        if early is not None:
            return early
        ok = True
    else:
        events, errors = ics_events(cfg.urls, tz, expand_start, expand_end, fetcher)
        ok = len(errors) < len(cfg.urls)
        extra = {"errors": errors} if errors else {}

    return {
        "configured": True,
        "ok": ok,
        "source": cfg.source,
        "timezone": config.get_timezone_name(env),
        **build_schedule(events, tz, start_day, days, now),
        **extra,
    }


@tool(
    "get_schedule",
    (
        "캘린더에서 date(YYYY-MM-DD, 비우면 오늘)부터 days일(기본 2: 오늘·내일)의 "
        "일정을 시작 시각 순으로 돌려준다. 캘린더는 Mac 캘린더 앱(source: macos) 또는 "
        "ICS 주소(source: ics)다. 각 일정: start, end, all_day, title, location, calendar "
        "(종일 일정의 end는 마지막 날 포함). 현재 진행 중인 일정(now), 바로 다음 일정(next_event), "
        "겹침(overlaps), 30분 이상 빈 시간(gaps)도 포함. warnings가 있으면 함께 전할 것. 읽기 전용. "
        "configured=false면 설정이나 권한이 없는 것이니 재시도하지 말 것."
    ),
    {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "default": "",
                "description": "시작 날짜 YYYY-MM-DD. 빈 문자열이면 TIMEZONE 기준 오늘.",
            },
            "days": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_DAYS,
                "default": 2,
                "description": "며칠치를 볼지 (기본 2 = 해당 날짜와 다음 날).",
            },
        },
        "required": [],
    },
    annotations=ToolAnnotations(readOnlyHint=True, maxResultSizeChars=MAX_RESULT_SIZE_CHARS),
)
async def get_schedule(args: dict[str, Any]) -> dict[str, Any]:
    date_str = str(args.get("date") or "")
    days = int_arg(args.get("days"), 2, 1, MAX_DAYS)
    try:
        payload = await asyncio.to_thread(run_schedule, date_str, days)
    except Exception as exc:  # noqa: BLE001 - last line of defence
        payload = {"configured": True, "ok": False, "error": safe_error(exc, redact_urls=True)}
    return tool_result(payload)
