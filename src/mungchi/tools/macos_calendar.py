"""Read events straight from the macOS Calendar app through EventKit (PyObjC).

EventKit is imported lazily inside ``load_eventkit``, so the package still
imports on Linux and on a Mac without pyobjc. The calendar tool and
``--calendar-setup`` only talk to a small adapter (``authorization_status``,
``request_access``, ``list_calendars``, ``fetch_events``); tests replace it
with a fake. ``EventKitCalendar`` is the real one.

All EventKit calls block, so callers run them in a worker thread
(the ``get_schedule`` tool uses ``asyncio.to_thread``). Each adapter owns its
own ``EKEventStore`` and is used from one thread only.
"""

from __future__ import annotations

import threading
import unicodedata
from datetime import datetime, time, timedelta, tzinfo
from typing import Any, Iterable, Protocol, Sequence

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


class EventKitUnavailable(RuntimeError):
    """EventKit (pyobjc-framework-EventKit) cannot be imported here."""


class CalendarAdapter(Protocol):
    """What the calendar tool and ``--calendar-setup`` need from the Calendar app."""

    def authorization_status(self) -> str: ...

    def request_access(self, timeout: float = REQUEST_TIMEOUT_SECONDS) -> bool: ...

    def list_calendars(self) -> list[dict[str, str]]: ...

    def fetch_events(
        self, start: datetime, end: datetime, names: Sequence[str] | None = None
    ) -> list[dict[str, Any]]: ...


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
    ):
        if eventkit is None or foundation is None:
            eventkit, foundation = load_eventkit()
        self._ek = eventkit
        self._ns = foundation
        self._tz = tz
        self._local_tz = local_tz
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


def default_adapter(tz: tzinfo) -> EventKitCalendar:
    """The real adapter; raises ``EventKitUnavailable`` without pyobjc."""
    return EventKitCalendar(tz)
