from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from mungchi import config
from mungchi.tools import calendar_tool
from mungchi.tools.calendar_tool import build_schedule, get_schedule, parse_events, run_schedule
from mungchi.tools.common import scrub

SEOUL = ZoneInfo("Asia/Seoul")
# Monday 2026-10-05, 10:30 in Seoul.
CLOCK = datetime(2026, 10, 5, 10, 30, tzinfo=SEOUL)
SECRET_URL = "https://calendar.google.com/calendar/ical/me%40gmail.com/private-0123456789abcdef/basic.ics"

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//mungchi tests//EN
X-WR-CALNAME:연구 일정
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
UID:seminar-1
DTSTART:20261005T050000Z
DURATION:PT1H
SUMMARY:세미나
END:VEVENT
BEGIN:VEVENT
UID:class-1
DTSTART;TZID=Asia/Seoul:20261006T090000
DTEND;TZID=Asia/Seoul:20261006T100000
SUMMARY:수업
END:VEVENT
BEGIN:VEVENT
UID:cancelled-1
DTSTART;TZID=Asia/Seoul:20261005T160000
DTEND;TZID=Asia/Seoul:20261005T170000
STATUS:CANCELLED
SUMMARY:취소된 회의
END:VEVENT
BEGIN:VEVENT
UID:later-1
DTSTART;TZID=Asia/Seoul:20261009T090000
DTEND;TZID=Asia/Seoul:20261009T100000
SUMMARY:창 밖 일정
END:VEVENT
END:VCALENDAR
"""


def schedule():
    events = parse_events(ICS, "캘린더 1", SEOUL, date(2026, 10, 5), date(2026, 10, 8))
    return build_schedule(events, SEOUL, date(2026, 10, 5), 2, CLOCK)


def test_recurring_and_all_day_events_are_expanded_and_sorted():
    result = schedule()
    titles = [e["title"] for e in result["events"]]
    assert titles == ["학회 초록 마감", "주간 랩미팅", "지도교수 면담", "세미나", "수업"]
    weekly = result["events"][1]
    assert weekly["start"] == "2026-10-05T10:00+09:00"
    assert weekly["end"] == "2026-10-05T11:00+09:00"
    assert weekly["location"] == "302호"
    assert weekly["calendar"] == "연구 일정"
    all_day = result["events"][0]
    assert all_day == {
        "start": "2026-10-05",
        "end": "2026-10-05",
        "all_day": True,
        "title": "학회 초록 마감",
        "location": "",
        "calendar": "연구 일정",
    }
    # UTC event with DURATION is converted to local time.
    assert result["events"][3]["start"] == "2026-10-05T14:00+09:00"
    assert result["events"][3]["end"] == "2026-10-05T15:00+09:00"
    assert result["range"] == {"start": "2026-10-05", "end": "2026-10-06", "days": 2}


def test_now_and_next_event_use_injected_clock():
    result = schedule()
    assert result["current_time"] == "2026-10-05T10:30+09:00"
    assert {e["title"] for e in result["now"]} == {"주간 랩미팅", "학회 초록 마감"}
    assert result["next_event"]["title"] == "지도교수 면담"


def test_overlaps_and_gaps():
    result = schedule()
    assert result["overlaps"] == [
        {"events": ["지도교수 면담", "세미나"], "from": "2026-10-05T14:00+09:00", "to": "2026-10-05T14:30+09:00"}
    ]
    assert result["gaps"] == [{"date": "2026-10-05", "from": "11:00", "to": "13:00", "minutes": 120}]


def test_run_schedule_with_fake_fetcher_and_failed_feed_hides_url():
    def fetch(url):
        if url == SECRET_URL:
            return ICS.encode()
        raise RuntimeError(f"connection failed for {url}")

    other = "https://outlook.office365.com/owa/calendar/abc/secret-key-xyz/calendar.ics"
    payload = run_schedule(
        "",
        2,
        env={"CALENDAR_ICS_URLS": f"{SECRET_URL}, {other}", "TIMEZONE": "Asia/Seoul"},
        now=CLOCK,
        fetcher=fetch,
    )
    text = json.dumps(payload, ensure_ascii=False)
    assert payload["ok"] is True
    assert payload["range"]["start"] == "2026-10-05"
    assert payload["next_event"]["title"] == "지도교수 면담"
    assert payload["errors"][0]["calendar"] == "캘린더 2"
    assert SECRET_URL not in text and other not in text and "secret-key-xyz" not in text


def test_run_schedule_rejects_bad_date():
    payload = run_schedule("10/05/2026", env={"CALENDAR_ICS_URLS": SECRET_URL}, now=CLOCK, fetcher=lambda u: ICS)
    assert payload["ok"] is False
    assert "YYYY-MM-DD" in payload["error"]


def test_explicit_date_window_still_reports_now_relative_to_clock():
    payload = run_schedule(
        "2026-10-06", 1, env={"CALENDAR_ICS_URLS": SECRET_URL}, now=CLOCK, fetcher=lambda u: ICS
    )
    assert [e["title"] for e in payload["events"]] == ["수업"]
    assert payload["next_event"]["title"] == "지도교수 면담"


def test_unconfigured_calendar():
    payload = run_schedule()
    assert payload == {
        "configured": False,
        "missing": ["CALENDAR_ICS_URLS"],
        "hint": payload["hint"],
    }
    assert "CALENDAR_ICS_URLS" in payload["hint"] and "iCal" in payload["hint"]


def test_tool_handler_unconfigured_returns_json():
    result = asyncio.run(get_schedule.handler({}))
    data = json.loads(result["content"][0]["text"])
    assert data["configured"] is False
    assert data["missing"] == ["CALENDAR_ICS_URLS"]


# ---------------------------------------------------------------- webcal:// (iCloud public calendars)

ICLOUD_KEY = "MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY3ODkwYWJjZGVm"
ICLOUD_WEBCAL = f"webcal://p42-caldav.icloud.com/published/2/{ICLOUD_KEY}"
ICLOUD_HTTPS = f"https://p42-caldav.icloud.com/published/2/{ICLOUD_KEY}"
WEBCALS = "webcals://cal.example.com/pub/secret-feed-key-0123456789.ics"


def test_ics_fetch_url_turns_webcal_and_webcals_into_https():
    assert config.ics_fetch_url(ICLOUD_WEBCAL) == ICLOUD_HTTPS
    assert config.ics_fetch_url(WEBCALS) == "https://cal.example.com/pub/secret-feed-key-0123456789.ics"
    assert config.ics_fetch_url("WEBCAL://Example.com/a.ics") == "https://Example.com/a.ics"
    assert config.ics_fetch_url("Webcals://example.com/b.ics") == "https://example.com/b.ics"
    # Everything else is left as it is.
    assert config.ics_fetch_url(SECRET_URL) == SECRET_URL
    assert config.ics_fetch_url(f"  {SECRET_URL} ") == SECRET_URL
    assert config.ics_fetch_url("http://example.com/c.ics") == "http://example.com/c.ics"
    assert config.ics_fetch_url("https://example.com/webcal://x") == "https://example.com/webcal://x"


def test_mixed_comma_separated_list_is_fetched_over_https():
    raw = f"{ICLOUD_WEBCAL}, {SECRET_URL} ,{WEBCALS}"
    cfg = config.load_calendar_config({"CALENDAR_ICS_URLS": raw})
    assert cfg.urls == [ICLOUD_HTTPS, SECRET_URL, "https://cal.example.com/pub/secret-feed-key-0123456789.ics"]

    fetched: list[str] = []

    def fetch(url):
        fetched.append(url)
        return ICS.encode()

    payload = run_schedule("", 2, env={"CALENDAR_ICS_URLS": raw, "TIMEZONE": "Asia/Seoul"}, now=CLOCK, fetcher=fetch)
    assert fetched == cfg.urls
    assert payload["ok"] is True and "errors" not in payload


def test_fetch_ics_requests_https_for_webcal(monkeypatch):
    seen: list[str] = []
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=ICS.encode())

    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    assert calendar_tool.fetch_ics(ICLOUD_WEBCAL) == ICS.encode()
    assert calendar_tool.fetch_ics(WEBCALS) == ICS.encode()
    assert seen == [ICLOUD_HTTPS, "https://cal.example.com/pub/secret-feed-key-0123456789.ics"]


def test_webcal_urls_never_appear_in_errors_or_logs():
    def fetch(url):
        raise RuntimeError(f"connection failed for {url} (configured as {ICLOUD_WEBCAL}, {WEBCALS})")

    env = {"CALENDAR_ICS_URLS": f"{ICLOUD_WEBCAL},{WEBCALS}"}
    payload = run_schedule("", 2, env=env, now=CLOCK, fetcher=fetch)
    text = json.dumps(payload, ensure_ascii=False)
    assert payload["ok"] is False and len(payload["errors"]) == 2
    assert ICLOUD_KEY not in text and "secret-feed-key" not in text

    # Every form of the address is a configured secret, for scrubbing logs and Slack replies.
    secrets = config.secret_values(env)
    message = (
        f"GET {ICLOUD_HTTPS} -> 404; raw {ICLOUD_WEBCAL}; host p42-caldav.icloud.com/published/2/{ICLOUD_KEY}; "
        "also https://cal.example.com/pub/secret-feed-key-0123456789.ics and " + WEBCALS
    )
    cleaned = scrub(message, secrets)
    assert ICLOUD_KEY not in cleaned and "secret-feed-key" not in cleaned
    # URL redaction (used by the calendar tool) also knows webcals://.
    redacted = scrub(f"see {WEBCALS} and {ICLOUD_WEBCAL}", secrets=[], redact_urls=True)
    assert "secret-feed-key" not in redacted and ICLOUD_KEY not in redacted
