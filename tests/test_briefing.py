"""The relay morning briefing's pieces (greeting, reports, relay order, terminal) and the "is it due?" check.

No network, no real Slack or LLM: the 업뎃 / 일정 runs are a fake ``run_turn``,
the greeting a fake generator (conftest makes the real one fail, so the
template is used), and the credits and weather come from fake fetches.
Slack delivery is tested in ``test_relay_briefing.py``.
"""

from __future__ import annotations

import asyncio
import io
import random
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from mungchi import briefing, config, credits, phrases, weather
from mungchi.agents import ACCURACY_RULE, VOICES
from mungchi.briefing import (
    ALREADY,
    DAY_OFF,
    DUE,
    EARLY,
    MISSED,
    OFF,
    GREETING_SYSTEM_PROMPT,
    brief_due,
    credit_section,
    fallback_greeting,
    greeting_prompt,
    make_greeting,
    report_prompt,
    run_brief_cli,
    start_relay,
    valid_greeting,
)
from mungchi.main import TurnResult, main
from mungchi.slack_format import SLACK_FORMAT_PROMPT
from mungchi.state import StateStore

SEOUL = ZoneInfo("Asia/Seoul")
UPDATE_SESSION = "22222222-2222-2222-2222-222222222222"
SCHEDULE_SESSION = "33333333-3333-3333-3333-333333333333"
UPDATE_REPORT = "업뎃 보고드립니다!\n공저자 변경 없음 (Dropbox): 지난 브리핑(10/07 07:00) 이후 바뀐 파일이 없어요"
SCHEDULE_REPORT = "좋은 아침이에요! 오늘은 여유로운 편이에요 😊\n10/08 (목)\n• 10:00–11:00 랩 미팅 (302호)"
GREETING = "똑똑! 🚪 10월 8일(목) 아침이에요. 오늘도 같이 챙겨 볼게요!"


def seoul(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment in October 2026, Seoul time (10/08 is a Thursday, 10/10 a Saturday)."""
    return datetime(2026, 10, day, hour, minute, tzinfo=SEOUL)


def schedule(**env: str) -> config.BriefSchedule:
    return config.load_brief_schedule({"BRIEF_TIME": "07:00", **env})


class PersonaRun:
    """Stands in for ``run_turn``: one scripted result (or exception, or delay) per persona; records calls."""

    def __init__(self, **results):
        self.results = {
            "update": TurnResult(text=UPDATE_REPORT, session_id=UPDATE_SESSION),
            "schedule": TurnResult(text=SCHEDULE_REPORT, session_id=SCHEDULE_SESSION),
            **results,
        }
        self.delays: dict[str, float] = {}
        self.calls: list[dict] = []

    async def __call__(self, prompt, *, resume=None, on_status=None, extra_system_prompt="", persona="mungchi", briefing=False, conversation_key=None, images=None):
        self.calls.append(
            {"prompt": prompt, "extra_system_prompt": extra_system_prompt, "persona": persona, "briefing": briefing, "resume": resume}
        )
        if self.delays.get(persona):
            await asyncio.sleep(self.delays[persona])
        result = self.results[persona]
        if isinstance(result, BaseException):
            raise result
        return result

    def call(self, persona: str) -> dict:
        [call] = [c for c in self.calls if c["persona"] == persona]
        return call


class FakeGreeting:
    """Stands in for the greeting's LLM call: records prompts, returns ``reply`` (or raises / sleeps)."""

    def __init__(self, reply=GREETING, delay=0.0):
        self.reply = reply
        self.delay = delay
        self.prompts: list[str] = []

    async def __call__(self, prompt):
        self.prompts.append(prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


def report(remaining=9050.5):
    payload = {
        "monthly_allocated": {"quota": 10000, "used": 10000 - remaining, "remaining": remaining, "renewal_date": "2026-11-01T00:00:00+09:00"},
        "total": {"quota": 10000, "used": 10000 - remaining, "remaining": remaining},
    }
    return credits.CreditReport(balance=credits.parse_balance(payload))


SUNNY = weather.WeatherReport(
    label="서울",
    forecast=weather.Forecast(code=1, low=11.5, high=22.6, rain_chance=10),
    air=weather.AirQuality(pm10=42.3, pm2_5=12.0),
)
WEATHER_LINE = "🌤️ 서울 날씨: 대체로 맑음 · 최저 12° / 최고 23° · 강수확률 10% · 미세먼지 보통"
CREDIT_LINE = "💳 Chat KHU 크레딧: 9,050.5 남음 / 10,000 (90.5%) · 11/01 갱신"
WEATHER_ON: dict[str, str] = {}  # an explicit env without BRIEF_WEATHER: the default, on


# ---------------------------------------------------------------- the schedule (BRIEF_TIME, BRIEF_DAYS, ...)


def test_schedule_parsing():
    on = schedule()
    assert on.enabled and on.at == time(7, 0) and on.days == "daily"
    assert on.catchup_until == time(12, 0) and on.timezone_name == "Asia/Seoul"
    assert on.describe() == "매일 07:00 (Asia/Seoul)" and on.warnings == ()
    assert config.load_brief_schedule({"BRIEF_TIME": "7:05", "BRIEF_DAYS": "weekdays"}).describe() == "평일 07:05 (Asia/Seoul)"
    assert config.load_brief_schedule({"BRIEF_TIME": "06:30", "BRIEF_DAYS": "평일"}).days == "weekdays"
    assert schedule(TIMEZONE="Europe/Berlin").describe() == "매일 07:00 (Europe/Berlin)"


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_or_unset_brief_time_turns_it_off(value):
    for env in ({}, {"BRIEF_TIME": value}):
        off = config.load_brief_schedule(env)
        assert not off.enabled and off.warnings == ()
        assert off.describe() == "꺼짐 (BRIEF_TIME 미설정)"
        assert brief_due(off, seoul(8, 7), None) == OFF


@pytest.mark.parametrize("value", ["7시", "25:00", "07:60", "0700", "7:5", "아침"])
def test_invalid_brief_time_turns_it_off_with_a_korean_warning(value):
    off = config.load_brief_schedule({"BRIEF_TIME": value})
    assert not off.enabled
    assert off.describe() == "꺼짐 (BRIEF_TIME 값이 잘못됨)"
    [warning] = off.warnings
    assert f"BRIEF_TIME 값 '{value}'" in warning and "07:00처럼" in warning
    assert brief_due(off, seoul(8, 7), None) == OFF


def test_invalid_days_and_catchup_keep_defaults_with_warnings():
    odd = schedule(BRIEF_DAYS="sometimes", BRIEF_CATCHUP_UNTIL="noon")
    assert odd.enabled and odd.days == "daily" and odd.catchup_until == time(12, 0)
    assert len(odd.warnings) == 2 and "BRIEF_DAYS" in odd.warnings[0] and "BRIEF_CATCHUP_UNTIL" in odd.warnings[1]
    assert schedule(BRIEF_CATCHUP_UNTIL="10:30").catchup_until == time(10, 30)
    assert schedule(BRIEF_CATCHUP_UNTIL="24:00").catchup_until is None
    # A briefing later than the cutoff still goes out: it catches up until midnight instead.
    late = config.load_brief_schedule({"BRIEF_TIME": "13:00"})
    assert late.catchup_until is None and late.warnings == ()
    assert brief_due(late, seoul(8, 23, 59), None) == DUE
    explicit = config.load_brief_schedule({"BRIEF_TIME": "13:00", "BRIEF_CATCHUP_UNTIL": "12:00"})
    assert explicit.catchup_until is None and "자정" in explicit.warnings[0]


# ---------------------------------------------------------------- due or not


@pytest.mark.parametrize(
    "now,last,expected",
    [
        (seoul(8, 6, 59), None, EARLY),  # before 07:00
        (seoul(8, 7, 0), None, DUE),  # 07:00 sharp
        (seoul(8, 8, 10), None, DUE),  # started late / Mac woke up at 08:10: still today's briefing
        (seoul(8, 8, 10), "2026-10-07", DUE),  # yesterday's does not count
        (seoul(8, 11, 59), None, DUE),
        (seoul(8, 12, 0), None, MISSED),  # the catch-up cutoff
        (seoul(8, 12, 30), None, MISSED),  # after noon: skipped for the day
        (seoul(8, 7, 0), "2026-10-08", ALREADY),  # already sent today
        (seoul(8, 12, 30), "2026-10-08", ALREADY),
    ],
)
def test_brief_due(now, last, expected):
    assert brief_due(schedule(), now, last) == expected


def test_brief_due_on_weekdays_only():
    weekdays = schedule(BRIEF_DAYS="weekdays")
    assert brief_due(weekdays, seoul(10, 7, 30), None) == DAY_OFF  # Saturday
    assert brief_due(weekdays, seoul(11, 7, 30), None) == DAY_OFF  # Sunday
    assert brief_due(weekdays, seoul(12, 7, 30), None) == DUE  # Monday
    assert brief_due(schedule(), seoul(10, 7, 30), None) == DUE  # daily: Saturday too


def test_brief_due_reads_the_wall_clock_in_timezone_from_any_aware_clock():
    # The clock may be UTC (as utcnow() is): 22:10 UTC on 10/07 is 07:10 on 10/08 in Seoul.
    assert brief_due(schedule(), datetime(2026, 10, 7, 22, 10, tzinfo=timezone.utc), "2026-10-07") == DUE
    assert brief_due(schedule(), datetime(2026, 10, 7, 21, 50, tzinfo=timezone.utc), None) == EARLY
    # A zone with DST (Europe/Berlin switches on 2026-10-25): wall-clock 07:00 on both sides.
    berlin = schedule(TIMEZONE="Europe/Berlin")
    assert brief_due(berlin, datetime(2026, 10, 24, 5, 0, tzinfo=timezone.utc), None) == DUE  # 07:00 CEST
    assert brief_due(berlin, datetime(2026, 10, 26, 5, 59, tzinfo=timezone.utc), None) == EARLY  # 06:59 CET
    assert brief_due(berlin, datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc), None) == DUE  # 07:00 CET


# ---------------------------------------------------------------- 고뭉치's greeting


def test_a_valid_llm_greeting_is_used_and_stored_for_tomorrow():
    store = StateStore(config.get_state_path())
    generate = FakeGreeting()
    greeting = asyncio.run(make_greeting(seoul(8, 7), weather_text=WEATHER_LINE, store=store, generate=generate))
    assert (greeting.text, greeting.source) == (GREETING, "llm")
    assert store.last_greeting() == ("2026-10-08", GREETING)
    [prompt] = generate.prompts
    # The date comes from code; the weather goes in as words only (no numbers to copy).
    assert "- 오늘 날짜: 2026년 10월 8일 목요일 (짧게 쓰면 10월 8일(목))" in prompt
    assert "- 오늘은 평일이에요." in prompt
    assert "- 날씨 요약: 대체로 맑음 · 미세먼지 보통" in prompt
    assert not re.search(r"\d+°|\d+%", prompt)
    assert "이전 인사" not in prompt  # nothing stored yet


def test_yesterdays_greeting_is_passed_and_todays_is_stored():
    store = StateStore(config.get_state_path())
    yesterday = "좋은 아침이에요! 10월 7일 수요일 브리핑 시작할게요 🙂"
    store.mark_greeting("2026-10-07", yesterday)
    generate = FakeGreeting()
    greeting = asyncio.run(make_greeting(seoul(8, 7), store=store, generate=generate))
    assert f'- 이전 인사: "{yesterday}" (이 인사와 다르게 시작하고, 같은 표현은 쓰지 마)' in generate.prompts[0]
    assert greeting.source == "llm" and store.last_greeting() == ("2026-10-08", GREETING)


@pytest.mark.parametrize(
    "reply",
    [
        "좋은 아침이에요! 10월 9일(금) 브리핑입니다.",  # wrong day
        "좋은 아침이에요! 10월 8일(금) 브리핑입니다.",  # wrong weekday
        "2025년 10월 8일 아침 브리핑입니다.",  # wrong year
        "좋은 아침이에요! 오늘도 힘내요.",  # no date at all
        "10월 8일(목), 오늘 최고 23도예요!",  # a number that is not the date
        "<@U123> 10월 8일(목) 아침이에요",  # a mention
        "똑똑! 10월 8일(목) " + "아주 " * 40 + "좋은 아침이에요",  # far too long
        "10월 8일(목)\n아침\n브리핑",  # three lines
        "",
        RuntimeError("gateway down"),
    ],
)
def test_an_invalid_or_failed_greeting_falls_back_to_a_template(reply):
    store = StateStore(config.get_state_path())
    greeting = asyncio.run(make_greeting(seoul(8, 7), store=store, generate=FakeGreeting(reply)))
    assert greeting.source == "template"
    assert greeting.text in [t.format(**briefing.date_fields(seoul(8, 7))) for t in phrases.GREETING_TEMPLATES]
    assert "10월 8일" in greeting.text and valid_greeting(greeting.text, seoul(8, 7))
    assert store.last_greeting() == ("2026-10-08", greeting.text)


def test_a_slow_greeting_times_out_into_a_template():
    greeting = asyncio.run(make_greeting(seoul(8, 7), generate=FakeGreeting(delay=5), timeout=0.01))
    assert greeting.source == "template" and "10월 8일" in greeting.text


def test_the_same_greeting_as_last_time_is_not_used_again():
    store = StateStore(config.get_state_path())
    store.mark_greeting("2026-10-08", GREETING)  # e.g. an earlier briefing on request today
    greeting = asyncio.run(make_greeting(seoul(8, 9), store=store, generate=FakeGreeting(GREETING)))
    assert greeting.source == "template" and greeting.text != GREETING


def test_conftest_keeps_the_real_greeting_call_away_from_any_model():
    greeting = asyncio.run(make_greeting(seoul(8, 7)))  # no generator: the (patched) default fails
    assert greeting.source == "template"


def test_valid_greeting_accepts_todays_date_in_any_usual_form():
    now = seoul(8, 7)
    for text in (
        "똑똑! 🚪 2026년 10월 8일(목) 아침 브리핑입니다~",
        "좋은 아침이에요! 10월 8일 목요일 브리핑 시작할게요 ☀️",
        "10/8(목) 아침이에요. 오늘도 화이팅!",
        "좋은 아침이에요.\n10월 8일 아침 브리핑입니다.",
    ):
        assert valid_greeting(text, now), text


def test_every_fallback_template_is_a_valid_greeting_on_every_day():
    day = date(2026, 1, 1)
    while day.year == 2026:
        now = datetime.combine(day, time(7), tzinfo=SEOUL)
        for template in briefing.greeting_templates(now):
            text = template.format(**briefing.date_fields(now))
            assert valid_greeting(text, now), text
        day += timedelta(days=1)
    # Weekend and Monday get their own touch as well.
    assert set(phrases.WEEKEND_GREETING_TEMPLATES) <= set(briefing.greeting_templates(seoul(10, 7)))
    assert set(phrases.MONDAY_GREETING_TEMPLATES) <= set(briefing.greeting_templates(seoul(12, 7)))
    assert not set(phrases.WEEKEND_GREETING_TEMPLATES) & set(briefing.greeting_templates(seoul(8, 7)))


def test_the_fallback_avoids_yesterdays_template_and_is_seedable():
    first = phrases.GREETING_TEMPLATES[0]
    yesterday = ("2026-10-07", first.format(**briefing.date_fields(date(2026, 10, 7))))
    picks = {fallback_greeting(seoul(8, 7), rng=random.Random(seed), previous=yesterday) for seed in range(50)}
    assert first.format(**briefing.date_fields(seoul(8, 7))) not in picks and len(picks) >= 3
    assert fallback_greeting(seoul(8, 7), rng=random.Random(4)) == fallback_greeting(seoul(8, 7), rng=random.Random(4))


def test_the_greeting_system_prompt_is_constant_and_in_mungchis_voice():
    assert VOICES["mungchi"] in GREETING_SYSTEM_PROMPT and ACCURACY_RULE in GREETING_SYSTEM_PROMPT
    assert "1~2문장" in GREETING_SYSTEM_PROMPT and "숫자는 날짜에만 쓴다" in GREETING_SYSTEM_PROMPT
    assert not re.search(r"\d{4}-\d{2}-\d{2}|\d{1,2}월 \d{1,2}일", GREETING_SYSTEM_PROMPT)  # no date: cache-stable
    # The date is only ever in the per-day user prompt.
    assert greeting_prompt(seoul(8, 7)) != greeting_prompt(seoul(9, 7))
    assert "주말(토요일)" in greeting_prompt(seoul(10, 7)) and "월요일" in greeting_prompt(seoul(12, 7))


def test_the_real_greeting_call_is_one_tool_less_turn(monkeypatch):
    from mungchi import main as main_module
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    seen = {}

    class OneTurnClient:
        def __init__(self, options=None):
            seen["options"] = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def query(self, prompt, session_id="default"):
            seen["prompt"] = prompt

        async def receive_response(self):
            yield AssistantMessage(content=[TextBlock(text=GREETING)], model="m")
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s")

    monkeypatch.setattr(main_module, "ClaudeSDKClient", OneTurnClient)
    assert asyncio.run(briefing.llm_greeting("인사 써 줘")) == GREETING
    options = seen["options"]
    assert options.system_prompt == GREETING_SYSTEM_PROMPT and seen["prompt"] == "인사 써 줘"
    assert options.tools == [] and options.allowed_tools == [] and options.mcp_servers == {} and not options.agents
    assert options.max_turns == 1 and options.setting_sources == [] and options.model == config.get_model()


# ---------------------------------------------------------------- 업뎃's and 일정's own reports


def test_report_prompts_ask_for_their_own_part_only():
    update, schedule_ = report_prompt("update", seoul(8, 7)), report_prompt("schedule", seoul(8, 7))
    assert "since_hours 없이(0)" in update and "지난 브리핑 이후" in update and "since_basis" in update
    assert '"업뎃 보고드립니다!"' in update
    assert 'date="2026-10-08", days=1' in schedule_ and "오늘(2026-10-08 (목요일))" in schedule_
    assert "get_weather는 부르지 마" in schedule_
    for prompt in (update, schedule_):
        assert "날씨, 크레딧" in prompt and "멘션하지 마" in prompt and "고뭉치가 인사와 날씨, Chat KHU 크레딧을 이미 전했고" in prompt


def _relay(run, **kwargs):
    async def go():
        async with start_relay(
            run=run,
            now=seoul(8, 7),
            env=WEATHER_ON,
            credit_fetch=report,
            weather_fetch=lambda: SUNNY,
            greeting_generate=FakeGreeting(),
            rng=random.Random(2),
            **kwargs,
        ) as relay:
            head = await relay.head()
            return head, {persona: await relay.report(persona) for persona in relay.personas}

    return asyncio.run(go())


@pytest.mark.parametrize("slack", [False, True])
def test_the_relay_runs_update_in_briefing_mode_and_schedule_for_today(slack):
    run = PersonaRun()
    head, reports = _relay(run, slack=slack)
    update, schedule_ = run.call("update"), run.call("schedule")
    assert update["briefing"] is True and schedule_["briefing"] is False  # only 업뎃's Dropbox checkpoint moves
    assert update["prompt"] == report_prompt("update", seoul(8, 7)) and schedule_["prompt"] == report_prompt("schedule", seoul(8, 7))
    assert update["extra_system_prompt"] == schedule_["extra_system_prompt"] == (SLACK_FORMAT_PROMPT if slack else "")
    assert update["resume"] is None and schedule_["resume"] is None
    assert reports["update"].text == UPDATE_REPORT and reports["schedule"].text == SCHEDULE_REPORT
    assert reports["update"].session_id == UPDATE_SESSION and not reports["update"].failed
    # Weather and credits: only in 고뭉치's part, never sent to a model.
    assert head.greeting.text == GREETING
    assert head.weather.endswith("대체로 맑음 · 최저 12° / 최고 23° · 강수확률 10% · 미세먼지 보통")
    assert "9,050.5 남음" in head.credits
    for call in run.calls:
        assert "대체로 맑음" not in call["prompt"] + call["extra_system_prompt"] and "9,050.5" not in call["prompt"]


def test_reports_run_concurrently_and_greeting_does_not_wait_for_them():
    started = []

    async def run(prompt, *, persona, **kwargs):
        started.append(persona)
        await asyncio.sleep(0.2)
        return TurnResult(text=f"{persona} 보고", session_id=UPDATE_SESSION)

    async def go():
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        async with start_relay(run=run, now=seoul(8, 7), credit_fetch=report, greeting_generate=FakeGreeting()) as relay:
            await relay.head()
            head_at = loop.time() - t0
            for persona in relay.personas:
                await relay.report(persona)
            return head_at, loop.time() - t0

    head_at, total = asyncio.run(go())
    assert sorted(started) == ["schedule", "update"]
    assert head_at < 0.15  # 고뭉치's part is ready before the reports
    assert total < 0.35  # the two 0.2 s runs overlap


@pytest.mark.parametrize(
    "result,expected",
    [
        (RuntimeError("boom sk-ant-aaaaaaaaaaaaaaaaaaaa"), "(사유: RuntimeError)"),
        (TurnResult(text="", failed=True, error="요청 한도에 걸렸습니다. 잠시 후 다시 시도하세요.\n→ 힌트"), "(사유: 요청 한도에 걸렸습니다. 잠시 후 다시 시도하세요.)"),
        (TurnResult(text="  ", session_id=UPDATE_SESSION), "(사유: 빈 응답)"),
    ],
)
def test_a_failed_report_becomes_a_short_apology_in_that_bots_voice(result, expected):
    run = PersonaRun(update=result, schedule=result)
    _head, reports = _relay(run)
    update, schedule_ = reports["update"], reports["schedule"]
    assert update.failed and schedule_.failed
    assert update.text.startswith("업뎃입니다.") and "Dropbox" in update.text and update.text.endswith(expected)
    assert schedule_.text.startswith("일정이에요.") and "😥" in schedule_.text and schedule_.text.endswith(expected)
    for text in (update.text, schedule_.text):
        assert "sk-ant-" not in text and "\n" not in text
        assert text.split(" (사유: ")[0] in [t.split(" (사유: ")[0] for pool in phrases.APOLOGY_TEMPLATES.values() for t in pool]


def test_a_report_that_times_out_says_so():
    run = PersonaRun()
    run.delays["update"] = 5
    _head, reports = _relay(run, run_timeout=0.05)
    assert reports["update"].failed and reports["update"].text.endswith("(사유: 시간 초과)")
    assert reports["schedule"].text == SCHEDULE_REPORT  # the other one is unaffected


def test_a_run_that_failed_after_writing_keeps_its_text_with_a_note():
    run = PersonaRun(update=TurnResult(text=UPDATE_REPORT, failed=True, error="응답을 마치지 못했습니다."))
    _head, reports = _relay(run)
    assert reports["update"].text == f"{UPDATE_REPORT}\n\n⚠️ 응답을 마치지 못했습니다." and reports["update"].failed


def test_the_update_run_moves_the_dropbox_checkpoint(monkeypatch):
    """업뎃's report run gets the briefing-mode Dropbox tool, which writes the checkpoint."""
    from mungchi.agents import PERSONA_TOOLS
    from mungchi.tools import dropbox_tool, tools_named

    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "sl." + "a" * 30)
    monkeypatch.setattr(dropbox_tool, "make_client", lambda cfg: object())
    monkeypatch.setattr(dropbox_tool, "collect_updates", lambda dbx, root, since, tz: {"configured": True, "total_files": 0, "groups": []})
    seen_basis = []

    async def run(prompt, *, persona, briefing=False, **kwargs):
        # What run_turn does: build this run's own tools with this run's mode, and the model calls the tool once.
        for tool in tools_named(PERSONA_TOOLS[persona], briefing=briefing):
            if tool.name == "check_dropbox_updates":
                result = await tool.handler({})
                seen_basis.append(result["content"][0]["text"])
        return TurnResult(text=f"{persona} 보고", session_id=UPDATE_SESSION)

    store = StateStore(config.get_state_path())
    assert store.last_checked("dropbox") is None
    _relay(run)
    assert len(seen_basis) == 1 and "lookback_default" in seen_basis[0]  # a briefing run, first time
    assert store.last_checked("dropbox") is not None  # the checkpoint was written
    assert store.last_brief_date() is None  # never touched here


# ---------------------------------------------------------------- 고뭉치's part: weather and credits by code


def test_mungchis_part_is_greeting_then_weather_and_credits_then_closing_lines():
    head, _reports = _relay(PersonaRun())
    text = head.text(["업뎃이, 일정이 아침 보고 부탁해요!"])
    greeting, data, closing = text.split("\n\n")
    assert greeting == GREETING
    assert data.splitlines()[0] == WEATHER_LINE and data.splitlines()[1] == CREDIT_LINE
    assert closing == "업뎃이, 일정이 아침 보고 부탁해요!"


@pytest.mark.parametrize("value", ["off", "0", "false", "OFF"])
def test_brief_weather_off_leaves_the_line_out_and_fetches_nothing(value):
    fetched = []

    async def go():
        async with start_relay(
            personas=(),
            now=seoul(8, 7),
            env={"BRIEF_WEATHER": value},
            credit_fetch=report,
            weather_fetch=lambda: fetched.append(1) or SUNNY,
            greeting_generate=FakeGreeting(),
        ) as relay:
            return await relay.head()

    head = asyncio.run(go())
    assert fetched == [] and head.weather == ""
    assert "날씨" not in head.text() and head.text().startswith(f"{GREETING}\n\n💳 ")


@pytest.mark.parametrize(
    "fetch",
    [lambda: weather.WeatherReport(label="서울", error="연결 실패: ConnectError"), lambda: (_ for _ in ()).throw(RuntimeError("down"))],
)
def test_a_weather_failure_is_a_short_note(fetch):
    async def go():
        async with start_relay(personas=(), now=seoul(8, 7), env=WEATHER_ON, credit_fetch=report, weather_fetch=fetch, greeting_generate=FakeGreeting()) as relay:
            return await relay.head()

    head = asyncio.run(go())
    assert head.weather == "🌤️ 서울 날씨: 가져오지 못했어요" and head.credits.startswith(CREDIT_LINE)


@pytest.mark.parametrize(
    "fetch,note",
    [
        (lambda: credits.CreditReport(error="크레딧 조회는 ...", supported=False), "확인 안 함 (Chat KHU 게이트웨이를 쓰지 않아요)"),
        (lambda: credits.CreditReport(error="크레딧을 확인하지 못했습니다 (HTTP 500).\n→ 힌트"), "⚠️ 확인하지 못했어요 (HTTP 500)"),
        (lambda: credits.CreditReport(error="크레딧을 확인할 키가 없습니다.\n→ 힌트"), "⚠️ 확인하지 못했어요 (크레딧을 확인할 키가 없습니다)"),
        (lambda: (_ for _ in ()).throw(RuntimeError("down")), "⚠️ 확인하지 못했어요 (RuntimeError)"),
    ],
)
def test_credit_failures_become_a_one_line_note(fetch, note):
    assert credit_section(fetch=fetch) == f"💳 Chat KHU 크레딧: {note}"
    assert credit_section(fetch=fetch, slack=True) == f"💳 *Chat KHU 크레딧*: {note}"

    async def go():
        async with start_relay(personas=(), now=seoul(8, 7), credit_fetch=fetch, greeting_generate=FakeGreeting()) as relay:
            return await relay.head()

    assert asyncio.run(go()).text().endswith(f"\n\n💳 Chat KHU 크레딧: {note}")


def test_credit_section_without_a_gateway_never_touches_the_network():
    # conftest removed every ANTHROPIC_* setting: Anthropic's own API has no credit endpoint.
    assert credit_section() == "💳 Chat KHU 크레딧: 확인 안 함 (Chat KHU 게이트웨이를 쓰지 않아요)"


def test_weather_and_credits_are_fetched_while_the_reports_run():
    import threading

    fetched = {"weather": threading.Event(), "credits": threading.Event()}
    seen_during_run = {}

    async def slow_run(prompt, *, persona, **kwargs):
        for _ in range(200):  # up to 2 s: the fetches run in worker threads meanwhile
            if all(event.is_set() for event in fetched.values()):
                break
            await asyncio.sleep(0.01)
        seen_during_run[persona] = {name: event.is_set() for name, event in fetched.items()}
        return TurnResult(text="보고", session_id=UPDATE_SESSION)

    async def go():
        async with start_relay(
            run=slow_run,
            now=seoul(8, 7),
            env=WEATHER_ON,
            credit_fetch=lambda: fetched["credits"].set() or report(),
            weather_fetch=lambda: fetched["weather"].set() or SUNNY,
            greeting_generate=FakeGreeting(),
        ) as relay:
            for persona in relay.personas:
                await relay.report(persona)

    asyncio.run(go())
    assert seen_during_run == {p: {"weather": True, "credits": True} for p in ("update", "schedule")}


# ---------------------------------------------------------------- --brief (terminal): three headed parts


def test_brief_cli_prints_three_headed_parts_in_order():
    out, err = io.StringIO(), io.StringIO()
    code = run_brief_cli(
        env=WEATHER_ON,
        run=PersonaRun(),
        now=seoul(8, 7),
        credit_fetch=report,
        weather_fetch=lambda: SUNNY,
        greeting_generate=FakeGreeting(),
        rng=random.Random(0),
        out=out,
        err=err,
    )
    assert code == 0
    text = out.getvalue()
    assert text.index("[고뭉치]\n") < text.index("[업뎃]\n") < text.index("[일정]\n")
    mungchi, update, schedule_ = (part.strip() for part in re.split(r"^\[(?:고뭉치|업뎃|일정)\]\n", text, flags=re.M)[1:])
    assert mungchi.startswith(f"{GREETING}\n\n{WEATHER_LINE}\n{CREDIT_LINE}")
    handoff = mungchi.splitlines()[-1]
    assert handoff in [t.format(bots="업뎃이, 일정이") for t in phrases.HANDOFF_TEMPLATES]
    assert update == UPDATE_REPORT and schedule_ == SCHEDULE_REPORT
    for part in (update, schedule_):  # weather and credits only in 고뭉치's part
        assert "날씨" not in part and "크레딧" not in part
    assert err.getvalue() == ""


def test_brief_cli_with_a_failed_report_still_prints_every_part_and_exits_nonzero():
    out, err = io.StringIO(), io.StringIO()
    code = run_brief_cli(run=PersonaRun(update=RuntimeError("boom")), now=seoul(8, 7), credit_fetch=report, greeting_generate=FakeGreeting(), out=out, err=err)
    assert code == 1
    text = out.getvalue()
    assert "[고뭉치]\n" in text and "[업뎃]\n업뎃입니다." in text and f"[일정]\n{SCHEDULE_REPORT}" in text
    assert "[오류] 업뎃을 실행하지 못했습니다: RuntimeError: boom" in err.getvalue()


def test_main_brief_goes_through_the_relay(monkeypatch, capsys):
    run = PersonaRun()
    monkeypatch.setattr(briefing, "run_turn", run)
    assert main(["--brief"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("[고뭉치]\n")
    assert f"[업뎃]\n{UPDATE_REPORT}\n" in out and f"[일정]\n{SCHEDULE_REPORT}\n" in out
    assert "💳 Chat KHU 크레딧: 확인 안 함 (Chat KHU 게이트웨이를 쓰지 않아요)" in out
    assert run.call("update")["briefing"] is True
    assert StateStore(config.get_state_path()).last_brief_date() is None


def test_main_brief_adds_the_open_meteo_line_by_default(monkeypatch, capsys):
    import httpx

    monkeypatch.delenv("BRIEF_WEATHER")  # conftest turns it off; the default is on
    hosts = []

    def handle(request):
        hosts.append(request.url.host)
        if request.url.host == "api.open-meteo.com":
            return httpx.Response(200, json={"daily": {"weather_code": [63], "temperature_2m_min": [14.2], "temperature_2m_max": [19.8], "precipitation_probability_max": [80]}})
        return httpx.Response(200, json={"current": {"pm10": 20, "pm2_5": 40}})

    real_client = httpx.Client
    monkeypatch.setattr(weather.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handle), timeout=kw.get("timeout")))
    run = PersonaRun()
    monkeypatch.setattr(briefing, "run_turn", run)
    assert main(["--brief"]) == 0
    out = capsys.readouterr().out
    assert "\n🌧️ 서울 날씨: 비 · 최저 14° / 최고 20° · 강수확률 80% · 미세먼지 나쁨 · ☔ 우산 챙기세요\n" in out
    assert hosts == ["api.open-meteo.com", "air-quality-api.open-meteo.com"]
    assert all("최저 14°" not in c["prompt"] and "우산" not in c["prompt"] for c in run.calls)  # never through the model
