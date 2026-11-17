"""The relay morning briefing in Slack: 고뭉치 greets and hands off, then 업뎃 and 일정 post their own parts.

Every bot has its own fake Slack client; all posts go into one shared log so
the order across bots can be checked. The 업뎃 / 일정 runs are a fake
``run_turn`` (one script per persona), the greeting a fake generator, and
the clock, weather and credits are fixed.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from mungchi import briefing, config, credits, phrases, slack_bot, weather
from mungchi.main import TurnResult, main
from mungchi.slack_bot import BriefBot, SlackHandler, post_briefing
from mungchi.slack_format import SLACK_FORMAT_PROMPT
from mungchi.state import StateStore, ThreadSessions

SEOUL = ZoneInfo("Asia/Seoul")
OWNER = "UOWNER1"
OTHER = "UOTHER1"
CHANNEL = "C0123ABCD"
IDS = {"mungchi": "UMUNGCHI", "update": "UUPDATE", "schedule": "USCHEDULE"}
UPDATE_SESSION = "22222222-2222-2222-2222-222222222222"
SCHEDULE_SESSION = "33333333-3333-3333-3333-333333333333"
LINK = "https://www.dropbox.com/home/20_%EC%97%B0%EA%B5%AC-%EC%A7%84%ED%96%89/01_ProjectA"
UPDATE_REPORT = (
    "업뎃 보고드립니다!\n"
    "Dropbox /20_연구-진행 (지난 브리핑(10/07 07:00) 이후, 파일 1개)\n"
    f"• *01_ProjectA* <{LINK}|📂 열기>\n"
    "  • 김공저: draft.tex (10/07 22:14)\n"
    "내용은 직접 확인해 주세요."
)
SCHEDULE_REPORT = (
    "좋은 아침이에요! 오늘은 여유로운 편이에요 😊\n"
    "지금 / 바로 다음 일정: 진행 중인 일정 없음 / 10:00 랩 미팅 (302호), 3시간 뒤\n"
    "*10/08 (목)*\n"
    "• 10:00–11:00 랩 미팅 (302호)"
)
GREETING = "똑똑! 🚪 10월 8일(목) 아침이에요. 오늘도 같이 챙겨 볼게요!"
WEATHER_LINE = "🌤️ *서울 날씨*: 대체로 맑음 · 최저 12° / 최고 23° · 강수확률 10% · 미세먼지 보통"
CREDIT_LINE = "💳 *Chat KHU 크레딧*: 9,050.5 남음 / 10,000 (90.5%) · 11/01 갱신"


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """October 2026 in Seoul (10/08 is a Thursday)."""
    return datetime(2026, 10, day, hour, minute, tzinfo=SEOUL)


def low_report(remaining=9050.5):
    payload = {
        "monthly_allocated": {"quota": 10000.0, "used": 10000.0 - remaining, "remaining": remaining, "renewal_date": "2026-11-01T00:00:00+09:00"},
        "total": {"quota": 10000.0, "used": 10000.0 - remaining, "remaining": remaining},
    }
    return credits.CreditReport(balance=credits.parse_balance(payload))


def sunny():
    return weather.WeatherReport(
        label="서울",
        forecast=weather.Forecast(code=1, low=11.5, high=22.6, rain_chance=10),
        air=weather.AirQuality(pm10=42.3, pm2_5=12.0),
    )


class FakeSlack:
    """One bot's Slack client. Posting to a member id lands in that bot's own DM with the person ("D" + id)."""

    def __init__(self, persona: str, log: list[dict], *, fail: dict[str, str] | None = None):
        self.persona = persona
        self.log = log  # shared by all bots: the order of every post
        self.fail = fail or {}  # channel -> Slack error code

    async def chat_postMessage(self, **kwargs):
        channel = kwargs["channel"]
        if channel in self.fail:
            exc = RuntimeError("slack error")
            exc.response = {"ok": False, "error": self.fail[channel]}
            raise exc
        ts = f"1700000100.{len(self.log) + 1:06d}"
        dm_channel = f"D{self.persona[:2].upper()}{channel[1:]}" if channel.startswith("U") else channel
        self.log.append({"bot": self.persona, **kwargs, "_ts": ts, "_channel": dm_channel})
        return {"ok": True, "channel": dm_channel, "ts": ts}

    async def chat_update(self, **kwargs):  # pragma: no cover - the relay never edits
        raise AssertionError("the relay posts, it never edits")

    async def auth_test(self):
        return {"ok": True, "user_id": IDS[self.persona]}


def make_bots(log, personas=("mungchi", "update", "schedule"), fail=None, ids=True):
    return {
        p: BriefBot(p, FakeSlack(p, log, fail=(fail or {}).get(p)), IDS[p] if ids else None) for p in personas
    }


class PersonaRun:
    """Stands in for ``run_turn``: one scripted result per persona (or an exception); records calls."""

    def __init__(self, **results):
        self.results = {
            "update": TurnResult(text=UPDATE_REPORT, session_id=UPDATE_SESSION),
            "schedule": TurnResult(text=SCHEDULE_REPORT, session_id=SCHEDULE_SESSION),
            **results,
        }
        self.waits: dict[str, asyncio.Event] = {}
        self.calls: list[dict] = []

    async def __call__(self, prompt, *, resume=None, on_status=None, extra_system_prompt="", persona="mungchi", briefing=False, conversation_key=None, images=None):
        self.calls.append({"prompt": prompt, "extra_system_prompt": extra_system_prompt, "persona": persona, "briefing": briefing, "resume": resume})
        if persona in self.waits:
            await asyncio.wait_for(self.waits[persona].wait(), 5)
        result = self.results[persona]
        if isinstance(result, BaseException):
            raise result
        return result

    def call(self, persona):
        [call] = [c for c in self.calls if c["persona"] == persona]
        return call


class FakeGreeting:
    def __init__(self, reply=GREETING):
        self.reply = reply
        self.prompts: list[str] = []

    async def __call__(self, prompt):
        self.prompts.append(prompt)
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


def relay(bots, destinations, run=None, *, tmp_path, greeting=None, **kwargs):
    """``post_briefing`` with the fixed clock, weather, credits, greeting and seeded rng."""
    options = {
        "run": run or PersonaRun(),
        "sessions": ThreadSessions(tmp_path / "threads.json"),
        "now": at(8, 7),
        "env": {},
        "credit_fetch": low_report,
        "weather_fetch": sunny,
        "greeting_generate": greeting or FakeGreeting(),
        "store": StateStore(tmp_path / "state.json"),
        "rng": random.Random(5),
        **kwargs,
    }
    return asyncio.run(post_briefing(bots, destinations, **options)), options


def texts(log, bot):
    return [p["text"] for p in log if p["bot"] == bot]


# ---------------------------------------------------------------- the channel relay


def test_channel_relay_posts_in_order_each_bot_its_own_part(tmp_path):
    log: list[dict] = []
    bots = make_bots(log)
    run = PersonaRun()
    code, options = relay(bots, CHANNEL, run, tmp_path=tmp_path)
    assert code == 0
    # 고뭉치 → 업뎃 → 일정, each with its own client, top level in the channel.
    assert [p["bot"] for p in log] == ["mungchi", "update", "schedule"]
    assert all(p["channel"] == CHANNEL and "thread_ts" not in p for p in log)
    mungchi, update, schedule = (p["text"] for p in log)
    # 고뭉치: greeting with the date, the weather and credit lines by code, then the hand-off mentioning both bots.
    greeting, data, handoff = mungchi.split("\n\n")
    assert greeting == GREETING
    assert data.splitlines()[:2] == [WEATHER_LINE, CREDIT_LINE]
    assert handoff in [t.format(bots="<@UUPDATE> <@USCHEDULE>") for t in phrases.HANDOFF_TEMPLATES["아침"]]
    # 업뎃 and 일정: their own reports, exactly; no weather and no credits.
    assert update == UPDATE_REPORT and schedule == SCHEDULE_REPORT
    for text in (update, schedule):
        assert "날씨" not in text and "💳" not in text and "크레딧" not in text
    # Each message's thread continues with that message's bot.
    sessions = options["sessions"]
    mungchi_ts, update_ts, schedule_ts = (p["_ts"] for p in log)
    assert sessions.get(CHANNEL, update_ts, persona="update") == UPDATE_SESSION
    assert sessions.get(CHANNEL, schedule_ts, persona="schedule") == SCHEDULE_SESSION
    assert sessions.get(CHANNEL, mungchi_ts, persona="mungchi") is None  # a mention there starts fresh
    assert sessions.get(CHANNEL, update_ts, persona="schedule") is None and sessions.get(CHANNEL, update_ts, persona="mungchi") is None
    # 업뎃 ran in briefing mode (its Dropbox checkpoint moves); 일정 looked at today only.
    assert run.call("update")["briefing"] is True and "since_hours 없이(0)" in run.call("update")["prompt"]
    assert run.call("schedule")["briefing"] is False and 'date="2026-10-08", days=1' in run.call("schedule")["prompt"]
    assert {c["extra_system_prompt"] for c in run.calls} == {SLACK_FORMAT_PROMPT}
    assert {c["persona"] for c in run.calls} == {"update", "schedule"}  # no 고뭉치 agent run at all
    # A manual briefing (the default) never stores its greeting: only the scheduled one does.
    # last_brief_date is never written by the relay.
    assert options["store"].recent_greetings() == []
    assert options["store"].last_brief_date() is None


def test_mungchi_posts_first_without_waiting_for_the_reports(tmp_path):
    log: list[dict] = []
    bots = make_bots(log)
    run = PersonaRun()
    mungchi_posted = asyncio.Event()

    class Watching(FakeSlack):
        async def chat_postMessage(self, **kwargs):
            response = await super().chat_postMessage(**kwargs)
            mungchi_posted.set()
            return response

    bots["mungchi"] = BriefBot("mungchi", Watching("mungchi", log), IDS["mungchi"])
    run.waits["update"] = mungchi_posted  # 업뎃 finishes only after 고뭉치's part is out
    run.waits["schedule"] = asyncio.Event()
    run.waits["schedule"].set()  # 일정 finishes first...
    code, _ = relay(bots, CHANNEL, run, tmp_path=tmp_path)
    assert code == 0
    assert [p["bot"] for p in log] == ["mungchi", "update", "schedule"]  # ...but still posts after 업뎃


def test_a_long_report_continues_in_its_own_thread(tmp_path):
    log: list[dict] = []
    long_report = "업뎃 보고드립니다!\n\n" + "\n\n".join(f"• 파일{i}.tex (10/07 22:{i:02d})" + " 내용" * 200 for i in range(6))
    code, options = relay(make_bots(log), CHANNEL, PersonaRun(update=TurnResult(text=long_report, session_id=UPDATE_SESSION)), tmp_path=tmp_path)
    assert code == 0
    update_posts = [p for p in log if p["bot"] == "update"]
    assert len(update_posts) > 1 and all(len(p["text"]) <= 3_500 for p in log)
    first, *rest = update_posts
    assert "thread_ts" not in first and all(p["thread_ts"] == first["_ts"] for p in rest)
    assert options["sessions"].get(CHANNEL, first["_ts"], persona="update") == UPDATE_SESSION
    assert [p["bot"] for p in log][-1] == "schedule"


def test_the_greeting_falls_back_to_a_template_with_the_right_date(tmp_path):
    log: list[dict] = []
    code, options = relay(make_bots(log), CHANNEL, tmp_path=tmp_path, greeting=FakeGreeting("좋은 아침! 10월 9일(금)이에요"), scheduled=True)
    assert code == 0
    greeting = texts(log, "mungchi")[0].split("\n\n")[0]
    assert greeting in [t.format(**briefing.date_fields(at(8, 7))) for t in phrases.GREETING_TEMPLATES["아침"]]
    assert "10월 8일" in greeting and "10월 9일" not in greeting
    assert options["store"].recent_greetings() == [("2026-10-08", greeting)]


def test_yesterdays_greeting_reaches_the_greeting_prompt(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.mark_greeting("2026-10-07", "좋은 아침이에요! 2026년 10월 7일(수) 아침 브리핑입니다~")
    greeting = FakeGreeting()
    relay(make_bots([]), CHANNEL, tmp_path=tmp_path, greeting=greeting, store=store, scheduled=True)
    assert '  1. "좋은 아침이에요! 2026년 10월 7일(수) 아침 브리핑입니다~"' in greeting.prompts[0]
    assert store.recent_greetings()[-1] == ("2026-10-08", GREETING)


def test_a_knock_yesterday_means_no_knock_today(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.mark_greeting("2026-10-07", "똑똑! 🚪 2026년 10월 7일(수) 아침 브리핑입니다~")
    greeting = FakeGreeting()  # "똑똑!" again: refused
    log: list[dict] = []
    relay(make_bots(log), CHANNEL, tmp_path=tmp_path, greeting=greeting, store=store, scheduled=True)
    assert "이번에는 '똑똑'을 쓰지 마" in greeting.prompts[0]
    posted = texts(log, "mungchi")[0].split("\n\n")[0]
    assert "똑똑" not in posted and "10월 8일" in posted
    assert store.recent_greetings() == [("2026-10-07", "똑똑! 🚪 2026년 10월 7일(수) 아침 브리핑입니다~"), ("2026-10-08", posted)]


def test_without_user_ids_the_hand_off_names_the_bots(tmp_path):
    log: list[dict] = []
    relay(make_bots(log, ids=False), CHANNEL, tmp_path=tmp_path)
    handoff = texts(log, "mungchi")[0].splitlines()[-1]
    assert handoff in [t.format(bots="업뎃이, 일정이") for t in phrases.HANDOFF_TEMPLATES["아침"]] and "<@" not in handoff


# ---------------------------------------------------------------- a bot that is not in the channel


def test_a_bot_not_in_the_channel_has_mungchi_post_its_part_with_the_invite_hint(tmp_path, caplog):
    log: list[dict] = []
    bots = make_bots(log, fail={"update": {CHANNEL: "not_in_channel"}})
    with caplog.at_level(logging.ERROR, logger="mungchi.slack"):
        code, options = relay(bots, CHANNEL, tmp_path=tmp_path)
    assert code == 0  # every part still reached the channel
    assert [p["bot"] for p in log] == ["mungchi", "mungchi", "schedule"]
    on_behalf = log[1]["text"]
    assert on_behalf == f"(업뎃이가 채널에 없어서 대신 전해드려요)\n{UPDATE_REPORT}\n\n{slack_bot.INVITE_HINT}"
    assert "`/invite @update @schedule`" in slack_bot.INVITE_HINT
    assert log[2]["text"] == SCHEDULE_REPORT  # 일정 is in the channel and posts itself
    assert "업뎃 봇이 브리핑을 Slack(C0123ABCD)에 올리지 못해 고뭉치가 대신 올립니다: not_in_channel" in caplog.text
    # 고뭉치's message carries 업뎃's text but not 업뎃's session (a mention there would reach 고뭉치).
    assert options["sessions"].get(CHANNEL, log[1]["_ts"], persona="update") is None


def test_the_invite_hint_is_given_only_once(tmp_path):
    log: list[dict] = []
    bots = make_bots(log, fail={"update": {CHANNEL: "not_in_channel"}, "schedule": {CHANNEL: "not_in_channel"}})
    code, _ = relay(bots, CHANNEL, tmp_path=tmp_path)
    assert code == 0 and [p["bot"] for p in log] == ["mungchi"] * 3
    assert log[1]["text"].startswith("(업뎃이가 채널에 없어서 대신 전해드려요)\n") and slack_bot.INVITE_HINT in log[1]["text"]
    assert log[2]["text"] == f"(일정이가 채널에 없어서 대신 전해드려요)\n{SCHEDULE_REPORT}"


# ---------------------------------------------------------------- DM mode


def test_dm_mode_each_bot_posts_in_its_own_dm(tmp_path):
    log: list[dict] = []
    run = PersonaRun()
    code, options = relay(make_bots(log), [OWNER, OTHER], run, tmp_path=tmp_path)
    assert code == 0
    assert len(run.calls) == 2  # one run per bot, however many people get it
    # 고뭉치 to everyone first, then 업뎃 to everyone, then 일정.
    assert [(p["bot"], p["channel"]) for p in log] == [
        ("mungchi", OWNER), ("mungchi", OTHER), ("update", OWNER), ("update", OTHER), ("schedule", OWNER), ("schedule", OTHER)
    ]
    mungchi = texts(log, "mungchi")[0]
    assert "<@" not in mungchi
    note = mungchi.splitlines()[-1]
    assert note in [t.format(names="업뎃이와 일정이는") for t in phrases.DM_HANDOFF_TEMPLATES["아침"]]
    assert texts(log, "update") == [UPDATE_REPORT] * 2 and texts(log, "schedule") == [SCHEDULE_REPORT] * 2
    # Each bot's own DM thread continues with that bot.
    for post in log:
        persona = post["bot"]
        expected = {"mungchi": None, "update": UPDATE_SESSION, "schedule": SCHEDULE_SESSION}[persona]
        assert options["sessions"].get(post["_channel"], post["_ts"], persona=persona) == expected


def test_dm_mode_without_a_bot_says_so_in_mungchis_part(tmp_path):
    log: list[dict] = []
    run = PersonaRun()
    code, _ = relay(make_bots(log, personas=("mungchi", "update")), [OWNER], run, tmp_path=tmp_path)
    assert code == 0
    assert [p["bot"] for p in log] == ["mungchi", "update"] and [c["persona"] for c in run.calls] == ["update"]
    closing = texts(log, "mungchi")[0].split("\n\n")[-1].splitlines()
    assert closing[0] == (
        "일정 봇은 아직 설정되지 않아서 오늘 보고는 빠졌어요 (필요한 값: SLACK_SCHEDULE_BOT_TOKEN, SLACK_SCHEDULE_APP_TOKEN)."
    )
    assert closing[1] in [t.format(names="업뎃이는") for t in phrases.DM_HANDOFF_TEMPLATES["아침"]]


def test_mungchi_alone_still_greets_with_weather_and_credits(tmp_path):
    log: list[dict] = []
    run = PersonaRun()
    code, _ = relay(make_bots(log, personas=("mungchi",)), CHANNEL, run, tmp_path=tmp_path)
    assert code == 0 and run.calls == []
    [text] = texts(log, "mungchi")
    assert text.startswith(f"{GREETING}\n\n{WEATHER_LINE}\n{CREDIT_LINE}")
    assert text.endswith("업뎃·일정 봇은 아직 설정되지 않아서 오늘 보고는 빠졌어요 (필요한 값: SLACK_UPDATE_BOT_TOKEN, SLACK_UPDATE_APP_TOKEN, SLACK_SCHEDULE_BOT_TOKEN, SLACK_SCHEDULE_APP_TOKEN).")


def test_a_dm_a_bot_cannot_send_is_sent_by_mungchi(tmp_path):
    log: list[dict] = []
    bots = make_bots(log, fail={"schedule": {OWNER: "channel_not_found"}})
    code, _ = relay(bots, [OWNER], tmp_path=tmp_path)
    assert code == 0
    assert [p["bot"] for p in log] == ["mungchi", "update", "mungchi"]
    assert log[2]["text"] == f"(일정이가 DM을 보내지 못해서 대신 전해드려요)\n{SCHEDULE_REPORT}" and log[2]["channel"] == OWNER


# ---------------------------------------------------------------- failures never block the others


def test_a_failed_report_is_an_apology_and_the_others_still_go_out(tmp_path, monkeypatch, caplog):
    token = "-".join(["xoxb", "123456789012", "123456789012", "abcdefghijklmnopqrstuvwx"])
    monkeypatch.setenv("SLACK_UPDATE_BOT_TOKEN", token)
    log: list[dict] = []
    run = PersonaRun(update=RuntimeError(f"agent exploded {token}"))
    with caplog.at_level(logging.INFO):
        code, options = relay(make_bots(log), CHANNEL, run, tmp_path=tmp_path)
    assert code == 1  # logged as an incomplete briefing
    assert [p["bot"] for p in log] == ["mungchi", "update", "schedule"]
    update = log[1]["text"]
    assert update.startswith("업뎃입니다.") and update.endswith("(사유: RuntimeError)") and token not in update
    assert log[2]["text"] == SCHEDULE_REPORT
    assert options["sessions"].get(CHANNEL, log[1]["_ts"], persona="update") is None  # no session to continue
    assert token not in caplog.text and "업뎃의 아침 보고를 만들지 못했습니다" in caplog.text


def test_a_timed_out_report_says_so(tmp_path):
    log: list[dict] = []
    run = PersonaRun()
    run.waits["schedule"] = asyncio.Event()  # never set: 일정 hangs
    code, _ = relay(make_bots(log), CHANNEL, run, tmp_path=tmp_path, run_timeout=0.05)
    assert code == 1
    assert texts(log, "schedule")[0].startswith("일정이에요.") and texts(log, "schedule")[0].endswith("(사유: 시간 초과)")
    assert texts(log, "update") == [UPDATE_REPORT]


def test_weather_and_credit_failures_are_short_notes_in_mungchis_part(tmp_path):
    log: list[dict] = []
    code, _ = relay(
        make_bots(log),
        CHANNEL,
        tmp_path=tmp_path,
        credit_fetch=lambda: credits.CreditReport(error="크레딧을 확인하지 못했습니다 (HTTP 500)."),
        weather_fetch=lambda: weather.WeatherReport(label="서울", error="연결 실패"),
    )
    assert code == 0
    data = texts(log, "mungchi")[0].split("\n\n")[1]
    assert data == "🌤️ *서울 날씨*: 가져오지 못했어요\n💳 *Chat KHU 크레딧*: ⚠️ 확인하지 못했어요 (HTTP 500)"


def test_brief_weather_off_has_no_weather_line(tmp_path):
    log: list[dict] = []
    fetched = []
    relay(make_bots(log), CHANNEL, tmp_path=tmp_path, env={"BRIEF_WEATHER": "off"}, weather_fetch=lambda: fetched.append(1) or sunny())
    assert fetched == [] and "날씨" not in texts(log, "mungchi")[0]


def test_one_destination_failing_does_not_stop_the_others(tmp_path, caplog):
    log: list[dict] = []
    bots = make_bots(log, fail={"mungchi": {OWNER: "channel_not_found"}})
    with caplog.at_level(logging.ERROR, logger="mungchi.slack"):
        code, _ = relay(bots, [OWNER, OTHER], tmp_path=tmp_path)
    assert code == 1
    assert [(p["bot"], p["channel"]) for p in log] == [
        ("mungchi", OTHER), ("update", OWNER), ("update", OTHER), ("schedule", OWNER), ("schedule", OTHER)
    ]
    assert f"브리핑을 Slack({OWNER})에 올리지 못했습니다: channel_not_found" in caplog.text


# ---------------------------------------------------------------- 고뭉치's mentions never start a second run


def test_mungchis_mention_of_the_other_bots_is_ignored(tmp_path):
    """Slack delivers app_mention to 업뎃 and 일정 for 고뭉치's hand-off line: dropped as a bot message."""
    for persona in ("update", "schedule"):
        run = PersonaRun()
        client = FakeSlack(persona, [])
        handler = SlackHandler(
            client,
            persona=persona,
            allowed_user_ids={OWNER},
            sessions=ThreadSessions(tmp_path / "t.json"),
            run=run,
            bot_user_id=IDS[persona],
            our_bot_user_ids=set(IDS.values()),
        )
        event = {
            "type": "app_mention",
            "user": IDS["mungchi"],
            "bot_id": "BMUNGCHI",
            "bot_profile": {"id": "BMUNGCHI", "name": "비서실 고뭉치"},
            "text": f"<@{IDS['update']}> <@{IDS['schedule']}> 아침 보고 부탁해요!",
            "ts": "1700000100.000001",
            "channel": CHANNEL,
        }
        assert handler.ignore_reason(event, "mention") == "bot"
        # Even without the bot fields, the sender is one of our own bots.
        bare = {k: v for k, v in event.items() if k not in ("bot_id", "bot_profile")}
        assert handler.ignore_reason(bare, "mention") == "our_bot"
        asyncio.run(handler.handle_event(event, event_id="Ev-handoff", source="mention"))
        asyncio.run(handler.handle_event(bare, event_id="Ev-handoff-2", source="mention"))
        assert run.calls == [] and client.log == []


# ---------------------------------------------------------------- a briefing asked of 고뭉치 in Slack


AFTERNOON_GREETING = "똑똑! 🚪 10월 8일(목) 오후 브리핑이에요. 지금까지 소식 챙겨 왔어요!"


def _request_handler(tmp_path, log, run, *, personas=("mungchi", "update", "schedule"), now=None, greeting=None):
    """A 고뭉치 handler whose briefing relay runs with a fixed clock (13:05 by default: 오후)."""
    bots = make_bots(log, personas=personas)
    store = StateStore(config.get_state_path())

    async def relay_now(bots_, targets, **kwargs):
        return await post_briefing(
            bots_,
            targets,
            **kwargs,
            now=now or at(8, 13, 5),
            env={},
            credit_fetch=low_report,
            weather_fetch=sunny,
            greeting_generate=greeting or FakeGreeting(AFTERNOON_GREETING),
            store=store,
        )

    handler = SlackHandler(
        bots["mungchi"].client,
        allowed_user_ids={OWNER, OTHER},
        sessions=ThreadSessions(tmp_path / "threads.json"),
        run=run,
        bot_user_id=IDS["mungchi"],
        briefing_relay=relay_now,
        brief_bots=bots,
        rng=random.Random(3),
    )
    return handler, store


def test_a_request_in_a_channel_runs_the_relay_into_that_channel(tmp_path):
    log: list[dict] = []
    run = PersonaRun()
    handler, store = _request_handler(tmp_path, log, run)
    store.mark_brief_date("2026-10-07")  # today's 07:00 briefing was skipped
    event = {"type": "app_mention", "user": OWNER, "text": "<@UMUNGCHI> 오늘 건너뛴 브리핑 좀 해봐", "ts": "1700000000.000100", "channel": CHANNEL}
    asyncio.run(handler.handle_event(event, event_id="Ev1", source="mention"))
    assert [p["bot"] for p in log] == ["mungchi", "update", "schedule"]
    assert all(p["channel"] == CHANNEL and "thread_ts" not in p for p in log)  # top level, like the morning one
    # Asked at 13:05: an afternoon briefing (오후 greeting and hand-off, today's schedule only).
    assert log[0]["text"].startswith(f"{AFTERNOON_GREETING}\n\n{WEATHER_LINE}\n{CREDIT_LINE}")
    assert log[0]["text"].splitlines()[-1] in [t.format(bots="<@UUPDATE> <@USCHEDULE>") for t in phrases.HANDOFF_TEMPLATES["오후"]]
    assert "아침" not in log[0]["text"]
    assert [p["text"] for p in log[1:]] == [UPDATE_REPORT, SCHEDULE_REPORT]
    assert run.call("update")["briefing"] is True
    assert run.call("update")["prompt"].startswith("오후 브리핑에서") and 'date="2026-10-08", days=1' in run.call("schedule")["prompt"]
    # last_brief_date is not written: tomorrow's scheduled briefing still goes out.
    assert store.last_brief_date() == "2026-10-07"
    assert briefing.brief_due(config.load_brief_schedule({"BRIEF_TIME": "07:00"}), at(9, 7), store.last_brief_date()) == briefing.DUE


def test_a_request_in_a_thread_runs_the_relay_in_that_thread_and_follow_ups_continue(tmp_path):
    log: list[dict] = []
    run = PersonaRun()
    handler, _store = _request_handler(tmp_path, log, run)
    root = "1700000000.000100"
    event = {"type": "app_mention", "user": OWNER, "text": "<@UMUNGCHI> 브리핑", "ts": "1700000000.000500", "thread_ts": root, "channel": CHANNEL}
    asyncio.run(handler.handle_event(event, event_id="Ev1", source="mention"))
    assert [(p["bot"], p["thread_ts"]) for p in log] == [("mungchi", root), ("update", root), ("schedule", root)]
    # A follow-up mention of 업뎃 (or 일정) in this thread continues its briefing run.
    assert handler.sessions.get(CHANNEL, root, persona="update") == UPDATE_SESSION
    assert handler.sessions.get(CHANNEL, root, persona="schedule") == SCHEDULE_SESSION
    assert handler.sessions.get(CHANNEL, root, persona="mungchi") is None


def test_a_request_in_mungchis_dm_runs_the_dm_relay_for_that_person_only(tmp_path):
    log: list[dict] = []
    run = PersonaRun()
    handler, store = _request_handler(tmp_path, log, run)
    event = {"type": "message", "channel_type": "im", "user": OWNER, "text": "브리핑", "ts": "1700000000.000200", "channel": "DMUOWNER1"}
    asyncio.run(handler.handle_event(event, event_id="Ev1", source="dm"))
    # 고뭉치 answers in this DM; 업뎃 and 일정 in their own DMs with the person who asked (not OTHER).
    assert [(p["bot"], p["channel"], p.get("thread_ts")) for p in log] == [
        ("mungchi", "DMUOWNER1", None), ("update", OWNER, None), ("schedule", OWNER, None)
    ]
    assert log[0]["text"].splitlines()[-1] in [t.format(names="업뎃이와 일정이는") for t in phrases.DM_HANDOFF_TEMPLATES["오후"]]
    assert handler.sessions.get(log[1]["_channel"], log[1]["_ts"], persona="update") == UPDATE_SESSION
    assert store.last_brief_date() is None


def test_the_default_relay_on_request_is_post_briefing(tmp_path, monkeypatch):
    log: list[dict] = []
    run = PersonaRun()
    bots = make_bots(log)
    handler = SlackHandler(
        bots["mungchi"].client, allowed_user_ids={OWNER}, sessions=ThreadSessions(tmp_path / "t.json"), run=run, bot_user_id=IDS["mungchi"], brief_bots=bots
    )
    asyncio.run(handler.handle_event({"type": "message", "channel_type": "im", "user": OWNER, "text": "", "ts": "1.000001", "channel": "DMUOWNER1"}, event_id="E", source="dm"))
    assert [p["bot"] for p in log] == ["mungchi", "update", "schedule"]
    assert "💳 *Chat KHU 크레딧*: 확인 안 함 (Chat KHU 게이트웨이를 쓰지 않아요)" in log[0]["text"]  # offline here
    # A briefing on request is manual: it never stores its greeting nor touches last_brief_date.
    assert StateStore(config.get_state_path()).recent_greetings() == []
    assert StateStore(config.get_state_path()).last_brief_date() is None


# ---------------------------------------------------------------- the wording follows the time of day


EVENING_SCHEDULE_REPORT = (
    "오늘 일정은 다 마치셨네요, 수고 많으셨어요!\n"
    "지금 / 바로 다음 일정: 진행 중인 일정 없음 / 10/09 (금) 10:00 랩 미팅 (302호), 16시간 10분 뒤\n"
    "*10/08 (목)*\n"
    "• 오늘 남은 일정 없음\n"
    "*10/09 (금)*\n"
    "• 10:00–11:00 랩 미팅 (302호)"
)


def test_a_request_at_1750_is_an_evening_briefing_with_tomorrows_schedule(tmp_path):
    """The reported case: "@비서실 고뭉치 브리핑해봐" at 17:50 got morning wording throughout."""
    log: list[dict] = []
    run = PersonaRun(schedule=TurnResult(text=EVENING_SCHEDULE_REPORT, session_id=SCHEDULE_SESSION))
    morning_like = FakeGreeting("좋은 아침이에요! 10월 8일(목), 구름 한 점 없이 맑아요")
    handler, store = _request_handler(tmp_path, log, run, now=at(8, 17, 50), greeting=morning_like)
    store.mark_greeting("2026-10-08", GREETING)  # this morning's scheduled greeting
    event = {"type": "app_mention", "user": OWNER, "text": "<@UMUNGCHI> 브리핑해봐", "ts": "1700000000.000100", "channel": CHANNEL}
    asyncio.run(handler.handle_event(event, event_id="Ev1", source="mention"))
    assert [p["bot"] for p in log] == ["mungchi", "update", "schedule"]
    # 고뭉치: the morning greeting is rejected for an evening one; the hand-off is an evening line.
    mungchi = log[0]["text"]
    greeting, data, handoff = mungchi.split("\n\n")
    assert greeting in [t.format(**briefing.date_fields(at(8, 17, 50))) for t in phrases.GREETING_TEMPLATES["저녁"]]
    assert data.splitlines()[:2] == [WEATHER_LINE, CREDIT_LINE]
    assert handoff in [t.format(bots="<@UUPDATE> <@USCHEDULE>") for t in phrases.HANDOFF_TEMPLATES["저녁"]]
    assert "아침" not in mungchi
    assert morning_like.prompts[0].startswith("저녁 브리핑을 여는 인사를 1~2문장으로 써 줘.\n- 지금: 저녁 브리핑 (17:50).")
    # 업뎃 and 일정 are told it is the evening at 17:50; 일정 looks at the rest of today and tomorrow.
    update, schedule = run.call("update")["prompt"], run.call("schedule")["prompt"]
    assert update.startswith("저녁 브리핑에서 네가 맡은 부분을 보고할 차례야(지금 17:50).")
    assert schedule.startswith("저녁 브리핑에서 네가 맡은 부분을 보고할 차례야(지금 17:50).")
    assert 'date="2026-10-08", days=2' in schedule and "*10/08 (목)*, *10/09 (금)*" in schedule
    assert {c["extra_system_prompt"] for c in run.calls} == {SLACK_FORMAT_PROMPT}  # the same system side as in the morning
    assert log[2]["text"] == EVENING_SCHEDULE_REPORT
    # This morning's greeting is still the only one stored (a manual briefing never adds to the list).
    assert store.recent_greetings() == [("2026-10-08", GREETING)]


def sunny_then_rain():
    return weather.WeatherReport(
        label="서울",
        forecast=weather.Forecast(code=1, low=11.5, high=22.6, rain_chance=10),
        tomorrow=weather.Forecast(code=63, low=14.2, high=19.6, rain_chance=70),
        air=weather.AirQuality(pm10=42.3, pm2_5=12.0),
        tomorrow_air=weather.AirQuality(pm10=40.0, pm2_5=20.0),
    )


TOMORROW_LINE = "🌧️ *내일(10/09 금)*: 비 · 최저 14° / 최고 20° · 강수확률 70% · 미세먼지 보통"


def test_an_evening_request_in_slack_adds_tomorrows_weather_line(tmp_path):
    log: list[dict] = []
    code, _ = relay(make_bots(log), CHANNEL, tmp_path=tmp_path, now=at(8, 17, 50), weather_fetch=sunny_then_rain)
    assert code == 0
    data = texts(log, "mungchi")[0].split("\n\n")[1]
    assert data.splitlines()[:3] == [WEATHER_LINE, TOMORROW_LINE, CREDIT_LINE]
    # 업뎃 and 일정 never get the weather.
    assert "내일(" not in texts(log, "update")[0] and "🌧️" not in texts(log, "schedule")[0]


def test_the_scheduled_briefing_never_has_tomorrows_weather_line(tmp_path):
    store = StateStore(tmp_path / "state.json")
    log: list[dict] = []
    status = tick(make_bots(log), store, FakeClock(at(8, 7, 0)), PersonaRun(), state=slack_bot.BriefLoopState(), weather_fetch=sunny_then_rain)
    assert status == "sent"
    data = texts(log, "mungchi")[0].split("\n\n")[1]
    assert data.splitlines()[:2] == [WEATHER_LINE, CREDIT_LINE] and "내일(" not in texts(log, "mungchi")[0]


def test_a_dm_request_at_1750_says_the_others_report_without_morning_words(tmp_path):
    log: list[dict] = []
    handler, _store = _request_handler(tmp_path, log, PersonaRun(), now=at(8, 17, 50))
    event = {"type": "message", "channel_type": "im", "user": OWNER, "text": "브리핑", "ts": "1700000000.000200", "channel": "DMUOWNER1"}
    asyncio.run(handler.handle_event(event, event_id="Ev1", source="dm"))
    note = log[0]["text"].splitlines()[-1]
    assert note in [t.format(names="업뎃이와 일정이는") for t in phrases.DM_HANDOFF_TEMPLATES["저녁"]]
    assert "아침" not in log[0]["text"]


def test_hand_off_lines_name_no_other_time_of_day():
    others = {"아침": ("오후", "저녁", "밤"), "오후": ("아침", "저녁", "밤"), "저녁": ("아침", "오후", "밤"), "밤": ("아침", "오후", "저녁")}
    assert set(phrases.HANDOFF_TEMPLATES) == set(phrases.DM_HANDOFF_TEMPLATES) == set(phrases.TIMES_OF_DAY)
    for period, words in others.items():
        for line in phrases.HANDOFF_TEMPLATES[period] + phrases.DM_HANDOFF_TEMPLATES[period]:
            assert not any(word in line for word in words), (period, line)
    assert "{bots} 오늘 소식 정리 부탁해요!" in phrases.HANDOFF_TEMPLATES["저녁"]
    assert "{bots} 저녁 보고 부탁해요!" in phrases.HANDOFF_TEMPLATES["저녁"]


@pytest.mark.parametrize("seed", range(10))
def test_the_hand_off_at_1750_never_says_morning(tmp_path, seed):
    log: list[dict] = []
    relay(make_bots(log), CHANNEL, tmp_path=tmp_path, now=at(8, 17, 50), rng=random.Random(seed))
    handoff = texts(log, "mungchi")[0].splitlines()[-1]
    assert "아침" not in handoff and handoff in [t.format(bots="<@UUPDATE> <@USCHEDULE>") for t in phrases.HANDOFF_TEMPLATES["저녁"]]


# ---------------------------------------------------------------- the scheduled 07:00 briefing


MORNING = config.load_brief_schedule({"BRIEF_TIME": "07:00"})


class FakeClock:
    def __init__(self, moment):
        self.now = moment

    def __call__(self):
        return self.now


def tick(bots, store, clock, run, *, state, destinations=(CHANNEL,), sessions=None, schedule=MORNING, greeting=None, weather_fetch=sunny, **kwargs):
    return asyncio.run(
        slack_bot.morning_brief_tick(
            bots,
            list(destinations),
            schedule=schedule,
            state=state,
            env={},
            store=store,
            clock=clock,
            run=run,
            sessions=sessions,
            credit_fetch=low_report,
            weather_fetch=weather_fetch,
            greeting_generate=greeting or FakeGreeting(),
            **kwargs,
        )
    )


def test_the_0700_tick_runs_the_relay_once_and_records_the_date_before_posting(tmp_path):
    store = StateStore(tmp_path / "state.json")
    log: list[dict] = []
    bots = make_bots(log)
    recorded_at_run = []

    class RecordingRun(PersonaRun):
        async def __call__(self, prompt, **kwargs):
            recorded_at_run.append((store.last_brief_date(), kwargs["persona"]))
            return await super().__call__(prompt, **kwargs)

    run = RecordingRun()
    clock = FakeClock(at(8, 6, 59))
    state = slack_bot.BriefLoopState()
    assert tick(bots, store, clock, run, state=state) == "early"
    assert log == [] and run.calls == []

    clock.now = at(8, 7, 0)
    assert tick(bots, store, clock, run, state=state) == "sent"
    assert sorted(recorded_at_run) == [("2026-10-08", "schedule"), ("2026-10-08", "update")]  # written before the runs
    assert [p["bot"] for p in log] == ["mungchi", "update", "schedule"]
    # The scheduled briefing is the one that stores its greeting (for tomorrow morning's).
    assert store.recent_greetings() == [("2026-10-08", GREETING)]
    # Later ticks the same day do nothing, in this process and after a restart (fresh state).
    for moment in (at(8, 7, 0), at(8, 7, 30), at(8, 11, 59)):
        clock.now = moment
        assert tick(bots, store, clock, run, state=state) == "already"
        assert tick(bots, store, clock, run, state=slack_bot.BriefLoopState()) == "already"
    assert len(log) == 3 and len(run.calls) == 2
    clock.now = at(9, 7, 0)  # the next morning it goes out again
    assert tick(bots, store, clock, run, state=state) == "sent"
    assert len(log) == 6 and store.last_brief_date() == "2026-10-09"


def test_the_real_loop_sends_the_relay_exactly_once_over_a_morning(tmp_path):
    store = StateStore(tmp_path / "state.json")
    log: list[dict] = []
    run = PersonaRun()
    clock = FakeClock(at(8, 6, 58))
    wakeups = []

    async def advance(seconds):  # every wake-up is 30 s later on the wall clock
        wakeups.append(clock.now)
        if clock.now >= at(8, 12, 30):
            raise asyncio.CancelledError
        clock.now = datetime.fromtimestamp(clock.now.timestamp() + seconds, SEOUL)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            slack_bot.morning_brief_loop(
                make_bots(log),
                [OWNER],
                schedule=MORNING,
                sleep=advance,
                store=store,
                clock=clock,
                run=run,
                credit_fetch=low_report,
                greeting_generate=FakeGreeting(),
            )
        )
    assert len(wakeups) > 600  # 06:58 → 12:30 in 30-second steps
    assert len(run.calls) == 2 and [p["bot"] for p in log] == ["mungchi", "update", "schedule"]
    assert store.last_brief_date() == "2026-10-08"


def test_a_crash_in_the_middle_is_not_resent_after_a_restart(tmp_path):
    store = StateStore(tmp_path / "state.json")
    killed = PersonaRun(update=asyncio.CancelledError())  # e.g. the process is stopped mid-run
    with pytest.raises(asyncio.CancelledError):
        tick(make_bots([]), store, FakeClock(at(8, 7, 5)), killed, state=slack_bot.BriefLoopState())
    assert store.last_brief_date() == "2026-10-08"
    log: list[dict] = []
    run = PersonaRun()
    assert tick(make_bots(log), store, FakeClock(at(8, 7, 6)), run, state=slack_bot.BriefLoopState()) == "already"
    assert run.calls == [] and log == []


def test_a_scheduled_catch_up_at_1130_is_still_the_morning_briefing(tmp_path):
    store = StateStore(tmp_path / "state.json")
    log: list[dict] = []
    run = PersonaRun()
    greeting = FakeGreeting()  # a morning greeting: fine for the scheduled briefing at any hour
    assert tick(make_bots(log), store, FakeClock(at(8, 11, 30)), run, state=slack_bot.BriefLoopState(), greeting=greeting) == "sent"
    mungchi = texts(log, "mungchi")[0]
    assert mungchi.startswith(f"{GREETING}\n\n")
    assert mungchi.splitlines()[-1] in [t.format(bots="<@UUPDATE> <@USCHEDULE>") for t in phrases.HANDOFF_TEMPLATES["아침"]]
    assert greeting.prompts[0].startswith("아침 브리핑을 여는 인사를 1~2문장으로 써 줘.\n- 지금: 아침 브리핑 (11:30).")
    for persona in ("update", "schedule"):
        assert run.call(persona)["prompt"].startswith("아침 브리핑에서 네가 맡은 부분을 보고할 차례야(지금 11:30).")
    assert 'date="2026-10-08", days=1' in run.call("schedule")["prompt"]
    assert store.recent_greetings() == [("2026-10-08", GREETING)]


def test_a_scheduled_briefing_in_the_evening_hours_would_still_be_the_morning_one(tmp_path):
    """``scheduled`` wins over the clock (e.g. BRIEF_TIME=18:00): 아침 wording and today only."""
    log: list[dict] = []
    run = PersonaRun()
    code, _ = relay(make_bots(log), CHANNEL, run, tmp_path=tmp_path, now=at(8, 18, 0), scheduled=True)
    assert code == 0 and texts(log, "mungchi")[0].startswith(f"{GREETING}\n\n")
    assert 'date="2026-10-08", days=1' in run.call("schedule")["prompt"]


def test_a_late_start_catches_up_until_noon_then_skips_with_one_log_line(tmp_path, caplog):
    log: list[dict] = []
    assert tick(make_bots(log), StateStore(tmp_path / "a.json"), FakeClock(at(8, 8, 10)), PersonaRun(), state=slack_bot.BriefLoopState()) == "sent"
    other = StateStore(tmp_path / "other.json")
    state = slack_bot.BriefLoopState()
    with caplog.at_level(logging.INFO, logger="mungchi.slack"):
        for moment in (at(8, 12, 30), at(8, 13, 0), at(8, 18, 0)):
            assert tick(make_bots(log), other, FakeClock(moment), PersonaRun(), state=state) == "missed"
    assert len([r for r in caplog.records if "건너뜁니다" in r.getMessage()]) == 1
    assert other.last_brief_date() is None and len(log) == 3


def test_weekdays_only_skips_the_weekend(tmp_path):
    log: list[dict] = []
    weekdays = config.load_brief_schedule({"BRIEF_TIME": "07:00", "BRIEF_DAYS": "weekdays"})
    store = StateStore(tmp_path / "state.json")
    assert tick(make_bots(log), store, FakeClock(at(10, 7, 0)), PersonaRun(), state=slack_bot.BriefLoopState(), schedule=weekdays) == "day_off"
    assert log == [] and store.last_brief_date() is None


def test_a_scheduled_relay_with_a_failed_report_is_logged_and_not_retried(tmp_path, caplog):
    store = StateStore(tmp_path / "state.json")
    log: list[dict] = []
    with caplog.at_level(logging.INFO, logger="mungchi.slack"):
        status = tick(make_bots(log), store, FakeClock(at(8, 7, 0)), PersonaRun(schedule=RuntimeError("calendar down")), state=slack_bot.BriefLoopState())
    assert status == "failed"
    assert [p["bot"] for p in log] == ["mungchi", "update", "schedule"]  # all three parts went out anyway
    assert texts(log, "schedule")[0].endswith("(사유: RuntimeError)")
    assert "아침 브리핑 실패 (2026-10-08)" in caplog.text
    assert store.last_brief_date() == "2026-10-08"  # not retried every 30 seconds


def test_an_unwritable_state_file_still_sends_only_once_per_process(tmp_path, caplog):
    class ReadOnlyStore(StateStore):
        def mark_brief_date(self, day):
            raise OSError("read-only file system")

        def mark_greeting(self, day, text):
            raise OSError("read-only file system")

    store = ReadOnlyStore(tmp_path / "state.json")
    log: list[dict] = []
    state = slack_bot.BriefLoopState()
    with caplog.at_level(logging.WARNING):
        assert tick(make_bots(log), store, FakeClock(at(8, 7, 0)), PersonaRun(), state=state) == "sent"
        assert tick(make_bots(log), store, FakeClock(at(8, 7, 1)), PersonaRun(), state=state) == "already"
    assert len(log) == 3
    assert "last_brief_date)를 기록하지 못했습니다" in caplog.text and "아침 인사를 상태 파일에 기록하지 못했습니다" in caplog.text


def test_brief_loop_checks_every_30_seconds_and_survives_errors():
    sleeps, ticks = [], []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 3:
            raise asyncio.CancelledError

    async def flaky_tick(bots, destinations, *, schedule, state, env=None, **options):
        ticks.append((bots, destinations, state))
        raise RuntimeError("clock exploded")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(slack_bot.morning_brief_loop("bots", [OWNER], schedule=MORNING, sleep=fake_sleep, tick=flaky_tick))
    assert sleeps == [30.0] * 4
    assert len(ticks) == 4 and all(t[:2] == ("bots", [OWNER]) for t in ticks)
    assert len({id(t[2]) for t in ticks}) == 1


# ---------------------------------------------------------------- python -m mungchi --brief --slack


SLACK_TOKENS = {
    "SLACK_BOT_TOKEN": "-".join(["xoxb", "1", "M" * 12]),
    "SLACK_UPDATE_BOT_TOKEN": "-".join(["xoxb", "2", "U" * 12]),
    "SLACK_SCHEDULE_BOT_TOKEN": "-".join(["xoxb", "3", "S" * 12]),
}


def _cli(monkeypatch, env, run=None):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    log: list[dict] = []
    by_token = {SLACK_TOKENS[k]: p for k, p in (("SLACK_BOT_TOKEN", "mungchi"), ("SLACK_UPDATE_BOT_TOKEN", "update"), ("SLACK_SCHEDULE_BOT_TOKEN", "schedule"))}
    created = []

    def fake_client(token):
        created.append(by_token[token])
        return FakeSlack(by_token[token], log)

    monkeypatch.setattr(slack_bot, "AsyncWebClient", fake_client)
    monkeypatch.setattr(slack_bot, "run_turn", run or PersonaRun())
    monkeypatch.setattr(slack_bot, "setup_logging", lambda: None)
    return log, created


def test_brief_slack_runs_the_relay_with_every_bot_token(monkeypatch, capsys):
    log, created = _cli(monkeypatch, {**SLACK_TOKENS, "SLACK_BRIEF_CHANNEL": CHANNEL})
    assert main(["--brief", "--slack"]) == 0
    assert created == ["mungchi", "update", "schedule"]
    assert [p["bot"] for p in log] == ["mungchi", "update", "schedule"]
    assert "<@UUPDATE> <@USCHEDULE>" in log[0]["text"]  # user ids from auth.test
    err = capsys.readouterr().err
    assert "Slack에 오늘 브리핑을 올렸습니다 (채널 C0123ABCD)." in err
    assert not any(token in err for token in SLACK_TOKENS.values())
    threads = ThreadSessions(config.get_slack_threads_path())
    assert threads.get(CHANNEL, log[1]["_ts"], persona="update") == UPDATE_SESSION
    assert StateStore(config.get_state_path()).last_brief_date() is None  # a test run never stops the scheduled one


def test_brief_slack_with_only_mungchi_dms_the_allowed_users(monkeypatch, capsys):
    log, created = _cli(monkeypatch, {"SLACK_BOT_TOKEN": SLACK_TOKENS["SLACK_BOT_TOKEN"], "SLACK_ALLOWED_USER_IDS": f"{OWNER},{OTHER}"})
    assert main(["--brief", "--slack"]) == 0
    assert created == ["mungchi"]
    assert sorted(p["channel"] for p in log) == sorted([OWNER, OTHER])
    assert "업뎃·일정 봇은 아직 설정되지 않아서" in log[0]["text"]
    assert "Slack에 오늘 브리핑을 올렸습니다 (DM (허용된 사용자 2명))." in capsys.readouterr().err


# ---------------------------------------------------------------- the briefing channel: a top-level "@업뎃 …" continues the briefing


class ChatSlack(FakeSlack):
    """A bot's client in a conversation: the placeholder is edited into the answer."""

    async def chat_update(self, **kwargs):
        self.log.append({"bot": self.persona, "edit": True, **kwargs})
        return {"ok": True}


OLD_SESSION = "44444444-4444-4444-4444-444444444444"
MUNGCHI_SESSION = "55555555-5555-5555-5555-555555555555"


def test_the_relay_starts_update_and_schedule_channel_conversations(tmp_path):
    log: list[dict] = []
    posted = at(8, 7, 3)
    sessions = ThreadSessions(tmp_path / "threads.json")
    sessions.set_channel_session(CHANNEL, OLD_SESSION, at(8, 6, 50), persona="mungchi")  # 고뭉치 was chatting just before
    code, _ = relay(make_bots(log), CHANNEL, tmp_path=tmp_path, sessions=sessions, clock=lambda: posted)
    assert code == 0
    assert sessions.channel_session(CHANNEL, persona="update") == (UPDATE_SESSION, posted)
    assert sessions.channel_session(CHANNEL, persona="schedule") == (SCHEDULE_SESSION, posted)
    assert sessions.channel_session(CHANNEL, persona="mungchi") is None  # 고뭉치 starts fresh
    update_ts = log[1]["_ts"]
    assert sessions.get(CHANNEL, update_ts, persona="update") == UPDATE_SESSION  # the report's thread still works

    # 20 minutes later, at the top level of the channel (SLACK_BRIEF_CHANNEL).
    later = lambda: posted + timedelta(minutes=20)  # noqa: E731
    asked = {"update": "그 파일 누가 고쳤어?", "schedule": "오후에 비는 시간 있어?", "mungchi": "고마워!"}
    for persona, expected in (("update", UPDATE_SESSION), ("schedule", SCHEDULE_SESSION), ("mungchi", None)):
        chat: list[dict] = []
        run = PersonaRun(mungchi=TurnResult(text="천만에요!", session_id=MUNGCHI_SESSION))
        handler = SlackHandler(
            ChatSlack(persona, chat),
            persona=persona,
            allowed_user_ids={OWNER},
            sessions=ThreadSessions(tmp_path / "threads.json"),
            run=run,
            bot_user_id=IDS[persona],
            brief_room=CHANNEL,
            clock=later,
        )
        event = {"type": "app_mention", "user": OWNER, "text": f"<@{IDS[persona]}> {asked[persona]}", "ts": "1700000200.000001", "channel": CHANNEL}
        asyncio.run(handler.handle_event(event, event_id=f"Ev-{persona}", source="mention"))
        assert [(c["prompt"], c["resume"]) for c in run.calls] == [(asked[persona], expected)]
        posts = [p for p in chat if not p.get("edit")]
        assert posts and all(p["channel"] == CHANNEL and "thread_ts" not in p for p in posts)

    # A mention in 업뎃's report thread still continues the briefing there, in that thread.
    chat: list[dict] = []
    run = PersonaRun()
    handler = SlackHandler(
        ChatSlack("update", chat), persona="update", allowed_user_ids={OWNER}, sessions=ThreadSessions(tmp_path / "threads.json"),
        run=run, bot_user_id=IDS["update"], brief_room=CHANNEL, clock=later,
    )
    event = {"type": "app_mention", "user": OWNER, "text": "<@UUPDATE> 그 파일 언제?", "ts": "1700000200.000002", "thread_ts": update_ts, "channel": CHANNEL}
    asyncio.run(handler.handle_event(event, event_id="Ev-thread", source="mention"))
    assert run.calls[0]["resume"] == UPDATE_SESSION
    assert all(p["thread_ts"] == update_ts for p in chat if not p.get("edit"))


def test_dm_and_thread_relays_leave_the_channel_conversations_and_a_failed_report_clears_its_bots(tmp_path):
    sessions = ThreadSessions(tmp_path / "threads.json")
    for persona in ("mungchi", "update", "schedule"):
        sessions.set_channel_session(CHANNEL, OLD_SESSION, at(8, 6, 50), persona=persona)
    before = {p: sessions.channel_session(CHANNEL, persona=p) for p in ("mungchi", "update", "schedule")}
    relay(make_bots([]), [OWNER], tmp_path=tmp_path, sessions=sessions, clock=lambda: at(8, 7, 3))
    relay(make_bots([]), slack_bot.BriefTarget(channel=CHANNEL, thread_ts="1700000000.000100"), tmp_path=tmp_path, sessions=sessions, clock=lambda: at(8, 7, 3))
    assert {p: sessions.channel_session(CHANNEL, persona=p) for p in before} == before
    # At the top level, a report that failed leaves its bot nothing old to continue.
    relay(make_bots([]), CHANNEL, PersonaRun(update=RuntimeError("boom")), tmp_path=tmp_path, sessions=sessions, clock=lambda: at(8, 7, 3))
    assert sessions.channel_session(CHANNEL, persona="update") is None
    assert sessions.channel_session(CHANNEL, persona="schedule") == (SCHEDULE_SESSION, at(8, 7, 3))
    assert sessions.channel_session(CHANNEL, persona="mungchi") is None


def test_the_scheduled_briefing_stamps_the_channel_conversations_with_its_clock(tmp_path):
    sessions = ThreadSessions(tmp_path / "threads.json")
    store = StateStore(tmp_path / "state.json")
    assert tick(make_bots([]), store, FakeClock(at(8, 7, 0)), PersonaRun(), state=slack_bot.BriefLoopState(), sessions=sessions) == "sent"
    assert sessions.channel_session(CHANNEL, persona="update") == (UPDATE_SESSION, at(8, 7, 0))
    assert sessions.channel_session(CHANNEL, persona="schedule") == (SCHEDULE_SESSION, at(8, 7, 0))
