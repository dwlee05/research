"""Adding calendar events from a pasted note, without macOS, network or a model.

Normalization and the preview are pure. The propose tool and event creation
run against a fake Calendar app adapter; the real EventKit adapter runs
against fake EventKit / Foundation objects that answer the selectors PyObjC
exposes (as far as they could be checked without a Mac).
"""

from __future__ import annotations

import asyncio
import io
import json
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from mungchi import config
from mungchi.calendar_setup import run_calendar_setup, write_target_lines
from mungchi.state import StateStore
from mungchi.tools import event_proposals, macos_calendar
from mungchi.tools.event_proposals import (
    CONFIRM_QUESTION,
    NO,
    YES,
    CreationOutcome,
    CreationResult,
    ProposedEvent,
    answer_text,
    cli_conversation_key,
    confirm_proposal,
    create_proposal_events,
    creation_report,
    normalize_event,
    normalize_events,
    preview_text,
    reply_kind,
    resolve_event_date,
    run_propose,
    slack_conversation_key,
)
from mungchi.tools.macos_calendar import (
    DENIED,
    GRANTED,
    NOT_DETERMINED,
    RESTRICTED,
    WRITE_ONLY,
    EventKitCalendar,
    duplicate_window,
    resolve_write_calendar,
    save_outcome,
    similar_events,
    titles_similar,
)
from mungchi.tools.propose_tool import make_propose_calendar_events

SEOUL = ZoneInfo("Asia/Seoul")
# Wednesday 2026-10-07, 10:00 in Seoul.
NOW = datetime(2026, 10, 7, 10, 0, tzinfo=SEOUL)
TODAY = NOW.date()
# The yes / no flow (no categories); the category tests use CAT_ENV.
ENV = {"TIMEZONE": "Asia/Seoul", "CALENDAR_CATEGORIES": ""}
KEY = slack_conversation_key("schedule", "D0123ABCD", "1700000000.000200")
OTHER_KEY = slack_conversation_key("schedule", "D0123ABCD", "1700000000.000900")

# The user's example note, as the model is told to extract it.
NOTE = (
    "문 결과, 가장 많은 교수님께서 참석 가능하신 날짜를 기준으로 10월과 11월 신임교수모임 일정을 아래와 같이 정하였습니다.\n\n"
    "* 10월 모임: 10월 22일(목) 오후 12시\n발표: 홍길동 교수님"
)
EXAMPLE = {
    "title": "신임교수모임 (10월)",
    "date": "2026-10-22",
    "start_time": "12:00",
    "end_time": None,
    "all_day": False,
    "notes": "발표: 홍길동 교수님",
    "weekday_in_text": "목",
}
EXAMPLE_LINE = "• 10/22(목) 12:00–13:00 신임교수모임 (10월) · 메모: 발표: 홍길동 교수님 · 캘린더: 연구"
WRITABLE = [
    {"name": "연구", "source": "iCloud", "is_default": True},
    {"name": "Work", "source": "Exchange", "is_default": False},
]


def at(month: int, day: int, hour: int = 0, minute: int = 0, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=SEOUL)


def norm(raw, now=NOW, minutes=60):
    event, problems = normalize_event(raw, tz=SEOUL, now=now, default_minutes=minutes)
    assert problems == [], problems
    return event


def record(title, start, end, calendar="연구", all_day=False):
    return {"title": title, "start": start, "end": end, "all_day": all_day, "location": "", "calendar": calendar}


class FakeCalendarApp:
    """The Calendar app behind the adapter protocol: permission, writable calendars, events, writes."""

    def __init__(self, status=GRANTED, writable=WRITABLE, existing=(), fail_titles=(), raise_titles=()):
        self.status = status
        self.writable = [dict(c) for c in writable]
        self.existing = list(existing)
        self.fail_titles = set(fail_titles)
        self.raise_titles = set(raise_titles)
        self.created: list[dict] = []
        self.similar_calls: list[tuple] = []

    def authorization_status(self):
        return self.status

    def list_writable_calendars(self):
        return [dict(c) for c in self.writable]

    def fetch_events(self, start, end, names=None):
        return [dict(r) for r in self.existing if r["start"] < end and r["end"] > start]

    def find_similar_events(self, start, end, title):
        self.similar_calls.append((start, end, title))
        window_start, window_end = duplicate_window(start, end)
        return similar_events(self.fetch_events(window_start, window_end), start, end, title)

    def create_event(self, title, start, end, all_day, location=None, notes=None, calendar_name=None):
        self.created.append(
            {
                "title": title,
                "start": start,
                "end": end,
                "all_day": all_day,
                "location": location,
                "notes": notes,
                "calendar_name": calendar_name,
            }
        )
        if title in self.raise_titles:
            raise RuntimeError("objc.error: boom")
        if title in self.fail_titles:
            return {"ok": False, "id": None, "calendar": "", "error": "캘린더에 저장하지 못했어요 (읽기 전용 캘린더)"}
        return {"ok": True, "id": f"EV-{len(self.created)}", "calendar": calendar_name or "연구", "error": None}


def propose(app, events, key=KEY, store=None, env=None, now=NOW, platform="darwin", **args):
    store = store or StateStore(config.get_state_path())
    payload = run_propose(
        {"events": events, "source_note": NOTE, **args},
        key,
        env={**ENV, **(env or {})},
        now=now,
        store=store,
        adapter_factory=lambda tz: app,
        platform=platform,
    )
    return payload, store


def creator(app, env=None):
    return lambda proposal: create_proposal_events(
        proposal, env={**ENV, **(env or {})}, now=NOW, adapter_factory=lambda tz: app, platform="darwin"
    )


# ---------------------------------------------------------------- pure normalization


def test_the_example_note_becomes_thursday_october_22_noon_to_one():
    event = norm(EXAMPLE)
    assert event.title == "신임교수모임 (10월)"
    assert (event.start, event.end) == (at(10, 22, 12), at(10, 22, 13))
    assert event.day == date(2026, 10, 22) and event.to_payload()["weekday"] == "목"
    assert event.notes == "발표: 홍길동 교수님" and event.location == ""
    assert event.end_defaulted and not event.needs_time and not event.all_day
    assert event.warnings == []  # (목) matches, not past, not midnight
    assert preview_text([event], "연구", TODAY) == EXAMPLE_LINE


def test_default_duration_and_explicit_end():
    assert norm({**EXAMPLE, "end_time": "14:30"}).end == at(10, 22, 14, 30)
    ninety = norm(EXAMPLE, minutes=90)
    assert ninety.end == at(10, 22, 13, 30) and ninety.end_defaulted
    # The end before the start: the default length, with a warning instead of a guess.
    backwards = norm({**EXAMPLE, "end_time": "11:00"})
    assert backwards.end == at(10, 22, 13) and "끝 시각(11:00)이 시작(12:00)보다 빨라서" in backwards.warnings[0]
    # DEFAULT_EVENT_MINUTES: whole minutes from 5 to 1440, anything else is the default.
    assert config.get_default_event_minutes({}) == 60
    assert config.get_default_event_minutes({"DEFAULT_EVENT_MINUTES": "90"}) == 90
    for bad in ("abc", "0", "-30", "2000", "1.5"):
        assert config.get_default_event_minutes({"DEFAULT_EVENT_MINUTES": bad}) == 60


def test_all_day_events_span_the_whole_day():
    event = norm({"title": "학과 체육대회", "date": "2026-10-23", "all_day": True, "start_time": None})
    assert (event.start, event.end) == (at(10, 23), at(10, 24)) and event.all_day
    assert preview_text([event], "연구", TODAY) == "• 10/23(금) 종일 학과 체육대회 · 캘린더: 연구"


def test_a_missing_start_time_is_flagged_never_guessed():
    event = norm({"title": "세미나", "date": "2026-10-28", "start_time": None, "end_time": None})
    assert event.needs_time and event.start is None and event.end is None
    assert event.warnings == [event_proposals.NEEDS_TIME_WARNING]
    assert preview_text([event], "연구", TODAY).splitlines() == [
        "• 10/28(수) 시간 미정 세미나 · 캘린더: 연구",
        f"  ⚠️ {event_proposals.NEEDS_TIME_WARNING}",
    ]


def test_a_weekday_that_does_not_match_the_date_is_warned_about():
    event = norm({**EXAMPLE, "date": "2026-10-21"})  # a Wednesday
    assert event.warnings == ["요일 불일치: 본문은 (목)인데 날짜는 수요일"]
    # "(목)", "목요일" and no weekday at all are understood too.
    assert norm({**EXAMPLE, "date": "2026-10-21", "weekday_in_text": "(목)"}).warnings == event.warnings
    assert norm({**EXAMPLE, "date": "2026-10-21", "weekday_in_text": "목요일"}).warnings == event.warnings
    assert norm({**EXAMPLE, "weekday_in_text": None}).warnings == []


def test_past_dates_and_times_are_warned_about():
    past = norm({**EXAMPLE, "date": "2026-10-01", "weekday_in_text": None})
    assert past.warnings == ["지난 날짜예요: 10/01(목). 연도나 날짜가 맞는지 확인해 주세요."]
    earlier_today = norm({**EXAMPLE, "date": "2026-10-07", "start_time": "09:00", "weekday_in_text": None})
    assert earlier_today.warnings == ["이미 지난 시각이에요: 오늘 09:00."]
    later_today = norm({**EXAMPLE, "date": "2026-10-07", "start_time": "15:00", "weekday_in_text": None})
    assert later_today.warnings == []
    assert norm({"title": "x", "date": "2026-10-07", "all_day": True}).warnings == []  # today, all day


def test_midnight_is_flagged_for_a_possible_am_12():
    event = norm({**EXAMPLE, "start_time": "00:00", "date": "2026-10-23", "weekday_in_text": None})
    assert event.warnings == [event_proposals.MIDNIGHT_WARNING]
    assert "'오전 12시'" in event_proposals.MIDNIGHT_WARNING


def test_more_than_ten_events_are_rejected():
    events, problems = normalize_events([EXAMPLE] * 11, tz=SEOUL, now=NOW)
    assert events == [] and problems == ["한 번에 10개까지만 제안할 수 있어요(받은 일정 11개). 나눠서 제안하세요."]
    events, problems = normalize_events([EXAMPLE] * 10, tz=SEOUL, now=NOW)
    assert len(events) == 10 and problems == []
    for empty in ([], None, "x"):
        assert normalize_events(empty, tz=SEOUL, now=NOW) == ([], ["events에 일정을 하나 이상 넣으세요."])


def test_unreadable_input_rejects_the_whole_proposal():
    bad = [EXAMPLE, {"title": "", "date": "10월 22일", "start_time": "오후 12시"}, "not an event"]
    events, problems = normalize_events(bad, tz=SEOUL, now=NOW)
    assert events == []
    assert problems[0] == "2번 일정: 제목(title)이 없어요."
    assert problems[1] == "2번 일정: 날짜(date) 값을 읽지 못했어요: '10월 22일'. YYYY-MM-DD로 적으세요."
    assert problems[2] == "2번 일정: start_time 값을 읽지 못했어요: '오후 12시'. 24시간제 HH:MM으로 적으세요(없으면 null)."
    assert problems[3].startswith("3번 일정:")


def test_the_year_rule_uses_the_injected_clock():
    assert resolve_event_date("2026-10-22", TODAY) == date(2026, 10, 22)
    assert resolve_event_date("10-22", TODAY) == date(2026, 10, 22)
    assert resolve_event_date("10/7", TODAY) == date(2026, 10, 7)  # today counts
    assert resolve_event_date("01-05", TODAY) == date(2027, 1, 5)  # already past this year
    assert resolve_event_date("10-06", TODAY) == date(2027, 10, 6)
    assert resolve_event_date("02-29", TODAY) == date(2028, 2, 29)
    assert resolve_event_date("01-05", date(2026, 1, 2)) == date(2026, 1, 5)
    for bad in ("", "22일", "13-01", "2026-02-30"):
        with pytest.raises(ValueError):
            resolve_event_date(bad, TODAY)
    # Through the normalizer, and the preview names a year that is not this one.
    january = norm({"title": "신년회", "date": "1-5", "start_time": "18:00"})
    assert january.start == at(1, 5, 18, year=2027)
    assert preview_text([january], "연구", TODAY) == "• 2027/01/05(화) 18:00–19:00 신년회 · 캘린더: 연구"


def test_preview_lines_carry_location_notes_and_warnings():
    event = norm(
        {
            **EXAMPLE,
            "date": "2026-10-21",
            "location": "  본관 302호 ",
            "notes": "발표: 홍길동 교수님\n\n  준비물: 노트북  ",
        }
    )
    assert event.notes == "발표: 홍길동 교수님\n준비물: 노트북"  # lines kept, blanks dropped
    lines = preview_text([event], "연구", TODAY, ["전체 안내"]).splitlines()
    assert lines == [
        "• 10/21(수) 12:00–13:00 신임교수모임 (10월) · 장소: 본관 302호 · 메모: 발표: 홍길동 교수님 / 준비물: 노트북 · 캘린더: 연구",
        "  ⚠️ 요일 불일치: 본문은 (목)인데 날짜는 수요일",
        "⚠️ 전체 안내",
    ]
    late = norm({"title": "야간 실험", "date": "2026-10-22", "start_time": "23:30"})
    assert preview_text([late], "", TODAY) == "• 10/22(목) 23:30–10/23 00:30 야간 실험"


@pytest.mark.parametrize(
    "text",
    ["네", "예", "응", "ㅇㅇ", "좋아", "추가해", "추가해줘", "추가해 줘", "등록해", "등록해줘", "넣어줘", "ok", "OK", "Okay",
     "yes", "Y", "👍", "👍🏻", ":+1:", ":+1::skin-tone-2:", "네!", " 네. ", "넵", "추가해 주세요"],
)
def test_affirmative_replies(text):
    assert reply_kind(text) == YES


@pytest.mark.parametrize("text", ["아니", "아니요", "아뇨", "취소", "됐어", "no", "N", "아니오", "취소해 줘"])
def test_negative_replies(text):
    assert reply_kind(text) == NO


@pytest.mark.parametrize(
    "text",
    ["", "시간은 1시로 바꿔줘", "네 근데 시간은 1시로", "네, 그리고 11월 것도", "아니 1시야", "응응응응응응응응응응응응응",
     "추가해줘 연구 캘린더에", "좋아요 근데", "nope", "yess"],
)
def test_anything_else_is_not_an_answer(text):
    assert reply_kind(text) is None


def test_conversation_keys():
    assert slack_conversation_key("update", "C1", "1.0") == "slack:update:C1:1.0"
    assert slack_conversation_key("update", "C1", "1.0") != slack_conversation_key("schedule", "C1", "1.0")
    with pytest.raises(ValueError):
        slack_conversation_key("nobody", "C1", "1.0")
    first, second = cli_conversation_key(), cli_conversation_key()
    assert first.startswith("cli:") and first != second


# ---------------------------------------------------------------- pending proposals in the state file


def test_pending_proposals_persist_expire_and_are_taken_once(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.mark_brief_date("2026-10-07")
    stored = store.save_pending_proposal(KEY, {"id": "p1", "events": []}, NOW)
    assert stored["expires_at"] == (NOW + timedelta(hours=24)).astimezone(timezone.utc).isoformat()
    reopened = StateStore(path)  # e.g. after a bot restart
    assert reopened.pending_proposal(KEY, NOW + timedelta(hours=23))["id"] == "p1"
    assert reopened.pending_proposal(KEY, NOW + timedelta(hours=24)) is None  # expired
    assert reopened.pending_proposal(OTHER_KEY, NOW) is None
    assert reopened.last_brief_date() == "2026-10-07"  # other keys untouched
    assert reopened.take_pending_proposal(KEY, NOW)["id"] == "p1"
    assert reopened.take_pending_proposal(KEY, NOW) is None  # never twice
    store.save_pending_proposal(KEY, {"id": "p2", "events": []}, NOW)
    assert store.take_pending_proposal(KEY, NOW + timedelta(days=2)) is None  # expired: nothing to take
    assert store.pending_proposal(KEY, NOW) is None  # and it is gone
    store.save_pending_proposal(KEY, {"id": "p3", "events": []}, NOW)
    assert store.clear_pending_proposal(KEY) is True and store.clear_pending_proposal(KEY) is False


def test_pending_proposals_are_capped_and_tolerate_junk(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"pending_events": {"a": "junk", "b": {"events": []}, "c": [1]}}), encoding="utf-8")
    store = StateStore(path)
    assert store.pending_proposal("b", NOW) is None  # no expiry: ignored
    monkeypatch.setattr("mungchi.state.MAX_PENDING_PROPOSALS", 3)
    for i in range(5):
        store.save_pending_proposal(f"k{i}", {"id": str(i), "events": []}, NOW + timedelta(minutes=i))
    assert list(store.load()["pending_events"]) == ["k2", "k3", "k4"]
    # An old proposal is dropped when a newer one is saved after it expired.
    store.save_pending_proposal("late", {"id": "late", "events": []}, NOW + timedelta(days=2))
    assert list(store.load()["pending_events"]) == ["late"]


# ---------------------------------------------------------------- the propose tool's work


def test_propose_stores_the_proposal_under_the_bound_key_and_returns_a_preview():
    app = FakeCalendarApp()
    payload, store = propose(app, [EXAMPLE])
    assert payload["ok"] is True and payload["can_confirm"] is True
    assert payload["calendar"] == "연구"  # the default calendar
    assert payload["preview"] == EXAMPLE_LINE
    assert payload["confirm_question"] == CONFIRM_QUESTION == "캘린더에 추가할까요? (네 / 아니요 / 고칠 내용)"
    assert payload["warnings"] == [] and payload["needs_time"] == 0
    [event] = payload["events"]
    assert event == {
        "title": "신임교수모임 (10월)",
        "date": "2026-10-22",
        "weekday": "목",
        "start_time": "12:00",
        "end_time": "13:00",
        "all_day": False,
        "location": "",
        "notes": "발표: 홍길동 교수님",
        "needs_time": False,
        "end_defaulted": True,
        "warnings": [],
    }
    pending = store.pending_proposal(KEY, NOW)
    assert pending["calendar"] == "연구" and pending["calendar_label"] == "연구"
    assert pending["events"] == [
        {
            "title": "신임교수모임 (10월)",
            "date": "2026-10-22",
            "start": "2026-10-22T12:00:00+09:00",
            "end": "2026-10-22T13:00:00+09:00",
            "all_day": False,
            "location": "",
            "notes": "발표: 홍길동 교수님",
            "needs_time": False,
        }
    ]
    assert pending["source_excerpt"].startswith("문 결과")
    assert store.pending_proposal(OTHER_KEY, NOW) is None
    assert app.created == []  # proposing never creates anything


def test_a_new_proposal_replaces_the_old_one_and_a_failed_one_leaves_none():
    app = FakeCalendarApp()
    _, store = propose(app, [EXAMPLE])
    first = store.pending_proposal(KEY, NOW)["id"]
    propose(app, [{**EXAMPLE, "start_time": "13:00"}], store=store)
    second = store.pending_proposal(KEY, NOW)
    assert second["id"] != first and second["events"][0]["start"] == "2026-10-22T13:00:00+09:00"
    payload, _ = propose(app, [{**EXAMPLE, "date": "언젠가"}], store=store)
    assert payload["ok"] is False and payload["errors"] and store.pending_proposal(KEY, NOW) is None


def test_two_runs_with_different_keys_never_collide():
    app = FakeCalendarApp()
    _, store = propose(app, [EXAMPLE], key=KEY)
    propose(app, [{**EXAMPLE, "title": "11월 모임", "date": "2026-11-19"}], key=OTHER_KEY, store=store)
    assert store.pending_proposal(KEY, NOW)["events"][0]["title"] == "신임교수모임 (10월)"
    assert store.pending_proposal(OTHER_KEY, NOW)["events"][0]["title"] == "11월 모임"
    assert store.take_pending_proposal(KEY, NOW) is not None
    assert store.pending_proposal(OTHER_KEY, NOW) is not None


def test_concurrent_tool_runs_keep_their_own_keys(monkeypatch):
    """Two runs' tools in one process (two Slack threads), called at the same time."""
    app = FakeCalendarApp()
    monkeypatch.setenv("CALENDAR_CATEGORIES", "")
    monkeypatch.setattr(config, "current_platform", lambda: "darwin")
    monkeypatch.setattr(macos_calendar, "default_adapter", lambda tz: app)
    monkeypatch.setattr(event_proposals, "utcnow", lambda: NOW)
    first, second = make_propose_calendar_events(KEY), make_propose_calendar_events(OTHER_KEY)

    async def both():
        return await asyncio.gather(
            first.handler({"events": [EXAMPLE], "source_note": NOTE}),
            second.handler({"events": [{**EXAMPLE, "title": "다른 스레드"}], "source_note": "x"}),
        )

    results = asyncio.run(both())
    payloads = [json.loads(r["content"][0]["text"]) for r in results]
    assert all(p["ok"] and p["can_confirm"] for p in payloads)
    store = StateStore(config.get_state_path())
    assert store.pending_proposal(KEY, NOW)["events"][0]["title"] == "신임교수모임 (10월)"
    assert store.pending_proposal(OTHER_KEY, NOW)["events"][0]["title"] == "다른 스레드"


def test_without_a_key_the_preview_is_shown_but_nothing_is_stored(tmp_path):
    app = FakeCalendarApp()
    store = StateStore(tmp_path / "s.json")
    payload, _ = propose(app, [EXAMPLE], key=None, store=store)
    assert payload["ok"] is True and payload["can_confirm"] is False
    assert "confirm_question" not in payload and payload["note"] == event_proposals.ONE_SHOT_NOTE
    assert "대화 모드" in payload["note"] and "Slack" in payload["note"]
    assert payload["preview"] == EXAMPLE_LINE
    assert store.load() == {}


def test_proposals_need_mac_calendar_mode():
    app = FakeCalendarApp()
    ics, store = propose(app, [EXAMPLE], env={"CALENDAR_ICS_URLS": "https://example.com/a.ics"})
    assert ics["ok"] is False and ics["error"].startswith("Mac 캘린더 모드에서만 일정을 추가할 수 있어요.")
    assert "CALENDAR_ICS_URLS" in ics["error"]
    off_mac, _ = propose(app, [EXAMPLE], platform="linux", store=store)
    assert off_mac["ok"] is False and off_mac["error"].startswith("Mac 캘린더 모드에서만 일정을 추가할 수 있어요.")
    assert store.pending_proposal(KEY, NOW) is None

    def missing(tz):
        raise macos_calendar.EventKitUnavailable("no pyobjc")

    payload = run_propose({"events": [EXAMPLE]}, KEY, env=ENV, now=NOW, store=store, adapter_factory=missing, platform="darwin")
    assert payload["ok"] is False and "pip install -e ." in payload["error"]


@pytest.mark.parametrize("status", [DENIED, RESTRICTED, NOT_DETERMINED])
def test_proposals_without_write_permission_explain_the_fix(status):
    payload, store = propose(FakeCalendarApp(status=status), [EXAMPLE])
    assert payload["ok"] is False and store.pending_proposal(KEY, NOW) is None
    if status == NOT_DETERMINED:
        assert "--calendar-setup" in payload["error"]
    else:
        assert macos_calendar.SETTINGS_PATH in payload["error"] and "'전체 접근'" in payload["error"]


def test_write_only_permission_proposes_without_the_duplicate_check():
    app = FakeCalendarApp(status=WRITE_ONLY, existing=[record("신임교수모임", at(10, 22, 12), at(10, 22, 13))])
    payload, _ = propose(app, [EXAMPLE])
    assert payload["ok"] and payload["calendar"] == "기본 캘린더" and app.similar_calls == []
    assert payload["warnings"] == [event_proposals.WRITE_ONLY_NOTE]
    assert payload["preview"].endswith(f"⚠️ {event_proposals.WRITE_ONLY_NOTE}")
    chosen, _ = propose(app, [EXAMPLE], env={"CALENDAR_WRITE_TARGET": "연구"})
    assert chosen["ok"] is False and chosen["error"] == event_proposals.WRITE_ONLY_CHOICE_TEXT


def test_choosing_the_calendar():
    app = FakeCalendarApp()
    target, _ = propose(app, [EXAMPLE], env={"CALENDAR_WRITE_TARGET": " work "})
    assert target["calendar"] == "Work" and "· 캘린더: Work" in target["preview"]
    asked, store = propose(app, [EXAMPLE], env={"CALENDAR_WRITE_TARGET": "Work"}, calendar=unicodedata.normalize("NFD", "연구"))
    assert asked["calendar"] == "연구" and store.pending_proposal(KEY, NOW)["calendar"] == "연구"
    unknown, store = propose(app, [EXAMPLE], env={"CALENDAR_WRITE_TARGET": "가족"})
    assert unknown["ok"] is False and store.pending_proposal(KEY, NOW) is None
    assert unknown["error"] == (
        "CALENDAR_WRITE_TARGET의 '가족' 캘린더가 없거나 일정을 추가할 수 없는 캘린더예요. "
        "일정을 추가할 수 있는 캘린더: 연구, Work"
    )
    no_default = FakeCalendarApp(writable=[{"name": "연구", "source": "iCloud", "is_default": False}])
    payload, store = propose(no_default, [EXAMPLE])
    assert payload["calendar"] == "기본 캘린더" and store.pending_proposal(KEY, NOW)["calendar"] is None


def test_likely_duplicates_are_warned_about():
    existing = [
        record("신임교수모임", at(10, 22, 12), at(10, 22, 13)),  # same title word, same day
        record("학과 회의", at(10, 22, 12), at(10, 22, 13), calendar="Work"),  # exact same time
        record("점심 약속", at(10, 22, 11), at(10, 22, 12)),  # unrelated
        record("신임교수모임 (11월)", at(11, 19, 12), at(11, 19, 13)),  # another day
    ]
    payload, _ = propose(FakeCalendarApp(existing=existing), [EXAMPLE])
    assert payload["events"][0]["warnings"] == [
        "비슷한 일정이 이미 있어요: 10/22(목) 12:00–13:00 신임교수모임 (연구)",
        "비슷한 일정이 이미 있어요: 10/22(목) 12:00–13:00 학과 회의 (Work)",
    ]
    assert payload["warnings"][0] == "1번 신임교수모임 (10월): 비슷한 일정이 이미 있어요: 10/22(목) 12:00–13:00 신임교수모임 (연구)"
    assert "  ⚠️ 비슷한 일정이 이미 있어요: 10/22(목) 12:00–13:00 학과 회의 (Work)" in payload["preview"]


def test_a_failing_duplicate_check_never_blocks_the_proposal():
    class Broken(FakeCalendarApp):
        def find_similar_events(self, start, end, title):
            raise RuntimeError("EKErrorDomain 1")

    payload, store = propose(Broken(), [EXAMPLE])
    assert payload["ok"] and store.pending_proposal(KEY, NOW) is not None
    assert "이미 있는 일정과 겹치는지 확인하지 못했어요" in payload["events"][0]["warnings"][0]


def test_the_tool_result_is_json_and_reports_errors_in_korean(monkeypatch):
    monkeypatch.setattr(event_proposals, "run_propose", lambda args, key: (_ for _ in ()).throw(RuntimeError("boom")))
    result = asyncio.run(make_propose_calendar_events("k").handler({"events": []}))
    data = json.loads(result["content"][0]["text"])
    assert data == {"ok": False, "error": "일정을 제안하지 못했어요: RuntimeError: boom"}


# ---------------------------------------------------------------- creating (code only, after "네")


def stored_proposal(app, events, **kwargs):
    _, store = propose(app, events, **kwargs)
    return store, store.pending_proposal(KEY, NOW)


def test_creating_a_confirmed_proposal_calls_the_adapter_per_event():
    app = FakeCalendarApp()
    store, proposal = stored_proposal(app, [EXAMPLE, {**EXAMPLE, "title": "신임교수모임 (11월)", "date": "2026-11-19"}])
    outcome = create_proposal_events(proposal, env=ENV, now=NOW, adapter_factory=lambda tz: app, platform="darwin")
    assert [c["title"] for c in app.created] == ["신임교수모임 (10월)", "신임교수모임 (11월)"]
    assert app.created[0] == {
        "title": "신임교수모임 (10월)",
        "start": at(10, 22, 12),
        "end": at(10, 22, 13),
        "all_day": False,
        "location": None,
        "notes": "발표: 홍길동 교수님",
        "calendar_name": "연구",
    }
    assert outcome.created == 2 and not outcome.fatal
    assert creation_report(outcome) == (
        "✅ 캘린더에 추가했어요\n"
        "• 10/22(목) 12:00–13:00 신임교수모임 (10월) · 캘린더: 연구\n"
        "• 11/19(목) 12:00–13:00 신임교수모임 (11월) · 캘린더: 연구"
    )


def test_partial_failures_skipped_items_and_crashes_are_reported_per_event():
    app = FakeCalendarApp(fail_titles={"실패할 일정"}, raise_titles={"터질 일정"})
    events = [
        EXAMPLE,
        {"title": "실패할 일정", "date": "2026-10-23", "start_time": "10:00"},
        {"title": "시간 모름", "date": "2026-10-24", "start_time": None},
        {"title": "터질 일정", "date": "2026-10-25", "all_day": True},
    ]
    _, proposal = stored_proposal(app, events)
    report = creation_report(create_proposal_events(proposal, env=ENV, now=NOW, adapter_factory=lambda tz: app, platform="darwin"))
    assert [c["title"] for c in app.created] == ["신임교수모임 (10월)", "실패할 일정", "터질 일정"]  # never the one without a time
    assert report.splitlines() == [
        "✅ 캘린더에 추가했어요",
        "• 10/22(목) 12:00–13:00 신임교수모임 (10월) · 캘린더: 연구",
        "❌ 10/23(금) 10:00–11:00 실패할 일정 추가 실패: 캘린더에 저장하지 못했어요 (읽기 전용 캘린더)",
        "⏭️ 10/24(토) 시간 미정 시간 모름: 시작 시각이 없어 추가하지 않았어요. 시각을 알려 주시면 다시 제안할게요.",
        "❌ 10/25(일) 종일 터질 일정 추가 실패: RuntimeError: objc.error: boom",
    ]
    assert app.created[2]["all_day"] is True and app.created[2]["end"] - app.created[2]["start"] == timedelta(days=1)


def test_nothing_is_created_without_mac_mode_or_permission():
    app = FakeCalendarApp()
    _, proposal = stored_proposal(app, [EXAMPLE])
    ics = create_proposal_events(proposal, env={**ENV, "CALENDAR_SOURCE": "ics", "CALENDAR_ICS_URLS": "https://x/a.ics"}, adapter_factory=lambda tz: app, platform="darwin")
    assert ics.fatal.startswith("Mac 캘린더 모드에서만") and app.created == []
    assert creation_report(ics).startswith("❌ 캘린더에 추가하지 못했어요: Mac 캘린더 모드에서만 일정을 추가할 수 있어요.")
    app.status = DENIED
    denied = create_proposal_events(proposal, env=ENV, adapter_factory=lambda tz: app, platform="darwin")
    assert macos_calendar.SETTINGS_PATH in denied.fatal and app.created == []
    app.status = WRITE_ONLY
    assert create_proposal_events(proposal, env=ENV, adapter_factory=lambda tz: app, platform="darwin").created == 1


def test_a_stored_proposal_that_cannot_be_read_back_is_reported():
    app = FakeCalendarApp()
    proposal = {"calendar": None, "events": [{"title": "x", "date": "nope"}, {**ProposedEvent(
        title="좋은 일정", day=date(2026, 10, 22), start=at(10, 22, 9), end=at(10, 22, 10)).to_state()}]}
    report = creation_report(create_proposal_events(proposal, env=ENV, now=NOW, adapter_factory=lambda tz: app, platform="darwin"))
    lines = report.splitlines()
    assert lines[0] == "✅ 캘린더에 추가했어요"
    assert lines[1].startswith("❌ 1번 일정 추가 실패: 저장된 제안을 읽지 못했어요")
    assert lines[2] == "• 10/22(목) 09:00–10:00 좋은 일정 · 캘린더: 연구"


def test_answers_take_the_proposal_exactly_once(tmp_path):
    app = FakeCalendarApp()
    store, _ = stored_proposal(app, [EXAMPLE], store=StateStore(tmp_path / "s.json"))
    assert confirm_proposal(store, KEY, NO, now=NOW, create=creator(app)) == "취소했어요"
    assert app.created == [] and store.pending_proposal(KEY, NOW) is None
    assert confirm_proposal(store, KEY, YES, now=NOW, create=creator(app)) is None  # nothing pending any more
    propose(app, [EXAMPLE], store=store)
    assert confirm_proposal(store, KEY, YES, now=NOW, create=creator(app)).startswith("✅ 캘린더에 추가했어요")
    assert len(app.created) == 1 and store.pending_proposal(KEY, NOW) is None
    # A crash while creating is a short note, never raised.
    boom = answer_text({"events": []}, YES, create=lambda p: (_ for _ in ()).throw(RuntimeError("down")))
    assert boom == "❌ 캘린더에 추가하지 못했어요: 오류가 났어요 (RuntimeError: down)"


def test_report_when_nothing_could_be_added():
    outcome = CreationOutcome(results=[CreationResult(summary="10/22(목) 종일 x", error="거부됨")])
    assert creation_report(outcome) == "❌ 캘린더에 추가하지 못했어요\n❌ 10/22(목) 종일 x 추가 실패: 거부됨"


# ---------------------------------------------------------------- pure adapter helpers


def test_resolve_write_calendar():
    nfd = unicodedata.normalize("NFD", "연구")
    writable = [{"name": nfd, "is_default": False}, {"name": "Work", "is_default": True}]
    assert resolve_write_calendar(writable, "연구") == (nfd, None)  # NFC, as the Calendar app writes it
    assert resolve_write_calendar(writable, " WORK ") == ("Work", None)
    assert resolve_write_calendar(writable, None, "연구") == (nfd, None)
    assert resolve_write_calendar(writable, "work", "연구") == ("Work", None)  # the request wins
    assert resolve_write_calendar(writable, None, "") == ("Work", None)  # the default
    assert resolve_write_calendar([{"name": "A", "is_default": False}], None) == (None, None)  # the store's default
    name, error = resolve_write_calendar(writable, "생일")
    assert name is None and error.startswith("'생일' 캘린더가 없거나 일정을 추가할 수 없는 캘린더예요.")
    assert resolve_write_calendar([], "x")[1].endswith("일정을 추가할 수 있는 캘린더: (없음)")


def test_title_similarity():
    assert titles_similar("신임교수모임 (10월)", "신임교수모임")
    assert titles_similar("신임교수 모임", "신임교수모임 (10월)")  # spaces ignored
    assert titles_similar("Lab Meeting", "lab meeting (weekly)")
    assert titles_similar(unicodedata.normalize("NFD", "논문 심사"), "박사 논문 심사")
    assert not titles_similar("학과 회의", "연구 회의")  # only a generic word in common
    assert not titles_similar("10월 모임", "11월 모임")
    assert not titles_similar("점심", "저녁")
    assert not titles_similar("", "신임교수모임")


def test_duplicate_window_and_similar_events():
    assert duplicate_window(at(10, 22, 12), at(10, 22, 13)) == (at(10, 22, 10), at(10, 22, 15))
    assert duplicate_window(at(10, 22, 1), at(10, 22, 23)) == (at(10, 22), at(10, 23))  # never another day
    records = [
        record("신임교수모임", at(10, 22, 14, 30), at(10, 22, 15, 30)),  # inside the window
        record("신임교수모임", at(10, 22, 16), at(10, 22, 17)),  # outside
        record("다른 일", at(10, 22, 12), at(10, 22, 13)),  # exact same time
        record("종일 행사", at(10, 22), at(10, 23), all_day=True),  # all-day: never by time alone
        record("신임교수모임 리마인더", at(10, 22, 11), at(10, 22, 11)),  # zero length, inside
    ]
    found = similar_events(records, at(10, 22, 12), at(10, 22, 13), "신임교수모임 (10월)")
    assert [(r["title"], r["start"].hour) for r in found] == [("신임교수모임", 14), ("다른 일", 12), ("신임교수모임 리마인더", 11)]


def test_save_outcome_accepts_tuples_and_bools():
    error = SimpleNamespace(localizedDescription=lambda: "The calendar is read only.")
    assert save_outcome((True, None)) == (True, None)
    assert save_outcome((False, error)) == (False, error)
    assert save_outcome([1]) == (True, None)
    assert save_outcome(True) == (True, None) and save_outcome(0) == (False, None)
    assert save_outcome(()) == (False, None)
    assert macos_calendar.error_text(error) == "The calendar is read only."
    assert macos_calendar.error_text("plain\ntext") == "plain text" and macos_calendar.error_text(None) == ""


# ---------------------------------------------------------------- the real adapter, on fake EventKit objects


class NSDate:
    def __init__(self, ts):
        self.ts = ts

    @classmethod
    def dateWithTimeIntervalSince1970_(cls, ts):
        return cls(ts)

    def timeIntervalSince1970(self):
        return self.ts


class Source:
    def __init__(self, title):
        self._title = title

    def title(self):
        return self._title


class Calendar:
    def __init__(self, title, source="iCloud", writable=True, ident=None):
        self._title, self._source, self._writable, self._ident = title, Source(source), writable, ident or f"id-{title}"

    def title(self):
        return self._title

    def source(self):
        return self._source

    def allowsContentModifications(self):
        return self._writable

    def calendarIdentifier(self):
        return self._ident


class World:
    def __init__(self, status=3, save_result=(True, None)):
        self.status = status
        self.save_result = save_result
        self.calendars: list = []
        self.default = None
        self.events: list = []  # existing EKEvents for find_similar_events
        self.built: list = []
        self.saves: list = []
        self.predicates: list = []


class ExistingEvent:
    def __init__(self, title, start, end, calendar, all_day=False):
        self._title, self._calendar, self._all_day = title, calendar, all_day
        self._start, self._end = NSDate(start.timestamp()), NSDate(end.timestamp())

    def title(self):
        return self._title

    def startDate(self):
        return self._start

    def endDate(self):
        return self._end

    def isAllDay(self):
        return self._all_day

    def location(self):
        return None

    def calendar(self):
        return self._calendar

    def status(self):
        return 1


def fake_eventkit(world: World) -> SimpleNamespace:
    class EKEvent:
        def __init__(self, store):
            self.store = store
            self.fields: dict = {}

        @classmethod
        def eventWithEventStore_(cls, store):
            event = cls(store)
            world.built.append(event)
            return event

        def setTitle_(self, value):
            self.fields["title"] = value

        def setStartDate_(self, value):
            self.fields["start"] = value.ts

        def setEndDate_(self, value):
            self.fields["end"] = value.ts

        def setAllDay_(self, value):
            self.fields["all_day"] = value

        def setLocation_(self, value):
            self.fields["location"] = value

        def setNotes_(self, value):
            self.fields["notes"] = value

        def setCalendar_(self, value):
            if value.title() == "터지는 캘린더":
                raise TypeError("depythonifying 'id'")
            self.fields["calendar"] = value

        def eventIdentifier(self):
            return f"EV-{len(world.saves)}"

    class EKEventStore:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

        @classmethod
        def authorizationStatusForEntityType_(cls, entity_type):
            return world.status

        def calendarsForEntityType_(self, entity_type):
            return list(world.calendars) if world.status == 3 else []

        def defaultCalendarForNewEvents(self):
            return world.default

        def saveEvent_span_commit_error_(self, event, span, commit, error):
            world.saves.append((event, span, commit, error))
            return world.save_result

        def predicateForEventsWithStartDate_endDate_calendars_(self, start, end, calendars):
            return (start, end, calendars)

        def eventsMatchingPredicate_(self, predicate):
            world.predicates.append(predicate)
            start, end, _calendars = predicate
            return [e for e in world.events if e.startDate().ts < end.ts and e.endDate().ts > start.ts]

    return SimpleNamespace(
        EKEventStore=EKEventStore,
        EKEvent=EKEvent,
        EKEntityTypeEvent=0,
        EKSpanThisEvent=0,
        EKAuthorizationStatusNotDetermined=0,
        EKAuthorizationStatusRestricted=1,
        EKAuthorizationStatusDenied=2,
        EKAuthorizationStatusFullAccess=3,
        EKAuthorizationStatusWriteOnly=4,
        EKEventStatusCanceled=3,
    )


def adapter(world, env=None):
    return EventKitCalendar(
        SEOUL, eventkit=fake_eventkit(world), foundation=SimpleNamespace(NSDate=NSDate), local_tz=SEOUL, env=env or {}
    )


def calendars_world(**kwargs):
    world = World(**kwargs)
    research, work, holidays = Calendar("연구"), Calendar("Work", "Exchange"), Calendar("대한민국 공휴일", "구독", writable=False)
    world.calendars = [research, work, holidays]
    world.default = research
    return world, research, work


def test_writable_calendars_and_the_default():
    world, research, work = calendars_world()
    assert adapter(world).list_writable_calendars() == [
        {"name": "연구", "source": "iCloud", "is_default": True},
        {"name": "Work", "source": "Exchange", "is_default": False},
    ]
    world.default = None
    assert all(not c["is_default"] for c in adapter(world).list_writable_calendars())
    world.status = 4  # write-only: no calendars to list
    assert adapter(world).list_writable_calendars() == []


def test_create_event_sets_every_field_and_saves_once():
    world, research, work = calendars_world()
    result = adapter(world).create_event(
        "신임교수모임 (10월)", at(10, 22, 12), at(10, 22, 13), False, location="본관 302호", notes="발표: 홍길동 교수님"
    )
    assert result == {"ok": True, "id": "EV-1", "calendar": "연구", "error": None}
    [event] = world.built
    assert event.fields == {
        "title": "신임교수모임 (10월)",
        "start": at(10, 22, 12).timestamp(),
        "end": at(10, 22, 13).timestamp(),
        "all_day": False,
        "location": "본관 302호",
        "notes": "발표: 홍길동 교수님",
        "calendar": research,
    }
    [(saved, span, commit, error)] = world.saves
    assert saved is event and (span, commit, error) == (0, True, None)


def test_all_day_events_end_on_their_last_day_eventkit_style():
    world, research, _ = calendars_world()
    adapter(world).create_event("체육대회", at(10, 23), at(10, 24), True)
    one_day = world.built[-1].fields
    assert (one_day["start"], one_day["end"], one_day["all_day"]) == (at(10, 23).timestamp(), at(10, 23).timestamp(), True)
    adapter(world).create_event("학회", at(10, 23), at(10, 26), True)
    three_days = world.built[-1].fields
    assert (three_days["start"], three_days["end"]) == (at(10, 23).timestamp(), at(10, 25).timestamp())
    assert "location" not in three_days and "notes" not in three_days


def test_create_event_picks_the_calendar_by_name_target_or_default():
    world, research, work = calendars_world()
    adapter(world).create_event("a", at(10, 22, 9), at(10, 22, 10), False, calendar_name=" WORK ")
    assert world.built[-1].fields["calendar"] is work
    adapter(world, env={"CALENDAR_WRITE_TARGET": unicodedata.normalize("NFD", "work")}).create_event(
        "b", at(10, 22, 9), at(10, 22, 10), False
    )
    assert world.built[-1].fields["calendar"] is work
    adapter(world, env={"CALENDAR_WRITE_TARGET": "Work"}).create_event(
        "c", at(10, 22, 9), at(10, 22, 10), False, calendar_name="연구"
    )
    assert world.built[-1].fields["calendar"] is research
    saves = len(world.saves)
    for name in ("가족", "대한민국 공휴일"):  # unknown, and read-only
        result = adapter(world).create_event("d", at(10, 22, 9), at(10, 22, 10), False, calendar_name=name)
        assert result["ok"] is False and result["id"] is None
        assert result["error"].endswith("일정을 추가할 수 있는 캘린더: 연구, Work")
    assert len(world.saves) == saves  # nothing saved


def test_create_event_permission_and_failures():
    for status, hint in ((2, "'전체 접근'"), (1, "기기 관리 정책"), (0, "--calendar-setup")):
        world, _, _ = calendars_world(status=status)
        result = adapter(world).create_event("x", at(10, 22, 9), at(10, 22, 10), False)
        assert result["ok"] is False and hint in result["error"] and world.saves == []
    # Write-only access may add to the default calendar.
    world, research, _ = calendars_world(status=4)
    assert adapter(world).create_event("x", at(10, 22, 9), at(10, 22, 10), False)["ok"] is True
    assert world.built[-1].fields["calendar"] is research
    world.default = None
    assert "기본 캘린더를 찾지 못했어요" in adapter(world).create_event("x", at(10, 22, 9), at(10, 22, 10), False)["error"]
    # EventKit says no: its own description is shown.
    error = SimpleNamespace(localizedDescription=lambda: "No calendar has been set.")
    world, _, _ = calendars_world(save_result=(False, error))
    assert adapter(world).create_event("x", at(10, 22, 9), at(10, 22, 10), False) == {
        "ok": False,
        "id": None,
        "calendar": "연구",
        "error": "캘린더에 저장하지 못했어요 (No calendar has been set.)",
    }
    world, _, _ = calendars_world(save_result=True)  # a bare BOOL
    assert adapter(world).create_event("x", at(10, 22, 9), at(10, 22, 10), False)["ok"] is True
    # A PyObjC exception is reported, never raised.
    world, _, _ = calendars_world()
    world.calendars.append(Calendar("터지는 캘린더"))
    result = adapter(world).create_event("x", at(10, 22, 9), at(10, 22, 10), False, calendar_name="터지는 캘린더")
    assert result["ok"] is False and "TypeError: depythonifying 'id'" in result["error"]


def test_find_similar_events_reads_two_hours_around_on_that_day_only():
    world, research, work = calendars_world()
    world.events = [
        ExistingEvent("신임교수모임", at(10, 22, 12), at(10, 22, 13), research),
        ExistingEvent("점심", at(10, 22, 11), at(10, 22, 12), work),
        ExistingEvent("세미나", at(10, 22, 12), at(10, 22, 13), work),
    ]
    found = adapter(world).find_similar_events(at(10, 22, 12), at(10, 22, 13), "신임교수모임 (10월)")
    start, end, calendars = world.predicates[-1]
    assert (start.ts, end.ts, calendars) == (at(10, 22, 10).timestamp(), at(10, 22, 15).timestamp(), None)
    assert [(r["title"], r["calendar"]) for r in found] == [("신임교수모임", "연구"), ("세미나", "Work")]


# ---------------------------------------------------------------- --calendar-setup


def test_calendar_setup_lists_where_new_events_go():
    app = FakeCalendarApp()
    assert write_target_lines(app, ENV) == [
        "일정을 추가할 수 있는 캘린더 2개 (메모로 일정 추가):",
        "    - 연구 (iCloud) [기본]",
        "    - Work (Exchange)",
        "추가할 캘린더: 연구 (CALENDAR_WRITE_TARGET이 비어 있어 기본 캘린더에 넣습니다)",
    ]
    assert write_target_lines(app, {**ENV, "CALENDAR_WRITE_TARGET": "work"})[-1] == "추가할 캘린더: Work (CALENDAR_WRITE_TARGET)"
    assert write_target_lines(app, {**ENV, "CALENDAR_WRITE_TARGET": "가족"})[-1].startswith("[경고] CALENDAR_WRITE_TARGET의 '가족'")

    class Broken(FakeCalendarApp):
        def list_writable_calendars(self):
            raise RuntimeError("EKErrorDomain")

    assert write_target_lines(Broken(), {})[0].startswith("[경고] 일정을 추가할 수 있는 캘린더를 확인하지 못했습니다")


def test_calendar_setup_run_shows_the_write_target():
    class SetupApp(FakeCalendarApp):
        def list_calendars(self):
            return [{"name": c["name"], "source": c["source"]} for c in self.writable]

    out = io.StringIO()
    code = run_calendar_setup(
        {**ENV, "CALENDAR_WRITE_TARGET": "Work"}, adapter_factory=lambda tz: SetupApp(), platform="darwin", now=NOW, out=out
    )
    assert code == 0
    text = out.getvalue()
    assert "일정을 추가할 수 있는 캘린더 2개 (메모로 일정 추가):\n    - 연구 (iCloud) [기본]\n    - Work (Exchange)\n" in text
    assert "추가할 캘린더: Work (CALENDAR_WRITE_TARGET)" in text
    assert text.index("추가할 캘린더") < text.index("연결 완료")


def test_calendar_write_target_setting():
    assert config.get_calendar_write_target({}) == ""
    assert config.get_calendar_write_target({"CALENDAR_WRITE_TARGET": "  연구   캘린더 "}) == "연구 캘린더"
    assert time(12, 0) == event_proposals.parse_clock("12:00") and event_proposals.parse_clock(None) is None


# ---------------------------------------------------------------- categories (CALENDAR_CATEGORIES)

from mungchi.tools.event_proposals import (  # noqa: E402
    CLARIFY,
    Answer,
    category_question,
    cli_prompt,
    parse_answer,
    proposal_question,
)

# Categories on: the default five (CALENDAR_CATEGORIES unset).
CAT_ENV = {"TIMEZONE": "Asia/Seoul"}
CATEGORY_NAMES = ["Family", "Teaching", "Research", "Event-Outside", "Event-KHU"]
CATEGORY_WRITABLE = [{"name": name, "source": "iCloud", "is_default": False} for name in CATEGORY_NAMES] + WRITABLE
QUESTION_KHU = (
    "카테고리를 골라주세요 (추천: Event-KHU) — 1 Family · 2 Teaching · 3 Research · 4 Event-Outside · 5 Event-KHU"
    " · 번호/이름으로 답하거나 '네'(추천대로), '아니요'(취소)"
)


def category_app(**kwargs):
    return FakeCalendarApp(writable=kwargs.pop("writable", CATEGORY_WRITABLE), **kwargs)


def run_categories(app, events, store=None, env=None, key=KEY, **args):
    """``run_propose`` with categories on (no CALENDAR_CATEGORIES in the env: the default five)."""
    store = store or StateStore(config.get_state_path())
    payload = run_propose(
        {"events": events, "source_note": NOTE, **args},
        key,
        env={**CAT_ENV, **(env or {})},
        now=NOW,
        store=store,
        adapter_factory=lambda tz: app,
        platform="darwin",
    )
    return payload, store


def category_creator(app, env=None):
    return lambda proposal: create_proposal_events(
        proposal, env={**CAT_ENV, **(env or {})}, now=NOW, adapter_factory=lambda tz: app, platform="darwin"
    )


def test_calendar_categories_setting():
    default = config.get_calendar_categories({})
    assert [c.label for c in default] == CATEGORY_NAMES and [c.calendar for c in default] == CATEGORY_NAMES
    assert dict((c.label, c.aliases) for c in default) == {
        "Family": ("가족", "집", "개인"),
        "Teaching": ("강의", "수업", "티칭", "교육"),
        "Research": ("연구",),
        "Event-Outside": ("외부", "외부행사", "학회"),
        "Event-KHU": ("경희", "학교", "교내", "khu"),
    }
    assert config.get_calendar_categories({"CALENDAR_CATEGORIES": ""}) == []  # off: the yes / no flow
    assert config.get_calendar_categories({"CALENDAR_CATEGORIES": "  ,  "}) == []
    custom = config.get_calendar_categories({"CALENDAR_CATEGORIES": "Family=가족, Research , research, 수업=  Teaching 2026 "})
    assert [(c.label, c.calendar) for c in custom] == [("Family", "가족"), ("Research", "Research"), ("수업", "Teaching 2026")]
    assert custom[0].aliases == ("가족", "집", "개인") and custom[2].aliases == ()
    aliased = config.get_calendar_categories({"CALENDAR_CATEGORY_ALIASES": "family=우리집|애들, Event-KHU=", "CALENDAR_CATEGORIES": "Family,Event-KHU"})
    assert [c.aliases for c in aliased] == [("우리집", "애들"), ()]
    many = ",".join(f"C{i}" for i in range(15))
    assert len(config.get_calendar_categories({"CALENDAR_CATEGORIES": many})) == config.MAX_CALENDAR_CATEGORIES


def test_the_category_question_and_the_cli_prompt():
    assert category_question(CATEGORY_NAMES, "Event-KHU") == QUESTION_KHU
    assert category_question(CATEGORY_NAMES[:2]) == "카테고리를 골라주세요 — 1 Family · 2 Teaching · 번호/이름으로 답하거나 '아니요'(취소)"
    assert category_question(CATEGORY_NAMES, assigned=True) == "일정마다 정한 카테고리로 추가할까요? (네 / 아니요 / 고칠 내용)"
    proposal = {"categories": [{"label": n, "calendar": n} for n in CATEGORY_NAMES], "suggested_category": "Research", "events": [{}]}
    assert cli_prompt(proposal) == (
        "카테고리를 골라주세요 (추천: Research) [1 Family · 2 Teaching · 3 Research · 4 Event-Outside · 5 Event-KHU / 네 / 아니요] "
    )
    assert cli_prompt({**proposal, "suggested_category": None}).endswith("5 Event-KHU / 아니요] ")
    assert cli_prompt({"events": [{}]}) == event_proposals.CLI_CONFIRM_PROMPT
    assert proposal_question({"events": [{}]}) == CONFIRM_QUESTION


def _pending(suggested="Event-KHU", events=({},)):
    categories = [{"label": c.label, "calendar": c.calendar, "aliases": list(c.aliases)} for c in config.get_calendar_categories({})]
    return {"id": "p1", "categories": categories, "suggested_category": suggested, "events": [dict(e) for e in events]}


@pytest.mark.parametrize(
    "text,label",
    [
        ("1", "Family"), ("2", "Teaching"), (" 5 ", "Event-KHU"), ("3번", "Research"), ("4번이요", "Event-Outside"), ("2️⃣", "Teaching"),
        ("Research", "Research"), ("research", "Research"), ("RESEARCH!", "Research"), ("event-khu", "Event-KHU"),
        ("Event KHU", "Event-KHU"), ("event_outside", "Event-Outside"),
        ("연구", "Research"), (unicodedata.normalize("NFD", "연구"), "Research"), ("가족", "Family"), ("집", "Family"),
        ("수업", "Teaching"), ("강의", "Teaching"), ("학회", "Event-Outside"), ("외부행사", "Event-Outside"),
        ("경희", "Event-KHU"), ("학교", "Event-KHU"), ("교내", "Event-KHU"), ("KHU", "Event-KHU"),
        # A part of one category's name.
        ("outside", "Event-Outside"), ("khu", "Event-KHU"), ("teach", "Teaching"), ("fam", "Family"),
        # Polite endings.
        ("Research로", "Research"), ("연구로 넣어줘", "Research"), ("khu로 해줘", "Event-KHU"), ("Teaching 캘린더에", "Teaching"),
        ("수업 캘린더로 넣어주세요", "Teaching"),
        # "네" means the suggestion.
        ("네", "Event-KHU"), ("응", "Event-KHU"), ("👍", "Event-KHU"), ("ok", "Event-KHU"),
    ],
)
def test_category_replies_pick_one_category(text, label):
    assert parse_answer(text, _pending()) == Answer(event_proposals.YES, label)


@pytest.mark.parametrize("text", ["아니요", "취소", "no", "됐어요"])
def test_negative_replies_cancel_a_category_proposal(text):
    assert parse_answer(text, _pending()) == Answer(NO)


def test_unclear_category_replies_are_asked_about_once_more():
    ambiguous = parse_answer("event", _pending())
    assert ambiguous.kind == CLARIFY and ambiguous.category is None
    assert ambiguous.message == "'event'에 맞는 카테고리가 여러 개예요: 4 Event-Outside · 5 Event-KHU. 번호나 전체 이름으로 골라 주세요."
    out_of_range = parse_answer("7", _pending())
    assert out_of_range.kind == CLARIFY and out_of_range.message.startswith("1~5 가운데 번호로 골라 주세요: 1 Family")
    no_suggestion = parse_answer("네", _pending(suggested=None))
    assert no_suggestion.kind == CLARIFY
    assert no_suggestion.message == (
        "추천한 카테고리가 없어요. 번호나 이름으로 골라 주세요: 1 Family · 2 Teaching · 3 Research · 4 Event-Outside · 5 Event-KHU"
    )


@pytest.mark.parametrize(
    "text",
    [
        "", "시간은 1시로 바꿔줘", "연구실 미팅 시간 바꿔줘", "네 근데 Research로", "1시", "2026", "Research 말고 다른 거", "e",
        "그냥 넣지 마세요 나중에",
        # Too short for a part of a name, or a part of an alias only.
        "re", "se", "행사", "외",
    ],
)
def test_anything_else_goes_to_the_model(text):
    assert parse_answer(text, _pending()) == Answer(None)


def test_without_categories_only_yes_and_no_count():
    old = {"id": "p1", "events": [{}]}
    assert parse_answer("네", old) == Answer(YES) and parse_answer("아니요", old) == Answer(NO)
    assert parse_answer("2", old) == Answer(None) and parse_answer("Research", old) == Answer(None)


def test_when_every_event_has_its_own_category_only_yes_and_no_count():
    assigned = _pending(suggested=None, events=({"category": "Research"}, {"category": "Event-KHU"}))
    assert parse_answer("네", assigned) == Answer(YES)  # each keeps its own category
    assert parse_answer("아니요", assigned) == Answer(NO)
    assert parse_answer("Family", assigned) == Answer(None)  # a change: for the model
    assert proposal_question(assigned) == event_proposals.ASSIGNED_QUESTION
    mixed = _pending(suggested=None, events=({"category": "Research"}, {}))
    assert parse_answer("네", mixed).kind == CLARIFY and parse_answer("1", mixed) == Answer(YES, "Family")


def test_category_proposal_is_stored_with_the_suggestion_and_asks_for_a_category():
    app = category_app()
    payload, store = run_categories(app, [EXAMPLE], suggested_category="event-khu")
    assert payload["ok"] and payload["can_confirm"]
    assert payload["categories"] == CATEGORY_NAMES and payload["suggested_category"] == "Event-KHU"
    assert payload["confirm_question"] == QUESTION_KHU
    assert "calendar" not in payload and payload["warnings"] == []
    # No calendar in the preview line: the user picks it.
    assert payload["preview"] == "• 10/22(목) 12:00–13:00 신임교수모임 (10월) · 메모: 발표: 홍길동 교수님"
    pending = store.pending_proposal(KEY, NOW)
    assert pending["suggested_category"] == "Event-KHU" and pending["calendar"] is None
    assert [(c["label"], c["calendar"]) for c in pending["categories"]] == [(n, n) for n in CATEGORY_NAMES]
    assert pending["categories"][4]["aliases"] == ["경희", "학교", "교내", "khu"]
    assert len(pending["id"]) == 32 and pending["id"] != run_categories(app, [EXAMPLE], store=store)[1].pending_proposal(KEY, NOW)["id"]
    assert app.created == []


def test_the_suggestion_is_matched_by_alias_or_dropped():
    app = category_app()
    by_alias, _ = run_categories(app, [EXAMPLE], suggested_category="학교")
    assert by_alias["suggested_category"] == "Event-KHU"
    by_calendar_arg, _ = run_categories(app, [EXAMPLE], calendar="연구")  # "연구 캘린더에 넣어줘"
    assert by_calendar_arg["suggested_category"] == "Research"
    unknown, store = run_categories(app, [EXAMPLE], suggested_category="Lecture")
    assert unknown["ok"] and unknown["suggested_category"] is None
    assert unknown["confirm_question"].startswith("카테고리를 골라주세요 — 1 Family")
    assert "'Lecture'은(는) 고를 수 있는 카테고리가 아니라서" in unknown["suggestion_problem"]
    assert store.pending_proposal(KEY, NOW)["suggested_category"] is None


def test_missing_category_calendars_are_not_offered_and_warned_about():
    writable = [{"name": n, "is_default": False} for n in ("Family", "research", "Event-Outside")] + WRITABLE
    payload, store = run_categories(category_app(writable=writable), [EXAMPLE], suggested_category="Event-KHU")
    assert payload["ok"] and payload["categories"] == ["Family", "Research", "Event-Outside"]
    note = "Mac 캘린더에 'Teaching', 'Event-KHU' 캘린더가 없어요. 캘린더 앱에서 그 이름으로 캘린더를 만들거나 .env의 CALENDAR_CATEGORIES를 고치세요."
    assert payload["warnings"] == [note] and payload["preview"].endswith(f"⚠️ {note}")
    assert payload["suggested_category"] is None  # Event-KHU is not offered
    pending = store.pending_proposal(KEY, NOW)
    # The calendar name as the Calendar app writes it.
    assert [(c["label"], c["calendar"]) for c in pending["categories"]] == [
        ("Family", "Family"), ("Research", "research"), ("Event-Outside", "Event-Outside")
    ]
    assert parse_answer("3", pending) == Answer(YES, "Event-Outside")
    none_there, store = run_categories(FakeCalendarApp(), [EXAMPLE])
    assert none_there["ok"] is False and store.pending_proposal(KEY, NOW) is None
    assert none_there["error"].startswith("Mac 캘린더에 카테고리 캘린더('Family', 'Teaching', 'Research', 'Event-Outside', 'Event-KHU')가 하나도 없어요.")


def test_categories_win_over_the_write_target_and_need_full_access():
    payload, _ = run_categories(category_app(), [EXAMPLE], env={"CALENDAR_WRITE_TARGET": "Work"})
    assert payload["categories"] == CATEGORY_NAMES and "calendar" not in payload
    write_only, store = run_categories(category_app(status=WRITE_ONLY), [EXAMPLE])
    assert write_only["ok"] is False and write_only["error"] == event_proposals.WRITE_ONLY_CATEGORY_TEXT
    assert "CALENDAR_CATEGORIES를 비우세요" in write_only["error"] and store.pending_proposal(KEY, NOW) is None


def test_empty_categories_keep_the_old_yes_no_flow():
    app = category_app()
    payload, store = run_categories(app, [{**EXAMPLE, "category": "Research"}], env={"CALENDAR_CATEGORIES": "", "CALENDAR_WRITE_TARGET": "Work"}, suggested_category="Research")
    assert payload["calendar"] == "Work" and payload["confirm_question"] == CONFIRM_QUESTION
    assert "categories" not in payload and "카테고리" not in payload["preview"]
    pending = store.pending_proposal(KEY, NOW)
    assert "categories" not in pending and "category" not in pending["events"][0]
    assert parse_answer("네", pending) == Answer(YES)
    assert confirm_proposal(store, KEY, YES, now=NOW, create=category_creator(app, {"CALENDAR_CATEGORIES": ""})).startswith(
        "✅ 캘린더에 추가했어요\n• 10/22(목) 12:00–13:00 신임교수모임 (10월) · 캘린더: Work"
    )
    assert [c["calendar_name"] for c in app.created] == ["Work"]


def test_creating_into_the_picked_category():
    app = category_app()
    _, store = run_categories(app, [EXAMPLE, {**EXAMPLE, "title": "신임교수모임 (11월)", "date": "2026-11-19"}], suggested_category="Event-KHU")
    reply = confirm_proposal(store, KEY, YES, now=NOW, create=category_creator(app), category="Event-KHU")
    assert [c["calendar_name"] for c in app.created] == ["Event-KHU", "Event-KHU"]
    assert reply == (
        "✅ Event-KHU 캘린더에 추가했어요\n"
        "• 10/22(목) 12:00–13:00 신임교수모임 (10월)\n"
        "• 11/19(목) 12:00–13:00 신임교수모임 (11월)"
    )
    assert store.pending_proposal(KEY, NOW) is None


def test_a_label_can_name_a_differently_named_calendar():
    app = category_app(writable=[{"name": "가족", "is_default": False}, {"name": "Research", "is_default": False}])
    payload, store = run_categories(app, [EXAMPLE], env={"CALENDAR_CATEGORIES": "Family=가족,Research"})
    assert payload["categories"] == ["Family", "Research"]
    assert parse_answer("집", store.pending_proposal(KEY, NOW)) == Answer(YES, "Family")
    confirm_proposal(store, KEY, YES, now=NOW, create=category_creator(app), category="Family")
    assert [c["calendar_name"] for c in app.created] == ["가족"]


def test_per_event_categories_override_the_pick():
    app = category_app()
    events = [{**EXAMPLE, "category": "research"}, {**EXAMPLE, "title": "학과 회의", "date": "2026-10-23", "weekday_in_text": None}]
    payload, store = run_categories(app, events, suggested_category="Event-KHU")
    assert [e.get("category") for e in payload["events"]] == ["Research", None]
    assert payload["preview"].splitlines()[0].endswith("· 카테고리: Research")
    pending = store.pending_proposal(KEY, NOW)
    assert [e.get("category") for e in pending["events"]] == ["Research", None]
    reply = confirm_proposal(store, KEY, YES, now=NOW, create=category_creator(app), category="Family")
    assert [(c["title"], c["calendar_name"]) for c in app.created] == [("신임교수모임 (10월)", "Research"), ("학과 회의", "Family")]
    assert reply.splitlines() == [
        "✅ 캘린더에 추가했어요",
        "• 10/22(목) 12:00–13:00 신임교수모임 (10월) · 캘린더: Research",
        "• 10/23(금) 12:00–13:00 학과 회의 · 캘린더: Family",
    ]


def test_per_event_categories_must_exist():
    writable = [{"name": n, "is_default": False} for n in ("Family", "Research")]
    unknown, store = run_categories(category_app(writable=writable), [{**EXAMPLE, "category": "Lecture"}])
    assert unknown["ok"] is False and store.pending_proposal(KEY, NOW) is None
    assert unknown["errors"] == ["1번 일정: 'Lecture'은(는) 고를 수 있는 카테고리가 아니에요 (고를 수 있는 카테고리: Family, Research)."]
    missing, _ = run_categories(category_app(writable=writable), [{**EXAMPLE, "category": "Event-KHU"}], store=store)
    assert missing["errors"][0].startswith("1번 일정: Mac 캘린더에 'Event-KHU' 캘린더가 없어요.")


def test_every_event_with_its_own_category_needs_only_a_yes():
    app = category_app()
    events = [{**EXAMPLE, "category": "Research"}, {**EXAMPLE, "title": "학회", "date": "2026-10-24", "weekday_in_text": None, "category": "학회"}]
    payload, store = run_categories(app, events)
    assert payload["confirm_question"] == event_proposals.ASSIGNED_QUESTION
    confirm_proposal(store, KEY, YES, now=NOW, create=category_creator(app))
    assert [c["calendar_name"] for c in app.created] == ["Research", "Event-Outside"]


def test_no_event_is_created_without_a_category():
    app = category_app()
    _, store = run_categories(app, [EXAMPLE])
    reply = confirm_proposal(store, KEY, YES, now=NOW, create=category_creator(app))  # no pick: never a guess
    assert app.created == [] and reply == (
        "❌ 캘린더에 추가하지 못했어요\n❌ 10/22(목) 12:00–13:00 신임교수모임 (10월) 추가 실패: 카테고리를 고르지 않아 추가하지 않았어요"
    )


def test_taking_a_proposal_by_id_only_takes_that_one(tmp_path):
    store = StateStore(tmp_path / "s.json")
    store.save_pending_proposal(KEY, {"id": "new", "events": []}, NOW)
    assert store.take_pending_proposal(KEY, NOW, proposal_id="old") is None
    assert store.pending_proposal(KEY, NOW)["id"] == "new"  # left as it is
    assert store.take_pending_proposal(KEY, NOW, proposal_id="new")["id"] == "new"
    assert store.take_pending_proposal(KEY, NOW, proposal_id="new") is None


def test_calendar_setup_lists_the_categories():
    writable = [{"name": n, "source": "iCloud", "is_default": False} for n in ("Family", "Research")]
    lines = write_target_lines(FakeCalendarApp(writable=writable), {"CALENDAR_CATEGORIES": "Family,Research,Event-KHU"})
    assert lines[-2:] == [
        "카테고리 (CALENDAR_CATEGORIES, 일정을 추가할 때 고릅니다): Family, Research",
        "[경고] Mac 캘린더에 'Event-KHU' 캘린더가 없어요. 캘린더 앱에서 그 이름으로 캘린더를 만들거나 .env의 CALENDAR_CATEGORIES를 고치세요.",
    ]
    mapped = write_target_lines(FakeCalendarApp(writable=[{"name": "가족"}]), {"CALENDAR_CATEGORIES": "Family=가족"})
    assert mapped[-1] == "카테고리 (CALENDAR_CATEGORIES, 일정을 추가할 때 고릅니다): Family(가족)"


def test_env_example_lists_the_default_categories():
    from pathlib import Path

    from dotenv import dotenv_values

    values = dotenv_values(Path(__file__).resolve().parents[1] / ".env.example")
    assert values["CALENDAR_CATEGORIES"] == config.DEFAULT_CALENDAR_CATEGORIES
    assert values["CALENDAR_CATEGORY_ALIASES"] == ""
    assert config.get_calendar_categories(values) == config.get_calendar_categories({})
