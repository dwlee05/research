"""The macOS Calendar app source (EventKit) and ``--calendar-setup``, without macOS.

The calendar tool and the setup command are driven through a fake adapter.
The real adapter (``EventKitCalendar``) is driven through fake EventKit and
Foundation objects that answer the same selectors PyObjC exposes.
"""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from mungchi import calendar_setup, config
from mungchi.calendar_setup import run_calendar_setup
from mungchi.main import build_parser, main
from mungchi.tools import calendar_tool, macos_calendar
from mungchi.tools.calendar_tool import adapter_event, get_schedule, run_schedule
from mungchi.tools.macos_calendar import (
    DENIED,
    GRANTED,
    NOT_DETERMINED,
    RESTRICTED,
    WRITE_ONLY,
    EventKitCalendar,
    EventKitUnavailable,
    event_record,
    load_eventkit,
    select_calendars,
    status_name,
)

SEOUL = ZoneInfo("Asia/Seoul")
# Monday 2026-10-05, 10:30 in Seoul.
CLOCK = datetime(2026, 10, 5, 10, 30, tzinfo=SEOUL)
URL = "https://calendar.google.com/calendar/ical/me%40gmail.com/private-0123456789abcdef/basic.ics"
ENV = {"TIMEZONE": "Asia/Seoul"}
SETTINGS_PATH = "시스템 설정 → 개인정보 보호 및 보안 → 캘린더"


def at(day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=SEOUL)


def record(title, start, end, calendar="연구", location="", all_day=False):
    """One event as an adapter returns it (``macos_calendar.event_record`` shape)."""
    return {"title": title, "start": start, "end": end, "all_day": all_day, "location": location, "calendar": calendar}


# The same events as ICS below: a weekly series, an all-day event and two meetings.
RECORDS = [
    record("학회 초록 마감", at(5), at(6), all_day=True),
    record("지도교수 면담", at(5, 13), at(5, 14, 30), location="교수 연구실"),
    record("수업", at(6, 9), at(6, 10)),
    record("창 밖 일정", at(9, 9), at(9, 10)),
]
# (title, first start, duration, calendar, location): expanded per request like EventKit does.
SERIES = [("주간 랩미팅", datetime(2026, 9, 7, 10, 0, tzinfo=SEOUL), timedelta(hours=1), "연구", "302호")]
CALENDARS = [{"name": "연구", "source": "iCloud"}, {"name": "Work", "source": "iCloud"}, {"name": "생일", "source": "기타"}]

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//mungchi tests//EN
X-WR-CALNAME:연구
BEGIN:VEVENT
UID:weekly-1
DTSTART;TZID=Asia/Seoul:20260907T100000
DTEND;TZID=Asia/Seoul:20260907T110000
RRULE:FREQ=WEEKLY;BYDAY=MO
SUMMARY:주간 랩미팅
LOCATION:302호
END:VEVENT
BEGIN:VEVENT
UID:allday-1
DTSTART;VALUE=DATE:20261005
DTEND;VALUE=DATE:20261006
SUMMARY:학회 초록 마감
END:VEVENT
BEGIN:VEVENT
UID:meeting-1
DTSTART;TZID=Asia/Seoul:20261005T130000
DTEND;TZID=Asia/Seoul:20261005T143000
SUMMARY:지도교수 면담
LOCATION:교수 연구실
END:VEVENT
BEGIN:VEVENT
UID:class-1
DTSTART;TZID=Asia/Seoul:20261006T090000
DTEND;TZID=Asia/Seoul:20261006T100000
SUMMARY:수업
END:VEVENT
BEGIN:VEVENT
UID:later-1
DTSTART;TZID=Asia/Seoul:20261009T090000
DTEND;TZID=Asia/Seoul:20261009T100000
SUMMARY:창 밖 일정
END:VEVENT
END:VCALENDAR
"""


class FakeAdapter:
    """Stands in for the Calendar app. ``fetch_events`` ignores ``names`` on
    purpose: the tool must keep only the selected calendars itself."""

    def __init__(self, status=GRANTED, calendars=CALENDARS, records=RECORDS, series=SERIES, grant=True):
        self.status = status
        self.calendars = calendars
        self.records = records
        self.series = series
        self.grant = grant
        self.calls: list[str] = []
        self.fetches: list[tuple] = []
        self.threads: set[str] = set()

    def authorization_status(self):
        self.calls.append("status")
        return self.status

    def request_access(self, timeout=60):
        self.calls.append("request")
        if self.grant is None:  # nobody answered the dialog
            return False
        self.status = GRANTED if self.grant else DENIED
        return self.grant

    def list_calendars(self):
        self.calls.append("list")
        return [dict(c) for c in self.calendars]

    def list_writable_calendars(self):
        self.calls.append("writable")
        return [{**c, "is_default": i == 0} for i, c in enumerate(self.calendars) if c["name"] != "생일"]

    def fetch_events(self, start, end, names=None):
        self.calls.append("fetch")
        self.threads.add(threading.current_thread().name)
        self.fetches.append((start, end, None if names is None else list(names)))
        found = [dict(r) for r in self.records if r["start"] < end and r["end"] > start]
        for title, first, duration, calendar, location in self.series:
            occurrence = first
            while occurrence < end:
                if occurrence + duration > start:
                    found.append(record(title, occurrence, occurrence + duration, calendar, location))
                occurrence += timedelta(weeks=1)
        return found


def run_mac(adapter, env=None, date_str="", days=2):
    return run_schedule(
        date_str, days, env={**ENV, **(env or {})}, now=CLOCK, platform="darwin", adapter_factory=lambda tz: adapter
    )


# ---------------------------------------------------------------- source selection


@pytest.mark.parametrize(
    "env, platform, source",
    [
        ({}, "darwin", "macos"),
        ({"CALENDAR_SOURCE": "AUTO"}, "darwin", "macos"),
        ({"CALENDAR_ICS_URLS": URL}, "darwin", "ics"),  # auto: ICS addresses win
        ({"CALENDAR_ICS_URLS": URL}, "linux", "ics"),
        ({"CALENDAR_SOURCE": " MacOS ", "CALENDAR_ICS_URLS": URL}, "darwin", "macos"),
        ({"CALENDAR_SOURCE": "ics", "CALENDAR_ICS_URLS": URL}, "darwin", "ics"),
    ],
)
def test_calendar_source_selection(env, platform, source):
    cfg = config.load_calendar_config(env, platform=platform)
    assert cfg.configured and cfg.source == source and cfg.missing == []


def test_calendar_source_that_cannot_be_used_explains_why():
    off_mac = config.load_calendar_config({}, platform="linux")
    assert not off_mac.configured and off_mac.missing == ["CALENDAR_ICS_URLS"]
    assert "--calendar-setup" in off_mac.hint and "macOS가 아니" in off_mac.hint and "iCal" in off_mac.hint

    macos_off_mac = config.load_calendar_config({"CALENDAR_SOURCE": "macos"}, platform="linux")
    assert not macos_off_mac.configured and macos_off_mac.missing == ["CALENDAR_ICS_URLS"]
    assert "macOS에서만" in macos_off_mac.hint and "CALENDAR_ICS_URLS" in macos_off_mac.hint

    ics_without_urls = config.load_calendar_config({"CALENDAR_SOURCE": "ics"}, platform="darwin")
    assert not ics_without_urls.configured and ics_without_urls.missing == ["CALENDAR_ICS_URLS"]

    typo = config.load_calendar_config({"CALENDAR_SOURCE": "outlook"}, platform="darwin")
    assert not typo.configured and typo.missing == []
    assert "'outlook'" in typo.hint and "auto(기본), macos, ics" in typo.hint


def test_default_platform_comes_from_current_platform(monkeypatch):
    assert config.load_calendar_config({}).source == ""  # conftest: not a Mac
    monkeypatch.setattr(config, "current_platform", lambda: "darwin")
    assert config.load_calendar_config({}).source == "macos"


def test_off_mac_the_calendar_app_is_never_touched():
    def factory(tz):
        raise AssertionError("EventKit must not be used off macOS")

    payload = run_schedule(env={"CALENDAR_SOURCE": "macos"}, now=CLOCK, platform="linux", adapter_factory=factory)
    assert payload == {"configured": False, "missing": ["CALENDAR_ICS_URLS"], "hint": payload["hint"]}


# ---------------------------------------------------------------- same output for both sources


def test_macos_and_ics_sources_give_identical_output():
    ics = run_schedule("", 2, env={**ENV, "CALENDAR_ICS_URLS": URL}, now=CLOCK, fetcher=lambda url: ICS.encode())
    adapter = FakeAdapter()
    mac = run_mac(adapter)
    assert ics["source"] == "ics" and mac["source"] == "macos"
    assert list(ics) == list(mac)  # same keys in the same order
    assert {k: v for k, v in ics.items() if k != "source"} == {k: v for k, v in mac.items() if k != "source"}
    assert [e["title"] for e in mac["events"]] == ["학회 초록 마감", "주간 랩미팅", "지도교수 면담", "수업"]
    assert mac["events"][0] == {
        "start": "2026-10-05",
        "end": "2026-10-05",
        "all_day": True,
        "title": "학회 초록 마감",
        "location": "",
        "calendar": "연구",
    }
    # now / next_event come from the injected clock (10:30).
    assert mac["current_time"] == "2026-10-05T10:30+09:00"
    assert {e["title"] for e in mac["now"]} == {"주간 랩미팅", "학회 초록 마감"}
    assert mac["next_event"]["title"] == "지도교수 면담"
    assert mac["gaps"] == [{"date": "2026-10-05", "from": "11:00", "to": "13:00", "minutes": 120}]
    # One read of today..day after tomorrow, every calendar.
    assert adapter.fetches == [(at(5), at(7), None)]


def test_recurring_and_multi_day_all_day_events_from_the_calendar_app():
    records = [record("학회", at(12), at(15), all_day=True), *RECORDS]
    adapter = FakeAdapter(records=records)
    payload = run_mac(adapter, date_str="2026-10-05", days=14)
    assert adapter.fetches == [(at(5), at(19), None)]
    weekly = [e["start"] for e in payload["events"] if e["title"] == "주간 랩미팅"]
    assert weekly == ["2026-10-05T10:00+09:00", "2026-10-12T10:00+09:00"]
    conference = next(e for e in payload["events"] if e["title"] == "학회")
    assert (conference["start"], conference["end"], conference["all_day"]) == ("2026-10-12", "2026-10-14", True)
    assert payload["range"] == {"start": "2026-10-05", "end": "2026-10-18", "days": 14}


def test_missing_title_and_location_are_filled_like_ics():
    payload = run_mac(FakeAdapter(records=[record(None, at(5, 15), at(5, 16), location=None)], series=[]))
    [event] = payload["events"]
    assert event["title"] == "(제목 없음)" and event["location"] == ""


# ---------------------------------------------------------------- permission states


def test_not_determined_never_prompts_from_the_tool():
    adapter = FakeAdapter(status=NOT_DETERMINED)
    payload = run_mac(adapter)
    assert payload == {
        "configured": False,
        "missing": [],
        "hint": payload["hint"],
        "source": "macos",
        "reason": "permission_not_determined",
    }
    assert "터미널에서 `python -m mungchi --calendar-setup`을 한 번 실행해 캘린더 접근을 허용하세요" in payload["hint"]
    assert adapter.calls == ["status"]  # no request_access: nobody may be at the Mac


@pytest.mark.parametrize("status", [DENIED, RESTRICTED, WRITE_ONLY])
def test_no_read_permission_points_to_system_settings(status):
    adapter = FakeAdapter(status=status)
    payload = run_mac(adapter)
    assert payload["configured"] is False and payload["reason"] == f"permission_{status}"
    assert SETTINGS_PATH in payload["hint"] and "'전체 접근'" in payload["hint"] and "다시 시작" in payload["hint"]
    assert adapter.calls == ["status"]


def test_missing_pyobjc_on_a_mac_points_to_pip_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "EventKit", None)  # makes ``import EventKit`` fail, also on a Mac
    with pytest.raises(EventKitUnavailable):
        load_eventkit()
    payload = run_schedule(env=ENV, now=CLOCK, platform="darwin")  # the real default adapter
    assert payload["configured"] is False and payload["reason"] == "eventkit_missing"
    assert "pip install -e ." in payload["hint"]


def test_calendar_app_errors_are_reported_not_raised():
    class Broken(FakeAdapter):
        def list_calendars(self):
            raise RuntimeError("EKErrorDomain error 1")

    payload = run_mac(Broken())
    assert payload["configured"] is True and payload["ok"] is False and payload["source"] == "macos"
    assert payload["error"].startswith("Mac 캘린더를 읽지 못했습니다") and "EKErrorDomain" in payload["error"]


# ---------------------------------------------------------------- MACOS_CALENDARS


def test_calendar_filter_matches_nfc_and_case_and_warns_about_missing_names():
    nfd = unicodedata.normalize("NFD", "연구")  # as macOS may report it
    assert nfd != "연구"
    calendars = [
        {"name": nfd, "source": "iCloud"},
        {"name": "Work", "source": "iCloud"},
        {"name": "가족", "source": "iCloud"},
    ]
    records = [
        record("논문 회의", at(5, 15), at(5, 16), calendar=nfd),
        record("Standup", at(5, 9), at(5, 9, 15), calendar="Work"),
        record("가족 저녁", at(5, 19), at(5, 21), calendar="가족"),
    ]
    adapter = FakeAdapter(calendars=calendars, records=records, series=[])
    payload = run_mac(adapter, {"MACOS_CALENDARS": "연구, WORK ,없는 캘린더"})
    assert [e["title"] for e in payload["events"]] == ["Standup", "논문 회의"]
    assert adapter.fetches[0][2] == [nfd, "Work"]  # names as the Calendar app writes them
    [warning] = payload["warnings"]
    assert "MACOS_CALENDARS" in warning and "'없는 캘린더'" in warning
    assert payload["ok"] is True


def test_when_no_listed_calendar_exists_nothing_is_read():
    adapter = FakeAdapter()
    payload = run_mac(adapter, {"MACOS_CALENDARS": "없음"})
    assert payload["events"] == [] and payload["next_event"] is None and len(payload["warnings"]) == 1
    assert adapter.fetches == []  # never "every calendar" instead


def test_select_calendars():
    assert select_calendars([], ["A"]) == (None, [])
    assert select_calendars(["a", "Ｂ"], ["A", "B", "A"]) == (["A"], ["Ｂ"])
    composed, decomposed = "학회", unicodedata.normalize("NFD", "학회")
    assert select_calendars([decomposed], [composed]) == ([composed], [])
    assert select_calendars(["  업무  일정 "], ["업무 일정"]) == (["업무 일정"], [])


# ---------------------------------------------------------------- pure EventKit helpers


def test_status_name_uses_eventkit_constants():
    macos14 = SimpleNamespace(
        EKAuthorizationStatusNotDetermined=0,
        EKAuthorizationStatusRestricted=1,
        EKAuthorizationStatusDenied=2,
        EKAuthorizationStatusAuthorized=3,
        EKAuthorizationStatusFullAccess=3,
        EKAuthorizationStatusWriteOnly=4,
    )
    assert [status_name(code, macos14) for code in range(5)] == [NOT_DETERMINED, RESTRICTED, DENIED, GRANTED, WRITE_ONLY]
    older = SimpleNamespace(
        EKAuthorizationStatusNotDetermined=0,
        EKAuthorizationStatusRestricted=1,
        EKAuthorizationStatusDenied=2,
        EKAuthorizationStatusAuthorized=3,
    )
    assert status_name(3, older) == GRANTED
    assert status_name(3) == GRANTED  # no constants: the documented numbers
    assert status_name(99, macos14) == DENIED  # unknown states never allow reading


def test_event_record_all_day_boundaries_match_the_ics_path():
    last_second = at(5, 23, 59).timestamp() + 59  # EventKit style: end at 23:59:59 of the last day
    one_day = event_record(
        title=None, start_ts=at(5).timestamp(), end_ts=last_second, all_day=True,
        location=None, calendar="연구", tz=SEOUL, local_tz=SEOUL,
    )
    assert one_day == {"title": "", "start": at(5), "end": at(6), "all_day": True, "location": "", "calendar": "연구"}
    three_days = event_record(
        title="학회", start_ts=at(5).timestamp(), end_ts=at(8).timestamp(), all_day=True,
        location="", calendar="연구", tz=SEOUL, local_tz=SEOUL,
    )
    assert (three_days["start"], three_days["end"]) == (at(5), at(8))
    # All-day events are floating: the Mac's own zone decides the date, TIMEZONE only the output.
    new_york = ZoneInfo("America/New_York")
    elsewhere = event_record(
        title="학회", start_ts=datetime(2026, 10, 5, tzinfo=new_york).timestamp(),
        end_ts=datetime(2026, 10, 6, tzinfo=new_york).timestamp(), all_day=True,
        location="", calendar="연구", tz=SEOUL, local_tz=new_york,
    )
    assert (elsewhere["start"], elsewhere["end"]) == (at(5), at(6))
    # Same dict as an ICS all-day event once it is an Event.
    assert adapter_event(one_day, SEOUL).to_dict() == {
        "start": "2026-10-05", "end": "2026-10-05", "all_day": True,
        "title": "(제목 없음)", "location": "", "calendar": "연구",
    }


def test_event_record_timed_events_are_in_timezone():
    start = datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)
    timed = event_record(
        title=" 세미나 ", start_ts=start.timestamp(), end_ts=(start + timedelta(hours=1)).timestamp(),
        all_day=False, location=" 302호 ", calendar="연구", tz=SEOUL,
    )
    assert (timed["start"], timed["end"]) == (at(5, 13), at(5, 14))
    assert timed["start"].tzinfo == SEOUL and timed["title"] == "세미나" and timed["location"] == "302호"
    backwards = event_record(
        title="x", start_ts=start.timestamp(), end_ts=start.timestamp() - 60, all_day=False,
        location="", calendar="", tz=SEOUL,
    )
    assert backwards["end"] == backwards["start"]


# ---------------------------------------------------------------- the real adapter, on fake EventKit objects


class FakeNSDate:
    def __init__(self, ts):
        self.ts = ts

    @classmethod
    def dateWithTimeIntervalSince1970_(cls, ts):
        return cls(ts)

    def timeIntervalSince1970(self):
        return self.ts


class FakeSource:
    def __init__(self, title):
        self._title = title

    def title(self):
        return self._title


class FakeCalendar:
    def __init__(self, title, source):
        self._title, self._source = title, FakeSource(source)

    def title(self):
        return self._title

    def source(self):
        return self._source


class FakeEvent:
    def __init__(self, title, start, end, calendar, all_day=False, location=None, status=1):
        self._title, self._calendar, self._all_day, self._location, self._status = title, calendar, all_day, location, status
        self._start, self._end = FakeNSDate(start.timestamp()), FakeNSDate(end.timestamp())

    def title(self):
        return self._title

    def startDate(self):
        return self._start

    def endDate(self):
        return self._end

    def isAllDay(self):
        return self._all_day

    def location(self):
        return self._location

    def calendar(self):
        return self._calendar

    def status(self):
        return self._status


class World:
    """What macOS knows: the permission, the calendars, the events."""

    def __init__(self, status=0, modern=True, answer=True, respond=True):
        self.status, self.modern, self.answer, self.respond = status, modern, answer, respond
        self.stores: list = []
        self.requests: list = []
        self.predicates: list = []
        self.calendars: list = []
        self.events: list = []


def fake_eventkit(world: World) -> SimpleNamespace:
    class EKEventStore:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            # Like EventKit: a store created before access was granted sees no calendars.
            self.sees_calendars = world.status == 3
            world.stores.append(self)
            return self

        @classmethod
        def authorizationStatusForEntityType_(cls, entity_type):
            assert entity_type == 0
            return world.status

        def respondsToSelector_(self, selector):
            return world.modern or selector != "requestFullAccessToEventsWithCompletion:"

        def requestFullAccessToEventsWithCompletion_(self, completion):
            world.requests.append("full access")
            self._answer(completion)

        def requestAccessToEntityType_completion_(self, entity_type, completion):
            world.requests.append(("entity", entity_type))
            self._answer(completion)

        def _answer(self, completion):
            if not world.respond:
                return

            def reply():  # EventKit calls back on a background queue
                world.status = 3 if world.answer else 2
                completion(world.answer, None)

            threading.Thread(target=reply).start()

        def calendarsForEntityType_(self, entity_type):
            assert entity_type == 0
            return list(world.calendars) if self.sees_calendars else []

        def predicateForEventsWithStartDate_endDate_calendars_(self, start, end, calendars):
            return (start, end, calendars)

        def eventsMatchingPredicate_(self, predicate):
            world.predicates.append(predicate)
            start, end, calendars = predicate
            return [
                e for e in world.events
                if (calendars is None or e.calendar() in calendars)
                and e.startDate().ts < end.ts and e.endDate().ts > start.ts
            ]

    return SimpleNamespace(
        EKEventStore=EKEventStore,
        EKEntityTypeEvent=0,
        EKAuthorizationStatusNotDetermined=0,
        EKAuthorizationStatusRestricted=1,
        EKAuthorizationStatusDenied=2,
        EKAuthorizationStatusAuthorized=3,
        EKAuthorizationStatusFullAccess=3,
        EKAuthorizationStatusWriteOnly=4,
        EKEventStatusConfirmed=1,
        EKEventStatusCanceled=3,
    )


FOUNDATION = SimpleNamespace(NSDate=FakeNSDate)


def real_adapter(world: World) -> EventKitCalendar:
    return EventKitCalendar(SEOUL, eventkit=fake_eventkit(world), foundation=FOUNDATION, local_tz=SEOUL)


@pytest.mark.parametrize("modern, expected_request", [(True, "full access"), (False, ("entity", 0))])
def test_request_access_waits_for_the_answer_and_renews_the_store(modern, expected_request):
    world = World(status=0, modern=modern)
    world.calendars = [FakeCalendar("연구", "iCloud")]
    adapter = real_adapter(world)
    assert adapter.authorization_status() == NOT_DETERMINED
    assert adapter.list_calendars() == []  # no access yet
    assert adapter.request_access(timeout=5) is True
    assert world.requests == [expected_request]  # macOS 14+ API when the store has it, else the older one
    assert len(world.stores) == 2  # a fresh store after access was granted
    assert adapter.authorization_status() == GRANTED
    assert adapter.list_calendars() == [{"name": "연구", "source": "iCloud"}]


def test_request_access_refused_or_unanswered():
    world = World(status=0, answer=False)
    adapter = real_adapter(world)
    assert adapter.request_access(timeout=5) is False
    assert adapter.authorization_status() == DENIED and len(world.stores) == 1

    silent = World(status=0, respond=False)
    assert real_adapter(silent).request_access(timeout=0.05) is False


def test_responds_to_falls_back_to_attribute_lookup():
    class Store:
        def respondsToSelector_(self, selector):
            raise TypeError("selector conversion failed")

        def requestFullAccessToEventsWithCompletion_(self, completion):
            pass

    assert macos_calendar._responds_to(Store(), "requestFullAccessToEventsWithCompletion:") is True
    assert macos_calendar._responds_to(Store(), "requestSomethingElse:") is False


def test_fetch_events_converts_eventkit_objects():
    world = World(status=3)
    research, work, family = FakeCalendar("연구", "iCloud"), FakeCalendar("Work", "Exchange"), FakeCalendar("가족", "나의 Mac")
    world.calendars = [research, work, family]
    world.events = [
        FakeEvent("지도교수 면담", at(5, 13), at(5, 14, 30), research, location="교수 연구실"),
        FakeEvent("학회 초록 마감", at(5), at(5, 23, 59) + timedelta(seconds=59), work, all_day=True),
        FakeEvent("취소된 회의", at(5, 16), at(5, 17), research, status=3),
        FakeEvent(None, at(6, 9), at(6, 10), family),
        FakeEvent("창 밖 일정", at(9, 9), at(9, 10), research),
    ]
    adapter = real_adapter(world)
    assert adapter.list_calendars() == [
        {"name": "연구", "source": "iCloud"},
        {"name": "Work", "source": "Exchange"},
        {"name": "가족", "source": "나의 Mac"},
    ]

    records = adapter.fetch_events(at(5), at(7))
    start, end, calendars = world.predicates[-1]
    assert (start.ts, end.ts, calendars) == (at(5).timestamp(), at(7).timestamp(), None)  # nil = every calendar
    assert records == [
        {"title": "지도교수 면담", "start": at(5, 13), "end": at(5, 14, 30), "all_day": False, "location": "교수 연구실", "calendar": "연구"},
        {"title": "학회 초록 마감", "start": at(5), "end": at(6), "all_day": True, "location": "", "calendar": "Work"},
        {"title": "", "start": at(6, 9), "end": at(6, 10), "all_day": False, "location": "", "calendar": "가족"},
    ]

    adapter.fetch_events(at(5), at(7), names=[unicodedata.normalize("NFD", "연구"), "WORK"])
    assert world.predicates[-1][2] == [research, work]

    seen = len(world.predicates)
    assert adapter.fetch_events(at(5), at(7), names=["없음"]) == []
    assert len(world.predicates) == seen  # no predicate with nil calendars (= all) was made


def test_real_adapter_through_the_calendar_tool():
    world = World(status=3)
    research = FakeCalendar("연구", "iCloud")
    world.calendars = [research]
    world.events = [FakeEvent("주간 랩미팅", at(5, 10), at(5, 11), research, location="302호")]
    payload = run_schedule(
        env=ENV, now=CLOCK, platform="darwin",
        adapter_factory=lambda tz: EventKitCalendar(tz, eventkit=fake_eventkit(world), foundation=FOUNDATION, local_tz=SEOUL),
    )
    assert payload["source"] == "macos" and payload["ok"] is True
    assert [e["title"] for e in payload["now"]] == ["주간 랩미팅"]


def test_tool_handler_reads_the_calendar_app_in_a_worker_thread(monkeypatch):
    adapter = FakeAdapter()
    monkeypatch.setattr(config, "current_platform", lambda: "darwin")
    monkeypatch.setattr(macos_calendar, "default_adapter", lambda tz: adapter)
    result = asyncio.run(get_schedule.handler({"days": 1}))
    data = json.loads(result["content"][0]["text"])
    assert data["configured"] is True and data["source"] == "macos"
    assert adapter.threads and threading.main_thread().name not in adapter.threads


# ---------------------------------------------------------------- --calendar-setup


def setup(adapter, env=None, platform="darwin"):
    out = io.StringIO()
    code = run_calendar_setup(
        {**ENV, **(env or {})}, adapter_factory=lambda tz: adapter, platform=platform, now=CLOCK, out=out
    )
    return code, out.getvalue()


def test_setup_when_already_granted_lists_calendars_and_events():
    adapter = FakeAdapter()
    code, out = setup(adapter)
    assert code == 0 and "request" not in adapter.calls
    assert "지금 캘린더 접근 권한: 허용됨(전체 접근)" in out
    assert "캘린더 3개 (계정별):\n  iCloud\n    - 연구\n    - Work\n  기타\n    - 생일\n" in out
    assert "MACOS_CALENDARS가 비어 있어 모든 캘린더를 읽습니다." in out
    assert (
        "오늘 (2026-10-05 월요일): 일정 3개\n"
        "    - 종일 학회 초록 마감 [연구]\n"
        "    - 10:00–11:00 주간 랩미팅 [연구]\n"
        "    - 13:00–14:30 지도교수 면담 [연구]\n"
        "내일 (2026-10-06 화요일): 일정 1개\n"
        "    - 09:00–10:00 수업 [연구]\n"
    ) in out
    assert "연결 완료" in out
    assert adapter.fetches == [(at(5), at(7), None)]


def test_setup_asks_once_when_not_determined():
    adapter = FakeAdapter(status=NOT_DETERMINED)
    code, out = setup(adapter)
    assert code == 0 and adapter.calls.count("request") == 1
    assert "'허용'을 눌러 주세요" in out and "터미널 앱 이름" in out
    assert "결과: 허용됨(전체 접근)" in out and "캘린더 3개 (계정별)" in out


@pytest.mark.parametrize("status", [DENIED, WRITE_ONLY, RESTRICTED])
def test_setup_without_permission_shows_system_settings(status):
    adapter = FakeAdapter(status=status)
    code, out = setup(adapter)
    assert code == 1 and SETTINGS_PATH in out and "'전체 접근'" in out
    assert "request" not in adapter.calls and "계정별" not in out


def test_setup_request_refused_or_unanswered():
    code, out = setup(FakeAdapter(status=NOT_DETERMINED, grant=False))
    assert code == 1 and "결과: 거부됨" in out and SETTINGS_PATH in out

    code, out = setup(FakeAdapter(status=NOT_DETERMINED, grant=None))
    assert code == 1 and "응답을 받지 못했습니다" in out


def test_setup_shows_the_filter_and_which_source_the_bot_reads():
    env = {"MACOS_CALENDARS": "work, 없는 캘린더", "CALENDAR_ICS_URLS": URL}
    records = [record("Standup", at(5, 9), at(5, 9, 15), calendar="Work"), *RECORDS]
    code, out = setup(FakeAdapter(records=records), env)
    assert code == 0
    assert "MACOS_CALENDARS: work, 없는 캘린더 (이 캘린더만 읽습니다)" in out
    assert "[경고] MACOS_CALENDARS의 '없는 캘린더' 캘린더를" in out
    assert "오늘 (2026-10-05 월요일): 일정 1개\n    - 09:00–09:15 Standup [Work]\n" in out
    assert "[참고] 지금은 .env의 CALENDAR_ICS_URLS(ICS 주소)를 읽습니다" in out


def test_setup_off_mac_or_without_pyobjc():
    code, out = setup(FakeAdapter(), platform="linux")
    assert code == 1 and "macOS에서만" in out and "CALENDAR_ICS_URLS" in out

    def missing(tz):
        raise EventKitUnavailable("no pyobjc")

    out_io = io.StringIO()
    assert run_calendar_setup(ENV, adapter_factory=missing, platform="darwin", now=CLOCK, out=out_io) == 1
    assert "pip install -e ." in out_io.getvalue()


def test_cli_calendar_setup_dispatch_help_and_conflicts(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(calendar_setup, "run_calendar_setup", lambda: calls.append("setup") or 0)
    assert main(["--calendar-setup"]) == 0 and calls == ["setup"]
    help_text = build_parser().format_help()
    assert "--calendar-setup" in help_text and "python -m mungchi --calendar-setup" in help_text
    for argv in (["--calendar-setup", "질문"], ["--calendar-setup", "--brief"], ["--calendar-setup", "--agent", "schedule"]):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2
    assert "--calendar-setup은 질문이나 다른 옵션" in capsys.readouterr().err


# ---------------------------------------------------------------- no EventKit at import time


def test_importing_the_package_never_imports_eventkit():
    code = (
        "import sys\n"
        "import mungchi, mungchi.main, mungchi.calendar_setup, mungchi.slack_bot\n"
        "import mungchi.tools, mungchi.tools.calendar_tool, mungchi.tools.macos_calendar\n"
        "loaded = [m for m in ('EventKit', 'Foundation', 'objc', 'AppKit') if m in sys.modules]\n"
        "print(loaded)\n"
        "sys.exit(1 if loaded else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
