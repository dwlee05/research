"""The shared briefing (terminal, --brief --slack, scheduled) and the "is it due?" check.

No network, no real Slack or LLM: the agent run is a fake ``run_turn`` and the
credits come from a fake fetch (or the gateway is simply not configured).
"""

from __future__ import annotations

import asyncio
import io
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from mungchi import briefing, config, credits, weather
from mungchi.briefing import (
    ALREADY,
    DAY_OFF,
    DUE,
    EARLY,
    MISSED,
    OFF,
    brief_due,
    build_briefing,
    credit_section,
    run_brief_cli,
)
from mungchi.main import TurnResult, main
from mungchi.slack_format import SLACK_FORMAT_PROMPT

SEOUL = ZoneInfo("Asia/Seoul")
SESSION = "11111111-1111-1111-1111-111111111111"
ANSWER = "*① 오늘의 일정*\n• 10:00–11:00 랩 미팅\n\n*② Dropbox 업데이트*\n• 공저자 변경 없음 (Dropbox)"


def seoul(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment in October 2026, Seoul time (10/08 is a Thursday, 10/10 a Saturday)."""
    return datetime(2026, 10, day, hour, minute, tzinfo=SEOUL)


def schedule(**env: str) -> config.BriefSchedule:
    return config.load_brief_schedule({"BRIEF_TIME": "07:00", **env})


class FakeRun:
    def __init__(self, result=None):
        self.result = result if result is not None else TurnResult(text=ANSWER, session_id=SESSION)
        self.calls: list[dict] = []

    async def __call__(self, prompt, *, resume=None, on_status=None, extra_system_prompt="", persona="mungchi", briefing=False):
        self.calls.append(
            {"prompt": prompt, "extra_system_prompt": extra_system_prompt, "persona": persona, "briefing": briefing}
        )
        if on_status is not None:
            on_status("→ 일정에게 맡기는 중...")
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def report(remaining=9050.5):
    payload = {
        "monthly_allocated": {"quota": 10000, "used": 10000 - remaining, "remaining": remaining, "renewal_date": "2026-11-01T00:00:00+09:00"},
        "total": {"quota": 10000, "used": 10000 - remaining, "remaining": remaining},
    }
    return credits.CreditReport(balance=credits.parse_balance(payload))


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


# ---------------------------------------------------------------- what goes in a briefing


def test_briefing_prompt_asks_for_todays_schedule_only_and_a_briefing_mode_run():
    run = FakeRun()
    result = asyncio.run(build_briefing(run=run, now=seoul(8, 7), credit_fetch=report))
    [call] = run.calls
    assert call["briefing"] is True  # the Dropbox tool looks at the time since the last briefing
    assert call["persona"] == "mungchi" and call["extra_system_prompt"] == ""
    prompt = call["prompt"]
    assert "오늘(2026-10-08 (목요일))" in prompt and "일정은 오늘 하루만(days=1)" in prompt
    assert "공저자 업데이트는 기간 없이" in prompt  # no since_hours: the briefing checkpoint decides
    assert result.session_id == SESSION and not result.failed


def test_credits_are_appended_by_code_after_the_answer():
    run = FakeRun()
    result = asyncio.run(build_briefing(run=run, now=seoul(8, 7), credit_fetch=report, slack=True))
    assert run.calls[0]["extra_system_prompt"] == SLACK_FORMAT_PROMPT
    assert "크레딧" not in run.calls[0]["prompt"]  # the model is never asked about credits
    assert result.text.startswith("☀️ *오늘의 브리핑 (10/08 목)*\n\n*① 오늘의 일정*")
    head, credit_part = result.text.split("\n\n💳 ", 1)
    assert head.endswith("• 공저자 변경 없음 (Dropbox)")
    assert credit_part.startswith("*Chat KHU 크레딧*: 9,050.5 남음 / 10,000 (90.5%) · 11/01 갱신")
    assert result.credits == credits.summary_text(report(), now=seoul(8, 7), slack=True)


def test_agent_failure_still_gives_header_failure_line_and_credits(monkeypatch):
    token = "sk-ant-" + "a" * 30
    run = FakeRun(RuntimeError(f"boom {token}"))
    result = asyncio.run(build_briefing(run=run, now=seoul(8, 7), credit_fetch=report, slack=True))
    assert result.failed and result.session_id is None
    header, body, credit_part = result.text.split("\n\n")
    assert header == "☀️ *오늘의 브리핑 (10/08 목)*"
    assert body == "⚠️ 오늘 브리핑을 만들지 못했어요 (RuntimeError). 실행 로그를 확인해 주세요."
    assert credit_part.startswith("💳 *Chat KHU 크레딧*: 9,050.5 남음")
    assert token not in result.text

    failed = TurnResult(text="", failed=True, error=f"요청 한도에 걸렸습니다. {token}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", token)
    result = asyncio.run(build_briefing(run=FakeRun(failed), now=seoul(8, 7), credit_fetch=report))
    assert result.body == "⚠️ 요청 한도에 걸렸습니다. ***" and result.failed
    assert result.text.startswith("☀️ 오늘의 브리핑 (10/08 목)\n\n⚠️ 요청 한도에 걸렸습니다. ***\n\n💳 Chat KHU 크레딧: 9,050.5 남음")


def test_a_slow_agent_run_times_out_into_a_failure_line():
    async def stuck(prompt, **kwargs):
        await asyncio.sleep(10)

    result = asyncio.run(build_briefing(run=stuck, now=seoul(8, 7), credit_fetch=report, run_timeout=0.01))
    assert result.failed and "(시간 초과)" in result.body and "💳" in result.text


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
    result = asyncio.run(build_briefing(run=FakeRun(), now=seoul(8, 7), credit_fetch=fetch))
    assert result.text.endswith(f"\n\n💳 Chat KHU 크레딧: {note}") and not result.failed


def test_credit_section_without_a_gateway_never_touches_the_network():
    # conftest removed every ANTHROPIC_* setting: Anthropic's own API has no credit endpoint.
    assert credit_section() == "💳 Chat KHU 크레딧: 확인 안 함 (Chat KHU 게이트웨이를 쓰지 않아요)"


# ---------------------------------------------------------------- --brief (terminal) has the same structure


def test_brief_cli_prints_the_same_structure_as_slack():
    out, err = io.StringIO(), io.StringIO()
    code = run_brief_cli(run=FakeRun(), now=seoul(8, 7), credit_fetch=report, out=out, err=err)
    assert code == 0
    terminal = out.getvalue().rstrip("\n")
    slack = asyncio.run(build_briefing(run=FakeRun(), now=seoul(8, 7), credit_fetch=report, slack=True)).text
    # Same parts in the same order; only the Slack bold differs.
    assert terminal.split("\n\n")[0] == "☀️ 오늘의 브리핑 (10/08 목)"
    assert slack.split("\n\n")[0] == "☀️ *오늘의 브리핑 (10/08 목)*"
    assert terminal.split("\n\n")[1:-1] == slack.split("\n\n")[1:-1] == ANSWER.split("\n\n")
    assert terminal.split("\n\n")[-1].startswith("💳 Chat KHU 크레딧: 9,050.5 남음")
    assert slack.split("\n\n")[-1].startswith("💳 *Chat KHU 크레딧*: 9,050.5 남음")
    assert err.getvalue() == "→ 일정에게 맡기는 중...\n"  # progress on stderr, the briefing alone on stdout


def test_brief_cli_failure_exits_nonzero_but_still_prints_header_and_credits():
    out, err = io.StringIO(), io.StringIO()
    code = run_brief_cli(run=FakeRun(RuntimeError("boom")), now=seoul(8, 7), credit_fetch=report, out=out, err=err)
    assert code == 1
    text = out.getvalue()
    assert text.startswith("☀️ 오늘의 브리핑 (10/08 목)\n\n⚠️ 오늘 브리핑을 만들지 못했어요 (RuntimeError)")
    assert "\n\n💳 Chat KHU 크레딧: 9,050.5 남음" in text
    assert "[오류] 고뭉치를 실행하지 못했습니다: RuntimeError: boom" in err.getvalue()


def test_main_brief_goes_through_the_shared_briefing(monkeypatch, capsys):
    run = FakeRun()
    monkeypatch.setattr(briefing, "run_turn", run)
    assert main(["--brief"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("☀️ 오늘의 브리핑 (")
    assert ANSWER in out
    assert out.rstrip().endswith("💳 Chat KHU 크레딧: 확인 안 함 (Chat KHU 게이트웨이를 쓰지 않아요)")
    assert run.calls[0]["briefing"] is True


# ---------------------------------------------------------------- the weather line (by code, never through the model)

SUNNY = weather.WeatherReport(
    label="서울",
    forecast=weather.Forecast(code=1, low=11.5, high=22.6, rain_chance=10),
    air=weather.AirQuality(pm10=42.3, pm2_5=12.0),
)
WEATHER_LINE = "🌤️ 서울 날씨: 대체로 맑음 · 최저 12° / 최고 23° · 강수확률 10% · 미세먼지 보통"
WEATHER_ON: dict[str, str] = {}  # an explicit env without BRIEF_WEATHER: the default, on


class FakeWeather:
    def __init__(self, result=SUNNY):
        self.result = result
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.mark.parametrize("slack", [False, True])
def test_weather_line_sits_right_under_the_header(slack):
    run, fetch = FakeRun(), FakeWeather()
    result = asyncio.run(
        build_briefing(run=run, now=seoul(8, 7), env=WEATHER_ON, credit_fetch=report, weather_fetch=fetch, slack=slack)
    )
    header = "☀️ *오늘의 브리핑 (10/08 목)*" if slack else "☀️ 오늘의 브리핑 (10/08 목)"
    line = WEATHER_LINE.replace("서울 날씨", "*서울 날씨*") if slack else WEATHER_LINE
    assert result.weather == line and fetch.calls == 1
    assert result.text.startswith(f"{header}\n{line}\n\n*① 오늘의 일정*")
    head, *middle, credit_part = result.text.split("\n\n")
    assert head.splitlines() == [header, line] and middle == ANSWER.split("\n\n")
    assert credit_part.startswith("💳 ")
    # The model is never asked about (or told) the weather.
    prompt = run.calls[0]["prompt"] + run.calls[0]["extra_system_prompt"]
    assert "날씨" not in prompt and "미세먼지" not in prompt


def test_agent_failure_still_gives_header_weather_failure_line_and_credits():
    result = asyncio.run(
        build_briefing(
            run=FakeRun(RuntimeError("boom")), now=seoul(8, 7), env=WEATHER_ON, credit_fetch=report, weather_fetch=FakeWeather(), slack=True
        )
    )
    assert result.failed
    head, body, credit_part = result.text.split("\n\n")
    assert head == "☀️ *오늘의 브리핑 (10/08 목)*\n🌤️ *서울 날씨*: 대체로 맑음 · 최저 12° / 최고 23° · 강수확률 10% · 미세먼지 보통"
    assert body == "⚠️ 오늘 브리핑을 만들지 못했어요 (RuntimeError). 실행 로그를 확인해 주세요."
    assert credit_part.startswith("💳 *Chat KHU 크레딧*: 9,050.5 남음")


@pytest.mark.parametrize(
    "fetch",
    [
        FakeWeather(weather.WeatherReport(label="서울", error="연결 실패: ConnectError")),
        FakeWeather(RuntimeError("weather exploded")),
    ],
)
def test_a_weather_failure_is_a_short_note_and_the_briefing_goes_out(fetch):
    result = asyncio.run(build_briefing(run=FakeRun(), now=seoul(8, 7), env=WEATHER_ON, credit_fetch=report, weather_fetch=fetch))
    assert not result.failed
    assert result.text.startswith("☀️ 오늘의 브리핑 (10/08 목)\n🌤️ 서울 날씨: 가져오지 못했어요\n\n*① 오늘의 일정*")
    assert "💳 Chat KHU 크레딧: 9,050.5 남음" in result.text


@pytest.mark.parametrize("value", ["off", "0", "false", "OFF"])
def test_brief_weather_off_leaves_the_line_out_and_fetches_nothing(value):
    fetch = FakeWeather()
    result = asyncio.run(
        build_briefing(run=FakeRun(), now=seoul(8, 7), env={"BRIEF_WEATHER": value}, credit_fetch=report, weather_fetch=fetch)
    )
    assert fetch.calls == 0 and result.weather == ""
    assert result.text.startswith("☀️ 오늘의 브리핑 (10/08 목)\n\n*① 오늘의 일정*")
    assert "날씨" not in result.text


def test_weather_and_credits_are_fetched_while_the_agent_runs():
    import threading

    fetched = {"weather": threading.Event(), "credits": threading.Event()}

    def weather_fetch():
        fetched["weather"].set()
        return SUNNY

    def credit_fetch():
        fetched["credits"].set()
        return report()

    seen_during_run = {}

    async def slow_run(prompt, **kwargs):
        for _ in range(200):  # up to 2 s: the fetches run in worker threads meanwhile
            if all(event.is_set() for event in fetched.values()):
                break
            await asyncio.sleep(0.01)
        seen_during_run.update({name: event.is_set() for name, event in fetched.items()})
        return TurnResult(text=ANSWER, session_id=SESSION)

    result = asyncio.run(
        build_briefing(run=slow_run, now=seoul(8, 7), env=WEATHER_ON, credit_fetch=credit_fetch, weather_fetch=weather_fetch)
    )
    assert seen_during_run == {"weather": True, "credits": True}
    assert result.text.startswith(f"☀️ 오늘의 브리핑 (10/08 목)\n{WEATHER_LINE}\n\n")


def test_brief_cli_prints_the_weather_under_the_header():
    out, err = io.StringIO(), io.StringIO()
    code = run_brief_cli(env=WEATHER_ON, run=FakeRun(), now=seoul(8, 7), credit_fetch=report, weather_fetch=FakeWeather(), out=out, err=err)
    assert code == 0
    assert out.getvalue().startswith(f"☀️ 오늘의 브리핑 (10/08 목)\n{WEATHER_LINE}\n\n*① 오늘의 일정*")

    out = io.StringIO()
    code = run_brief_cli(env=WEATHER_ON, run=FakeRun(RuntimeError("boom")), now=seoul(8, 7), credit_fetch=report, weather_fetch=FakeWeather(), out=out, err=io.StringIO())
    assert code == 1
    assert out.getvalue().startswith(f"☀️ 오늘의 브리핑 (10/08 목)\n{WEATHER_LINE}\n\n⚠️ 오늘 브리핑을 만들지 못했어요 (RuntimeError)")


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
    run = FakeRun()
    monkeypatch.setattr(briefing, "run_turn", run)
    assert main(["--brief"]) == 0
    first, second, *_ = capsys.readouterr().out.splitlines()
    assert first.startswith("☀️ 오늘의 브리핑 (")
    assert second == "🌧️ 서울 날씨: 비 · 최저 14° / 최고 20° · 강수확률 80% · 미세먼지 나쁨 · ☔ 우산 챙기세요"
    assert hosts == ["api.open-meteo.com", "air-quality-api.open-meteo.com"]
    assert "날씨" not in run.calls[0]["prompt"]
