"""Read events straight from the macOS Calendar app through EventKit (PyObjC),
and add events the user confirmed.

EventKit is imported lazily inside ``load_eventkit``, so the package still
imports on Linux and on a Mac without pyobjc. The calendar tool,
``--calendar-setup`` and the "add events from a note" flow only talk to a
small adapter (``authorization_status``, ``request_access``,
``list_calendars``, ``fetch_events``, ``list_writable_calendars``,
``create_event``, ``find_similar_events``); tests replace it with a fake.
``EventKitCalendar`` is the real one. Only code calls ``create_event``, and
only after the user said "네" (see ``event_proposals``); no agent tool can.

All EventKit calls block, so callers run them in a worker thread
(the ``get_schedule`` tool uses ``asyncio.to_thread``). Each adapter owns its
own ``EKEventStore`` and is used from one thread only.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from datetime import datetime, time, timedelta, tzinfo
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .. import config

# Permission states (EKAuthorizationStatus), as plain strings.
GRANTED = "granted"
DENIED = "denied"
RESTRICTED = "restricted"
NOT_DETERMINED = "not_determined"
WRITE_ONLY = "write_only"

REQUEST_TIMEOUT_SECONDS = 60

STATUS_LABELS = {
    GRANTED: "허용됨(전체 접근)",
    DENIED: "거부됨",
    RESTRICTED: "제한됨(기기 관리 정책 등)",
    NOT_DETERMINED: "아직 정하지 않음",
    WRITE_ONLY: "쓰기 전용(일정을 읽을 수 없음)",
}

SETTINGS_PATH = "시스템 설정 → 개인정보 보호 및 보안 → 캘린더"
NOT_DETERMINED_HINT = (
    "터미널에서 `python -m mungchi --calendar-setup`을 한 번 실행해 캘린더 접근을 허용하세요. "
    "그다음 봇을 다시 시작하세요. 봇을 백그라운드 서비스로 돌린다면 대신 `python -m mungchi service restart`를 "
    "실행하고 '비서실 고뭉치' 확인 창에서 허용을 누르세요."
)
EVENTKIT_MISSING_HINT = (
    "Mac 캘린더 앱을 읽는 데 필요한 pyobjc(EventKit)가 설치되어 있지 않습니다. "
    "가상환경을 켠 상태로 저장소 폴더에서 pip install -e . 를 다시 실행하세요 "
    "(또는 .env의 CALENDAR_ICS_URLS에 ICS 주소를 넣으세요)."
)


def permission_hint(status: str) -> str:
    """Korean fix for a permission state that does not allow reading events."""
    hint = (
        f"Mac 캘린더 접근이 허용되지 않았습니다(상태: {STATUS_LABELS.get(status, status)}). "
        f"{SETTINGS_PATH}에서 봇을 실행하는 앱(백그라운드 서비스면 '비서실 고뭉치', 터미널에서 띄웠으면 그 터미널 앱)을 "
        "'전체 접근'으로 바꾼 뒤 봇을 다시 시작하세요."
    )
    if status == RESTRICTED:
        hint += " 기기 관리 정책으로 막혀 있으면 바꿀 수 없으니 CALENDAR_ICS_URLS(ICS 주소)를 쓰세요."
    return hint


# Adding an event needs full access or write-only access.
WRITE_STATUSES = (GRANTED, WRITE_ONLY)


def write_permission_hint(status: str) -> str:
    """Korean fix for a permission state that does not allow adding events."""
    return NOT_DETERMINED_HINT if status == NOT_DETERMINED else permission_hint(status)


class EventKitUnavailable(RuntimeError):
    """EventKit (pyobjc-framework-EventKit) cannot be imported here."""


class CalendarAdapter(Protocol):
    """What the calendar tool, ``--calendar-setup`` and the note -> event flow need from the Calendar app."""

    def authorization_status(self) -> str: ...

    def request_access(self, timeout: float = REQUEST_TIMEOUT_SECONDS) -> bool: ...

    def list_calendars(self) -> list[dict[str, str]]: ...

    def fetch_events(
        self, start: datetime, end: datetime, names: Sequence[str] | None = None
    ) -> list[dict[str, Any]]: ...

    def list_writable_calendars(self) -> list[dict[str, Any]]: ...

    def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        all_day: bool,
        location: str | None = None,
        notes: str | None = None,
        calendar_name: str | None = None,
    ) -> dict[str, Any]: ...

    def find_similar_events(self, start: datetime, end: datetime, title: str) -> list[dict[str, Any]]: ...


# ---------------------------------------------------------------- pure helpers


def normalize_name(name: Any) -> str:
    """Calendar names compare after NFC normalization, case-insensitively.

    macOS can hand out Hangul decomposed (NFD) while .env holds it composed.
    """
    return " ".join(unicodedata.normalize("NFC", str(name or "")).split()).casefold()


def select_calendars(wanted: Sequence[str], available: Iterable[str]) -> tuple[list[str] | None, list[str]]:
    """Which calendars to read for ``MACOS_CALENDARS``.

    Returns ``(names, not_found)``. ``names`` is ``None`` for "every calendar"
    (nothing wanted), otherwise the matching names as the Calendar app writes
    them (possibly empty). ``not_found`` lists wanted names, as written, that
    match no calendar.
    """
    if not wanted:
        return None, []
    by_key: dict[str, list[str]] = {}
    for name in available:
        by_key.setdefault(normalize_name(name), []).append(str(name))
    selected: list[str] = []
    not_found: list[str] = []
    for name in wanted:
        matches = by_key.get(normalize_name(name))
        if not matches:
            not_found.append(name)
            continue
        selected.extend(m for m in matches if m not in selected)
    return selected, not_found


def status_name(code: Any, eventkit: Any = None) -> str:
    """Map an ``EKAuthorizationStatus`` value to a permission state.

    The EventKit constants are used when available. macOS 14+ reports 3 for
    full access (``EKAuthorizationStatusFullAccess``) and 4 for write-only;
    older systems report 3 as ``EKAuthorizationStatusAuthorized``.
    """

    def const(name: str, default: int) -> int:
        return int(getattr(eventkit, name, default))

    full_access = const("EKAuthorizationStatusFullAccess", const("EKAuthorizationStatusAuthorized", 3))
    table = {
        const("EKAuthorizationStatusNotDetermined", 0): NOT_DETERMINED,
        const("EKAuthorizationStatusRestricted", 1): RESTRICTED,
        const("EKAuthorizationStatusDenied", 2): DENIED,
        full_access: GRANTED,
        const("EKAuthorizationStatusWriteOnly", 4): WRITE_ONLY,
    }
    # An unknown future state never counts as permission to read.
    return table.get(int(code), DENIED)


def _local(timestamp: float, local_tz: tzinfo | None) -> datetime:
    # Without ``local_tz`` this is the Mac's own time zone (naive local time).
    return datetime.fromtimestamp(timestamp, local_tz) if local_tz is not None else datetime.fromtimestamp(timestamp)


def event_record(
    *,
    title: Any,
    start_ts: float,
    end_ts: float,
    all_day: bool,
    location: Any,
    calendar: Any,
    tz: tzinfo,
    local_tz: tzinfo | None = None,
) -> dict[str, Any]:
    """One event as the calendar tool expects it: aware datetimes in ``tz``.

    All-day events are floating: EventKit reports them at midnight of the
    Mac's own time zone (``local_tz``, default: the system zone), and the end
    may be the last second of the last day or the midnight after it. They are
    rebuilt like the ICS path does it: start at midnight of the first day and
    end at the (exclusive) midnight after the last day, both in ``tz``.
    """
    if all_day:
        first = _local(start_ts, local_tz).date()
        end_local = _local(end_ts, local_tz)
        last = end_local.date()
        if end_local.time() == time.min:  # exclusive end at midnight
            last -= timedelta(days=1)
        last = max(last, first)
        start = datetime.combine(first, time.min, tzinfo=tz)
        end = datetime.combine(last + timedelta(days=1), time.min, tzinfo=tz)
    else:
        start = datetime.fromtimestamp(start_ts, tz)
        end = max(datetime.fromtimestamp(end_ts, tz), start)
    return {
        "title": str(title or "").strip(),
        "start": start,
        "end": end,
        "all_day": bool(all_day),
        "location": str(location or "").strip(),
        "calendar": str(calendar or ""),
    }


# ---------------------------------------------------------------- pure helpers: adding events


def resolve_write_calendar(
    writable: Sequence[Mapping[str, Any]], calendar_name: str | None, target: str | None = None
) -> tuple[str | None, str | None]:
    """Which calendar a new event goes to: ``(name, error)``.

    ``calendar_name`` first, else ``target`` (``CALENDAR_WRITE_TARGET``), both
    matched NFC and case-insensitively against the writable calendars and
    returned as the Calendar app writes them. Neither: the calendar marked
    ``is_default``, or ``None`` for "the store's default calendar". An unknown
    name is an error that lists the calendars events can be added to.
    """
    for wanted, setting in ((calendar_name, ""), (target, "CALENDAR_WRITE_TARGET")):
        wanted = " ".join(str(wanted or "").split())
        if not wanted:
            continue
        for calendar in writable:
            if normalize_name(calendar.get("name")) == normalize_name(wanted):
                return str(calendar.get("name") or ""), None
        names = ", ".join(str(c.get("name") or "") for c in writable) or "(없음)"
        where = f"{setting}의 " if setting else ""
        return None, (
            f"{where}'{wanted}' 캘린더가 없거나 일정을 추가할 수 없는 캘린더예요. "
            f"일정을 추가할 수 있는 캘린더: {names}"
        )
    for calendar in writable:
        if calendar.get("is_default"):
            return str(calendar.get("name") or ""), None
    return None, None


# Duplicate check: events this close to the proposed one (same day only) are compared.
DUPLICATE_WINDOW = timedelta(hours=2)
# Words too generic to call two titles similar on their own.
_GENERIC_TITLE_WORDS = frozenset({"회의", "모임", "미팅", "일정", "약속", "meeting", "event", "the", "and"})
_TITLE_WORD_RE = re.compile(r"[^\W_]+")
# "10월", "2차", "3회" and plain numbers say nothing about what the event is.
_COUNTER_WORD_RE = re.compile(r"\d+(?:월|일|차|회|시|분|년|주|번)?")


def title_tokens(title: Any) -> set[str]:
    """Meaningful words of a title: NFC, case-insensitive, without generic words and counters."""
    words = _TITLE_WORD_RE.findall(normalize_name(title))
    return {w for w in words if len(w) >= 2 and w not in _GENERIC_TITLE_WORDS and not _COUNTER_WORD_RE.fullmatch(w)}


def _compact_title(title: Any) -> str:
    return "".join(_TITLE_WORD_RE.findall(normalize_name(title)))


def titles_similar(first: Any, second: Any) -> bool:
    """True when two titles share a meaningful word, or one (3+ letters, spaces ignored) contains the other."""
    if title_tokens(first) & title_tokens(second):
        return True
    shorter, longer = sorted((_compact_title(first), _compact_title(second)), key=len)
    return len(shorter) >= 3 and shorter not in _GENERIC_TITLE_WORDS and shorter in longer


def duplicate_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """Two hours either side of ``[start, end)``, never leaving the day ``start`` is on."""
    day_start = datetime.combine(start.date(), time.min, tzinfo=start.tzinfo)
    day_end = day_start + timedelta(days=1)
    return max(start - DUPLICATE_WINDOW, day_start), min(max(end, start) + DUPLICATE_WINDOW, day_end)


def similar_events(
    records: Iterable[Mapping[str, Any]], start: datetime, end: datetime, title: str
) -> list[dict[str, Any]]:
    """Records (``event_record`` shape) that look like the proposed event: a similar title or the exact same times."""
    window_start, window_end = duplicate_window(start, end)
    found: list[dict[str, Any]] = []
    for record in records:
        r_start, r_end = record.get("start"), record.get("end")
        if not isinstance(r_start, datetime) or not isinstance(r_end, datetime):
            continue
        # Zero-length events count when they start inside the window.
        inside = r_end > window_start if r_end > r_start else r_start >= window_start
        if not (r_start < window_end and inside):
            continue
        # Exact times only count for timed events: every all-day event spans the whole day.
        same_time = not record.get("all_day") and r_start == start and r_end == end
        if same_time or titles_similar(record.get("title"), title):
            found.append(dict(record))
    return found


def save_outcome(result: Any) -> tuple[bool, Any]:
    """``(ok, NSError or None)`` from ``saveEvent:span:commit:error:``.

    PyObjC returns ``(BOOL, NSError)`` for a method with an ``NSError **``
    out-parameter; a bare BOOL is accepted as well.
    """
    if isinstance(result, (tuple, list)):
        ok = bool(result[0]) if result else False
        error = result[1] if len(result) > 1 else None
        return ok, error
    return bool(result), None


def error_text(error: Any) -> str:
    """An NSError's localized description (or ``str(error)``), one line."""
    if error is None:
        return ""
    try:
        text = error.localizedDescription()
    except Exception:  # noqa: BLE001 - not an NSError
        text = error
    return " ".join(str(text or "").split())


def _create_failure(error: str, calendar: str = "") -> dict[str, Any]:
    return {"ok": False, "id": None, "calendar": calendar, "error": error}


# ---------------------------------------------------------------- EventKit


def load_eventkit() -> tuple[Any, Any]:
    """``(EventKit, Foundation)`` from PyObjC, imported only when needed."""
    try:
        import EventKit  # type: ignore[import-not-found]
        import Foundation  # type: ignore[import-not-found]
    except ImportError as exc:  # Linux, or pyobjc not installed on the Mac
        raise EventKitUnavailable(EVENTKIT_MISSING_HINT) from exc
    return EventKit, Foundation


def _text(value: Any) -> str:
    return str(value) if value is not None else ""


def _responds_to(obj: Any, selector: str) -> bool:
    try:
        return bool(obj.respondsToSelector_(selector))
    except Exception:  # noqa: BLE001 - fall back to PyObjC's own method lookup
        return hasattr(obj, selector.replace(":", "_"))


class EventKitCalendar:
    """The macOS Calendar app through EventKit (macOS only)."""

    def __init__(
        self,
        tz: tzinfo,
        *,
        eventkit: Any = None,
        foundation: Any = None,
        local_tz: tzinfo | None = None,
        env: Mapping[str, str] | None = None,
    ):
        if eventkit is None or foundation is None:
            eventkit, foundation = load_eventkit()
        self._ek = eventkit
        self._ns = foundation
        self._tz = tz
        self._local_tz = local_tz
        self._env = env
        self._store = self._new_store()

    def _new_store(self) -> Any:
        return self._ek.EKEventStore.alloc().init()

    def authorization_status(self) -> str:
        code = self._ek.EKEventStore.authorizationStatusForEntityType_(self._ek.EKEntityTypeEvent)
        return status_name(code, self._ek)

    def request_access(self, timeout: float = REQUEST_TIMEOUT_SECONDS) -> bool:
        """Ask macOS for full calendar access (shows the system dialog once)."""
        done = threading.Event()
        outcome = {"granted": False}

        def completion(granted: Any, error: Any) -> None:  # called by EventKit on a background queue
            outcome["granted"] = bool(granted)
            done.set()

        if _responds_to(self._store, "requestFullAccessToEventsWithCompletion:"):  # macOS 14+
            self._store.requestFullAccessToEventsWithCompletion_(completion)
        else:
            self._store.requestAccessToEntityType_completion_(self._ek.EKEntityTypeEvent, completion)
        if not done.wait(timeout):
            return False
        if outcome["granted"]:
            # A store created before access was granted sees no calendars.
            self._store = self._new_store()
        return outcome["granted"]

    def _calendars(self) -> list[Any]:
        return list(self._store.calendarsForEntityType_(self._ek.EKEntityTypeEvent) or [])

    def list_calendars(self) -> list[dict[str, str]]:
        calendars = []
        for calendar in self._calendars():
            source = calendar.source()
            calendars.append(
                {"name": _text(calendar.title()), "source": _text(source.title()) if source is not None else ""}
            )
        return calendars

    def fetch_events(
        self, start: datetime, end: datetime, names: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """Events overlapping ``[start, end)``; recurring events come expanded."""
        calendars = None  # nil: every calendar
        if names is not None:
            wanted = {normalize_name(name) for name in names}
            calendars = [c for c in self._calendars() if normalize_name(_text(c.title())) in wanted]
            if not calendars:
                return []  # passing nil would read every calendar instead
        nsdate = self._ns.NSDate
        predicate = self._store.predicateForEventsWithStartDate_endDate_calendars_(
            nsdate.dateWithTimeIntervalSince1970_(start.timestamp()),
            nsdate.dateWithTimeIntervalSince1970_(end.timestamp()),
            calendars,
        )
        canceled = int(getattr(self._ek, "EKEventStatusCanceled", 3))
        records = []
        for event in self._store.eventsMatchingPredicate_(predicate) or []:
            start_date, end_date = event.startDate(), event.endDate()
            if start_date is None or event.status() == canceled:
                continue
            calendar = event.calendar()
            records.append(
                event_record(
                    title=event.title(),
                    start_ts=start_date.timeIntervalSince1970(),
                    end_ts=(end_date or start_date).timeIntervalSince1970(),
                    all_day=bool(event.isAllDay()),
                    location=event.location(),
                    calendar=_text(calendar.title()) if calendar is not None else "",
                    tz=self._tz,
                    local_tz=self._local_tz,
                )
            )
        return records

    # -- adding events (only ever called by code, after the user confirmed)

    def _default_calendar(self) -> Any:
        try:
            return self._store.defaultCalendarForNewEvents()
        except Exception:  # noqa: BLE001 - no default calendar: the caller reports it
            return None

    def _writable(self) -> list[tuple[Any, dict[str, Any]]]:
        """``(EKCalendar, {name, source, is_default})`` for calendars that accept new events."""
        default = self._default_calendar()
        default_id = _calendar_id(default)
        writable = []
        for calendar in self._calendars():
            if not _allows_modifications(calendar):
                continue
            source = calendar.source()
            if default is None:
                is_default = False
            elif default_id:
                is_default = _calendar_id(calendar) == default_id
            else:
                is_default = calendar == default
            info = {
                "name": _text(calendar.title()),
                "source": _text(source.title()) if source is not None else "",
                "is_default": bool(is_default),
            }
            writable.append((calendar, info))
        return writable

    def list_writable_calendars(self) -> list[dict[str, Any]]:
        """Calendars new events can go to: ``[{name, source, is_default}]``."""
        return [info for _calendar, info in self._writable()]

    def _nsdate(self, timestamp: float) -> Any:
        return self._ns.NSDate.dateWithTimeIntervalSince1970_(timestamp)

    def _local_midnight(self, day: Any) -> float:
        # All-day events are floating: midnight in the Mac's own zone picks the day.
        if self._local_tz is not None:
            return datetime.combine(day, time.min, tzinfo=self._local_tz).timestamp()
        return datetime.combine(day, time.min).timestamp()

    def _event_dates(self, start: datetime, end: datetime, all_day: bool) -> tuple[Any, Any]:
        """NSDates for a new event. ``end`` is exclusive; an all-day event ends on its last day (EventKit style)."""
        if not all_day:
            return self._nsdate(start.timestamp()), self._nsdate(max(end, start).timestamp())
        first = start.astimezone(self._tz).date()
        last = (end.astimezone(self._tz) - timedelta(days=1)).date() if end > start else first
        last = max(last, first)
        return self._nsdate(self._local_midnight(first)), self._nsdate(self._local_midnight(last))

    def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        all_day: bool,
        location: str | None = None,
        notes: str | None = None,
        calendar_name: str | None = None,
    ) -> dict[str, Any]:
        """Add one event: ``{"ok", "id", "calendar", "error"}``. Never raises.

        The calendar is ``calendar_name``, else ``CALENDAR_WRITE_TARGET``, else
        the default calendar for new events. ``end`` is exclusive (an all-day
        event on one day ends at the next midnight, like ``event_record``).
        """
        try:
            status = self.authorization_status()
            if status not in WRITE_STATUSES:
                return _create_failure(write_permission_hint(status))
            writable = self._writable()
            name, error = resolve_write_calendar(
                [info for _calendar, info in writable], calendar_name, config.get_calendar_write_target(self._env)
            )
            if error:
                return _create_failure(error)
            if name is None:
                calendar = self._default_calendar()
            else:
                calendar = next(c for c, info in writable if info["name"] == name)
            if calendar is None:
                return _create_failure(
                    "일정을 추가할 기본 캘린더를 찾지 못했어요. .env의 CALENDAR_WRITE_TARGET에 캘린더 이름을 적어 주세요."
                )
            label = _text(calendar.title())
            ek_start, ek_end = self._event_dates(start, end, all_day)
            event = self._ek.EKEvent.eventWithEventStore_(self._store)
            event.setTitle_(title)
            event.setStartDate_(ek_start)
            event.setEndDate_(ek_end)
            event.setAllDay_(bool(all_day))
            if location:
                event.setLocation_(location)
            if notes:
                event.setNotes_(notes)
            event.setCalendar_(calendar)
            span = int(getattr(self._ek, "EKSpanThisEvent", 0))
            ok, save_error = save_outcome(self._store.saveEvent_span_commit_error_(event, span, True, None))
            if not ok:
                detail = error_text(save_error) or "알 수 없는 오류"
                return _create_failure(f"캘린더에 저장하지 못했어요 ({detail})", label)
            return {"ok": True, "id": _text(event.eventIdentifier()) or None, "calendar": label, "error": None}
        except Exception as exc:  # noqa: BLE001 - reported per event, never raised
            detail = " ".join(f"{type(exc).__name__}: {exc}".split())[:300]
            return _create_failure(f"캘린더에 저장하지 못했어요 ({detail})")

    def find_similar_events(self, start: datetime, end: datetime, title: str) -> list[dict[str, Any]]:
        """Existing events (every calendar) within two hours of ``[start, end)`` on that day that look the same."""
        window_start, window_end = duplicate_window(start, end)
        return similar_events(self.fetch_events(window_start, window_end), start, end, title)


def _calendar_id(calendar: Any) -> str:
    if calendar is None:
        return ""
    try:
        return _text(calendar.calendarIdentifier())
    except Exception:  # noqa: BLE001 - compare the objects instead
        return ""


def _allows_modifications(calendar: Any) -> bool:
    try:
        return bool(calendar.allowsContentModifications())
    except Exception:  # noqa: BLE001 - unknown: never offer it
        return False


def default_adapter(tz: tzinfo) -> EventKitCalendar:
    """The real adapter; raises ``EventKitUnavailable`` without pyobjc."""
    return EventKitCalendar(tz)
