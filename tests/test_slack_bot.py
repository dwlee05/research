from __future__ import annotations

import asyncio
import inspect
import logging
import re
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from mungchi import config, slack_bot
from mungchi.main import TurnResult, main
from mungchi.slack_bot import (
    CRASH_TEXT,
    REFUSAL_TEXT,
    RecentKeys,
    SlackHandler,
    StatusUpdater,
    build_app,
    compose_reply,
    post_briefing,
)
from mungchi.personas import SLACK_APP_NAMES, SLACK_HANDLES
from mungchi.slack_format import PLACEHOLDER_TEXT, PLACEHOLDERS, SLACK_FORMAT_PROMPT
from mungchi.state import StateStore, ThreadSessions

OWNER = "UOWNER1"
STRANGER = "USTRANGER"
BOT = "UBOT"
CHANNEL = "C0123ABCD"
DM = "D0123ABCD"
SESSION_1 = "11111111-1111-1111-1111-111111111111"
SESSION_2 = "22222222-2222-2222-2222-222222222222"
# A fake token, assembled at runtime so no token-shaped literal is committed.
SLACK_TOKEN = "-".join(["xoxb", "123456789012", "123456789012", "abcdefghijklmnopqrstuvwx"])


class FakeSlackClient:
    """Records chat_postMessage / chat_update calls like AsyncWebClient would receive them."""

    def __init__(self, *, fail_updates: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.fail_updates = fail_updates
        self._counter = 0

    async def chat_postMessage(self, **kwargs):
        self._counter += 1
        ts = f"1700000100.{self._counter:06d}"
        self.calls.append(("post", {**kwargs, "_ts": ts}))
        return {"ok": True, "channel": kwargs["channel"], "ts": ts}

    async def chat_update(self, **kwargs):
        self.calls.append(("update", dict(kwargs)))
        if self.fail_updates:
            raise RuntimeError("update failed")
        return {"ok": True}

    @property
    def posts(self) -> list[dict]:
        return [kw for kind, kw in self.calls if kind == "post"]

    @property
    def updates(self) -> list[dict]:
        return [kw for kind, kw in self.calls if kind == "update"]


class FakeRun:
    """Stands in for ``run_turn``: records calls, emits statuses, returns scripted results."""

    def __init__(self, *results, statuses=("→ 업뎃에게 맡기는 중...", "→ 일정에게 맡기는 중..."), delay=0.0):
        self.results = list(results) or [TurnResult(text="답변입니다.", session_id=SESSION_1)]
        self.statuses = statuses
        self.delay = delay
        self.calls: list[dict] = []
        self.active = 0
        self.max_active = 0

    async def __call__(
        self, prompt, *, resume=None, on_status=None, extra_system_prompt="", persona="mungchi", briefing=False
    ):
        self.calls.append(
            {
                "prompt": prompt,
                "resume": resume,
                "extra_system_prompt": extra_system_prompt,
                "persona": persona,
                "briefing": briefing,
            }
        )
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            for line in self.statuses:
                if on_status is not None:
                    outcome = on_status(line)  # like run_turn: sync or async callbacks
                    if inspect.isawaitable(outcome):
                        await outcome
            if self.delay:
                await asyncio.sleep(self.delay)
            result = self.results[min(len(self.calls), len(self.results)) - 1]
            if isinstance(result, BaseException):
                raise result
            return result
        finally:
            self.active -= 1


def make_handler(tmp_path: Path, run=None, client=None, **kwargs) -> tuple[SlackHandler, FakeSlackClient, FakeRun]:
    client = client or FakeSlackClient()
    run = run or FakeRun()
    handler = SlackHandler(
        client,
        allowed_user_ids={OWNER},
        sessions=ThreadSessions(tmp_path / "threads.json"),
        run=run,
        bot_user_id=BOT,
        status_interval=0,
        **kwargs,
    )
    return handler, client, run


def mention(text=f"<@{BOT}> 오늘 일정 알려줘", *, user=OWNER, ts="1700000000.000100", thread_ts=None, **extra):
    event = {"type": "app_mention", "user": user, "text": text, "ts": ts, "channel": CHANNEL, **extra}
    if thread_ts:
        event["thread_ts"] = thread_ts
    return event


def dm(text="오늘 일정 알려줘", *, user=OWNER, ts="1700000000.000200", thread_ts=None, **extra):
    event = {"type": "message", "channel_type": "im", "user": user, "text": text, "ts": ts, "channel": DM, **extra}
    if thread_ts:
        event["thread_ts"] = thread_ts
    return event


# ---------------------------------------------------------------- end to end


def test_mention_end_to_end_placeholder_status_and_chunked_answer(tmp_path):
    long_answer = "\n\n".join(f"**항목 {i}**: " + "내용 " * 600 for i in range(3))
    assert len(long_answer) > 3_500
    run = FakeRun(TurnResult(text=long_answer, session_id=SESSION_1))
    handler, client, run = make_handler(tmp_path, run=run)

    asyncio.run(handler.handle_event(mention(), event_id="Ev1", source="mention"))

    # 1) placeholder posted in the thread of the mention
    first = client.calls[0]
    assert first[0] == "post"
    assert first[1]["text"] == PLACEHOLDER_TEXT
    assert first[1]["thread_ts"] == "1700000000.000100"
    placeholder_ts = first[1]["_ts"]
    # 2) the agent ran once with the stripped prompt and Slack formatting
    assert run.calls == [
        {
            "prompt": "오늘 일정 알려줘",
            "resume": None,
            "extra_system_prompt": SLACK_FORMAT_PROMPT,
            "persona": "mungchi",
            "briefing": False,  # Slack turns never move the Dropbox briefing checkpoint
        }
    ]
    # 3) status updates edited the placeholder
    status_updates = [u for u in client.updates if u["text"].startswith(PLACEHOLDER_TEXT)]
    assert status_updates[0]["ts"] == placeholder_ts
    assert "→ 업뎃에게 맡기는 중..." in status_updates[0]["text"]
    assert "→ 일정에게 맡기는 중..." in status_updates[-1]["text"]
    # 4) the placeholder became the first chunk, the rest are thread replies
    final_update = client.updates[-1]
    assert final_update["ts"] == placeholder_ts
    assert final_update["text"].startswith("*항목 0*")
    rest = client.posts[1:]
    assert rest and all(p["thread_ts"] == "1700000000.000100" for p in rest)
    assert all(len(p["text"]) <= 3_500 for p in rest)
    assert "*항목 2*" in rest[-1]["text"]
    assert all(p.get("unfurl_links") is False for p in client.posts)
    # 5) the session is remembered for the thread
    assert handler.sessions.get(CHANNEL, "1700000000.000100", persona="mungchi") == SESSION_1


def test_follow_up_in_thread_resumes_session(tmp_path):
    run = FakeRun(
        TurnResult(text="첫 답", session_id=SESSION_1),
        TurnResult(text="이어진 답", session_id=SESSION_1),
        TurnResult(text="새 스레드", session_id=SESSION_2),
    )
    handler, client, run = make_handler(tmp_path, run=run)
    root = "1700000000.000100"

    async def scenario():
        await handler.handle_event(mention(ts=root), event_id="Ev1", source="mention")
        await handler.handle_event(
            mention(f"<@{BOT}> 그럼 내일은?", ts="1700000000.000300", thread_ts=root), event_id="Ev2", source="mention"
        )
        await handler.handle_event(mention(ts="1700000000.000900"), event_id="Ev3", source="mention")

    asyncio.run(scenario())
    assert [c["resume"] for c in run.calls] == [None, SESSION_1, None]
    assert run.calls[1]["prompt"] == "그럼 내일은?"
    # Replies of the follow-up go to the same thread.
    assert {p["thread_ts"] for p in client.posts[:2]} == {root}
    # A handler restarted on the same file still knows the thread.
    restarted, _, run2 = make_handler(tmp_path, run=FakeRun(TurnResult(text="ok", session_id=SESSION_1)))
    asyncio.run(
        restarted.handle_event(dm(ts="1700000001.000001"), event_id="Ev4", source="dm")
    )
    assert run2.calls[0]["resume"] is None
    asyncio.run(
        restarted.handle_event(
            mention("<@UBOT> 계속", ts="1700000002.000001", thread_ts=root), event_id="Ev5", source="mention"
        )
    )
    assert run2.calls[1]["resume"] == SESSION_1


def test_dm_is_answered_in_thread(tmp_path):
    handler, client, run = make_handler(tmp_path)
    asyncio.run(handler.handle_event(dm("내일 비는 시간?"), event_id="Ev1", source="dm"))
    assert run.calls[0]["prompt"] == "내일 비는 시간?"
    assert client.posts[0]["channel"] == DM
    assert client.posts[0]["thread_ts"] == "1700000000.000200"


def test_empty_mention_means_briefing(tmp_path):
    handler, _, run = make_handler(tmp_path)
    asyncio.run(handler.handle_event(mention(f"<@{BOT}>"), event_id="Ev1", source="mention"))
    prompt = run.calls[0]["prompt"]
    assert prompt.startswith("업뎃과 '일정' 에이전트에게 일을 맡겨서 오늘(")
    assert "브리핑" in prompt
    # Asked for in Slack, it is still an ad-hoc run: only --brief moves the checkpoint.
    assert run.calls[0]["briefing"] is False


# ---------------------------------------------------------------- authorization


def test_unauthorized_user_is_refused_once_and_agent_never_runs(tmp_path, caplog):
    handler, client, run = make_handler(tmp_path)

    async def scenario():
        await handler.handle_event(mention(user=STRANGER), event_id="Ev1", source="mention")
        await handler.handle_event(
            mention(user=STRANGER, ts="1700000000.000500", thread_ts="1700000000.000100"),
            event_id="Ev2",
            source="mention",
        )
        await handler.handle_event(dm(user=STRANGER), event_id="Ev3", source="dm")

    with caplog.at_level(logging.WARNING, logger="mungchi.slack"):
        asyncio.run(scenario())
    assert run.calls == []
    texts = [p["text"] for p in client.posts]
    # One refusal per thread: the mention thread and the DM thread.
    assert texts == [REFUSAL_TEXT, REFUSAL_TEXT]
    assert "소유자만 사용할 수 있어요" in REFUSAL_TEXT
    assert client.posts[0]["thread_ts"] == "1700000000.000100"
    assert client.updates == []
    assert STRANGER in caplog.text


def test_handler_refuses_to_exist_without_allow_list(tmp_path):
    with pytest.raises(ValueError):
        SlackHandler(FakeSlackClient(), allowed_user_ids=[], sessions=ThreadSessions(tmp_path / "t.json"))


# ---------------------------------------------------------------- filters and dedupe


@pytest.mark.parametrize(
    "event,source,reason",
    [
        (mention(bot_id="B123"), "mention", "bot"),
        (mention(user=BOT), "mention", "self"),
        (dm(subtype="message_changed"), "dm", "subtype"),
        (dm(subtype="channel_join"), "dm", "subtype"),
        ({**dm(), "channel_type": "channel"}, "dm", "not_dm"),
        ({**dm(), "user": None}, "dm", "no_user"),
        (dm(bot_profile={"id": "B1"}), "dm", "bot"),
    ],
)
def test_ignored_events_never_reach_slack_or_agent(tmp_path, event, source, reason):
    handler, client, run = make_handler(tmp_path)
    assert handler.ignore_reason(event, source) == reason
    asyncio.run(handler.handle_event(event, event_id="Ev1", source=source))
    assert client.calls == [] and run.calls == []


def test_duplicate_deliveries_are_handled_once(tmp_path):
    handler, client, run = make_handler(tmp_path)

    async def scenario():
        event = mention(client_msg_id="m-1")
        await handler.handle_event(event, event_id="Ev1", source="mention")
        await handler.handle_event(event, event_id="Ev1", source="mention")  # Slack retry
        # Same message delivered again as message.im with another event id
        await handler.handle_event({**dm(client_msg_id="m-1")}, event_id="Ev2", source="dm")
        # Same channel+ts without any ids
        await handler.handle_event(mention(), event_id=None, source="mention")

    asyncio.run(scenario())
    assert len(run.calls) == 1


def test_recent_keys_is_bounded():
    keys = RecentKeys(maxlen=3)
    assert not keys.seen("a")
    assert keys.seen("a", None)
    for key in "bcd":
        keys.seen(key)
    assert len(keys) == 3
    assert not keys.seen("a")  # forgotten


# ---------------------------------------------------------------- concurrency


def test_same_thread_runs_sequentially_and_global_cap_applies(tmp_path):
    run = FakeRun(TurnResult(text="ok", session_id=SESSION_1), statuses=(), delay=0.02)
    handler, client, run = make_handler(tmp_path, run=run, max_concurrent=2)
    root = "1700000000.000100"

    async def scenario():
        await asyncio.gather(
            handler.handle_event(mention(ts=root), event_id="E1", source="mention"),
            handler.handle_event(mention(ts="1700000000.000101", thread_ts=root), event_id="E2", source="mention"),
            handler.handle_event(mention(ts="1700000000.000102", thread_ts=root), event_id="E3", source="mention"),
        )

    asyncio.run(scenario())
    assert run.max_active == 1  # one thread -> strictly sequential
    # The 2nd and 3rd messages saw the session stored by the 1st.
    assert [c["resume"] for c in run.calls] == [None, SESSION_1, SESSION_1]
    assert handler._locks == {}  # per-thread locks are cleaned up

    run_many = FakeRun(TurnResult(text="ok", session_id=SESSION_2), statuses=(), delay=0.02)
    handler2, _, run_many = make_handler(tmp_path / "b", run=run_many, max_concurrent=2)

    async def many_threads():
        await asyncio.gather(
            *(
                handler2.handle_event(mention(ts=f"1700000000.0002{i:02d}"), event_id=f"F{i}", source="mention")
                for i in range(6)
            )
        )

    asyncio.run(many_threads())
    assert len(run_many.calls) == 6
    assert run_many.max_active == 2  # SLACK_MAX_CONCURRENT


# ---------------------------------------------------------------- errors


def test_agent_crash_shows_short_korean_error_without_secrets(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_TOKEN)
    run = FakeRun(RuntimeError(f"boom with {SLACK_TOKEN} and sk-ant-abcdefghijklmnop"))
    handler, client, _ = make_handler(tmp_path, run=run)
    with caplog.at_level(logging.ERROR, logger="mungchi.slack"):
        asyncio.run(handler.handle_event(mention(), event_id="Ev1", source="mention"))
    final = client.updates[-1]["text"]
    assert final == CRASH_TEXT.format(kind="RuntimeError")
    assert "boom" not in final and "Traceback" not in final
    # Details go to the log, scrubbed.
    assert "boom" in caplog.text and "Traceback" in caplog.text
    assert SLACK_TOKEN not in caplog.text and "sk-ant-abcdefghijklmnop" not in caplog.text


def test_failed_turn_reports_reason_and_broken_resume_is_forgotten(tmp_path):
    root = "1700000000.000100"
    run = FakeRun(TurnResult(text="", session_id=None, failed=True, error="요청 한도에 걸렸습니다. 잠시 후 다시 시도하세요."))
    handler, client, _ = make_handler(tmp_path, run=run)
    handler.sessions.set(CHANNEL, root, SESSION_1, persona="mungchi")
    asyncio.run(handler.handle_event(mention(ts="1700000000.000300", thread_ts=root), event_id="E", source="mention"))
    final = client.updates[-1]["text"]
    assert final.startswith("⚠️ 요청 한도에 걸렸습니다")
    assert "새 대화로 시작" in final
    assert handler.sessions.get(CHANNEL, root, persona="mungchi") is None


def test_failed_placeholder_update_falls_back_to_posting(tmp_path):
    client = FakeSlackClient(fail_updates=True)
    handler, client, _ = make_handler(tmp_path, client=client)
    asyncio.run(handler.handle_event(mention(), event_id="Ev1", source="mention"))
    assert [p["text"] for p in client.posts] == [PLACEHOLDER_TEXT, "답변입니다."]


def test_compose_reply_variants():
    assert compose_reply(TurnResult(text="  답  ")) == "답"
    assert compose_reply(TurnResult(text="")) == slack_bot.EMPTY_ANSWER_TEXT
    assert compose_reply(TurnResult(text="일부", failed=True, error="오류")) == "일부\n\n⚠️ 오류"
    assert compose_reply(TurnResult(text="", failed=True)) == slack_bot.FAILED_TEXT


# ---------------------------------------------------------------- status throttling


def test_status_updates_are_throttled_and_close_stops_them():
    client = FakeSlackClient()
    clock = SimpleNamespace(now=100.0)

    async def scenario():
        updater = StatusUpdater(client, CHANNEL, "1.0", interval=10.0, clock=lambda: clock.now)
        await updater("→ 업뎃에게 맡기는 중...")  # immediate
        await updater("→ 일정에게 맡기는 중...")  # within interval -> deferred
        await updater("→ 일정에게 맡기는 중...")  # duplicate -> ignored
        await updater.close()  # pending deferred update is dropped
        await updater("늦은 상태")  # after close -> ignored
        return updater

    updater = asyncio.run(scenario())
    assert len(client.updates) == 1
    assert client.updates[0]["text"] == f"{PLACEHOLDER_TEXT}\n→ 업뎃에게 맡기는 중..."
    assert updater.lines == ["→ 업뎃에게 맡기는 중...", "→ 일정에게 맡기는 중..."]


def test_deferred_status_update_is_sent_after_interval():
    client = FakeSlackClient()

    async def scenario():
        updater = StatusUpdater(client, CHANNEL, "1.0", interval=0.05)
        await updater("→ 업뎃에게 맡기는 중...")
        await updater("→ 일정에게 맡기는 중...")
        await asyncio.sleep(0.15)
        await updater.close()

    asyncio.run(scenario())
    assert len(client.updates) == 2
    assert client.updates[-1]["text"].endswith("→ 업뎃에게 맡기는 중...\n→ 일정에게 맡기는 중...")


def test_status_update_failures_are_tolerated():
    client = FakeSlackClient(fail_updates=True)

    async def scenario():
        updater = StatusUpdater(client, CHANNEL, "1.0", interval=0)
        await updater("→ 업뎃에게 맡기는 중...")
        await updater.close()

    asyncio.run(scenario())  # no exception
    assert len(client.updates) == 1


# ---------------------------------------------------------------- configuration


def test_slack_config_parsing_and_validation():
    env = {
        "SLACK_BOT_TOKEN": "xoxb-1-2-abc",
        "SLACK_APP_TOKEN": "xapp-1-A1-abc",
        "SLACK_ALLOWED_USER_IDS": f" {OWNER} , W0ENTERPRISE ",
        "SLACK_MAX_CONCURRENT": "abc",
    }
    cfg = config.load_slack_config(env)
    assert cfg.allowed_user_ids == {OWNER, "W0ENTERPRISE"}
    assert cfg.max_concurrent == 2
    assert config.slack_bot_problems(cfg) == []
    assert config.load_slack_config({**env, "SLACK_MAX_CONCURRENT": "4"}).max_concurrent == 4

    swapped = config.load_slack_config({**env, "SLACK_BOT_TOKEN": "xapp-x", "SLACK_APP_TOKEN": "xoxb-y"})
    problems = "\n".join(config.slack_bot_problems(swapped))
    assert "SLACK_BOT_TOKEN 값은 xoxb-로 시작" in problems and "SLACK_APP_TOKEN 값은 xapp-로 시작" in problems
    assert "xapp-x" not in problems and "xoxb-y" not in problems  # never echo token values

    bad_ids = config.load_slack_config({**env, "SLACK_ALLOWED_USER_IDS": f"{OWNER},@kim"})
    assert any("@kim" in p for p in config.slack_bot_problems(bad_ids))

    assert config.slack_brief_problems(config.load_slack_config({**env, "SLACK_BRIEF_CHANNEL": "C0123ABCD"})) == []
    named = config.load_slack_config({**env, "SLACK_BRIEF_CHANNEL": "#general"})
    assert any("채널 ID" in p for p in config.slack_brief_problems(named))


def test_slack_command_without_env_prints_korean_error_and_exits_nonzero(monkeypatch, capsys):
    async def must_not_run(cfg):  # pragma: no cover - the test fails if called
        raise AssertionError("bot must not start")

    monkeypatch.setattr(slack_bot, "run_bots", must_not_run)
    assert main(["slack"]) == 1
    err = capsys.readouterr().err
    assert "[오류] Slack 봇을 시작할 수 없습니다." in err
    assert "빠진 환경변수: SLACK_ALLOWED_USER_IDS" in err
    assert "Slack 봇 토큰이 하나도 없습니다" in err
    for name in (
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "SLACK_UPDATE_BOT_TOKEN",
        "SLACK_UPDATE_APP_TOKEN",
        "SLACK_SCHEDULE_BOT_TOKEN",
        "SLACK_SCHEDULE_APP_TOKEN",
    ):
        assert name in err
    assert "Slack에서 부르기" in err


def test_slack_command_refuses_to_start_with_empty_allow_list(monkeypatch, capsys):
    async def must_not_run(cfg):  # pragma: no cover
        raise AssertionError("bot must not start")

    monkeypatch.setattr(slack_bot, "run_bots", must_not_run)
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_TOKEN)
    monkeypatch.setenv("SLACK_APP_TOKEN", "-".join(["xapp", "1", "A0123", "1234567890", "abcdef"]))
    monkeypatch.setenv("SLACK_ALLOWED_USER_IDS", " , ")
    assert main(["slack"]) == 1
    err = capsys.readouterr().err
    assert "SLACK_ALLOWED_USER_IDS가 비어 있어 봇을 시작하지 않습니다" in err
    assert SLACK_TOKEN not in err


def test_brief_slack_without_token_or_destination_is_a_korean_error(capsys):
    assert main(["--brief", "--slack"]) == 1
    err = capsys.readouterr().err
    assert "빠진 환경변수: SLACK_BOT_TOKEN, SLACK_BRIEF_CHANNEL(또는 SLACK_ALLOWED_USER_IDS)" in err
    assert "브리핑을 보낼 곳이 없습니다" in err


def test_brief_problems_accept_allowed_users_instead_of_a_channel():
    base = {"SLACK_BOT_TOKEN": "xoxb-1-2-abc"}
    # SLACK_BRIEF_CHANNEL is optional now: the allowed users get DMs.
    assert config.slack_brief_problems(config.load_slack_config({**base, "SLACK_ALLOWED_USER_IDS": OWNER})) == []
    assert config.slack_brief_problems(config.load_slack_config({**base, "SLACK_BRIEF_CHANNEL": CHANNEL})) == []
    bad = "\n".join(config.slack_brief_problems(config.load_slack_config({**base, "SLACK_ALLOWED_USER_IDS": "@kim"})))
    assert "@kim" in bad and "DM" in bad
    # Invalid user ids do not matter when a channel is set.
    with_channel = config.load_slack_config({**base, "SLACK_ALLOWED_USER_IDS": "@kim", "SLACK_BRIEF_CHANNEL": CHANNEL})
    assert config.slack_brief_problems(with_channel) == []


def test_slack_cli_argument_rules(capsys):
    for argv in (["--slack"], ["--slack", "질문"], ["slack", "--brief"], ["slack", "--slack"]):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--slack은 --brief와 함께 써야 합니다." in err
    assert "slack 명령은" in err


# ---------------------------------------------------------------- --brief --slack


def test_post_briefing_posts_header_and_threads_overflow(tmp_path):
    client = FakeSlackClient()
    body = "\n\n".join(f"## 섹션 {i}\n" + "내용 " * 400 for i in range(3))
    run = FakeRun(TurnResult(text=body, session_id=SESSION_1), statuses=())
    sessions = ThreadSessions(tmp_path / "threads.json")
    now = datetime(2026, 10, 4, 23, 30, tzinfo=timezone.utc)  # 08:30 on 10-05 in Seoul

    code = asyncio.run(
        post_briefing(client, CHANNEL, run=run, sessions=sessions, now=now, env={"TIMEZONE": "Asia/Seoul"})
    )
    assert code == 0
    assert "2026-10-05 (월요일)" in run.calls[0]["prompt"]
    assert run.calls[0]["extra_system_prompt"] == SLACK_FORMAT_PROMPT
    assert run.calls[0]["persona"] == "mungchi"
    assert run.calls[0]["briefing"] is True  # --brief --slack is the scheduled briefing
    first, *rest = client.posts
    assert first["channel"] == CHANNEL and "thread_ts" not in first
    # This env has no BRIEF_WEATHER=off, so the weather line is there; tests are offline, so it is the failure note.
    assert first["text"].startswith("☀️ *오늘의 브리핑 (10/05 월)*\n🌤️ *서울 날씨*: 가져오지 못했어요\n\n*섹션 0*")
    assert rest and all(p["thread_ts"] == first["_ts"] for p in rest)
    # The credits come last, appended by code (no gateway configured here: a one-line note).
    assert rest[-1]["text"].endswith("\n\n💳 *Chat KHU 크레딧*: 확인 안 함 (Chat KHU 게이트웨이를 쓰지 않아요)")
    assert all(len(p["text"]) <= 3_500 for p in client.posts)
    assert client.updates == []
    # Mentioning the bot in the briefing's thread continues the briefing session.
    assert sessions.get(CHANNEL, first["_ts"], persona="mungchi") == SESSION_1


def test_post_briefing_reports_failures_in_slack(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_TOKEN)
    client = FakeSlackClient()
    code = asyncio.run(post_briefing(client, CHANNEL, run=FakeRun(RuntimeError(f"bad {SLACK_TOKEN}"))))
    assert code == 1
    [post] = client.posts
    assert "오늘 브리핑을 만들지 못했어요 (RuntimeError)" in post["text"]
    assert SLACK_TOKEN not in post["text"]


def test_brief_slack_cli_uses_bot_token_client(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_TOKEN)
    monkeypatch.setenv("SLACK_BRIEF_CHANNEL", CHANNEL)
    client = FakeSlackClient()
    created: list[str] = []

    def fake_client(token):
        created.append(token)
        return client

    run = FakeRun(TurnResult(text="*① 공저자 업데이트*\n• 변경 없음", session_id=SESSION_1))
    monkeypatch.setattr(slack_bot, "AsyncWebClient", fake_client)
    monkeypatch.setattr(slack_bot, "run_turn", run)
    monkeypatch.setattr(slack_bot, "setup_logging", lambda: None)  # keep pytest's logging intact

    assert main(["--brief", "--slack"]) == 0
    assert created == [SLACK_TOKEN]
    [post] = client.posts
    assert post["channel"] == CHANNEL
    assert "\n\n*① 공저자 업데이트*\n• 변경 없음\n\n💳 *Chat KHU 크레딧*: " in post["text"]
    err = capsys.readouterr().err
    assert "→ 업뎃에게 맡기는 중..." in err  # progress goes to stderr
    assert "Slack에 오늘 브리핑을 올렸습니다 (채널 C0123ABCD)." in err
    assert ThreadSessions(config.get_slack_threads_path()).get(CHANNEL, post["_ts"], persona="mungchi") == SESSION_1


# ---------------------------------------------------------------- Bolt wiring


def test_bolt_app_builds_offline_and_routes_events(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    cfg = config.load_slack_config(
        {
            "SLACK_BOT_TOKEN": "xoxb-fake-fake-fake",
            "SLACK_APP_TOKEN": "xapp-fake-fake",
            "SLACK_ALLOWED_USER_IDS": OWNER,
            "SLACK_MAX_CONCURRENT": "3",
        }
    )
    app, handler = build_app(cfg, sessions=ThreadSessions(tmp_path / "t.json"))
    assert app.process_before_response is False  # ack first, then run the listener
    listeners = {listener.ack_function.__name__: listener for listener in app._async_listeners}
    assert set(listeners) == {"on_app_mention", "on_message"}
    assert all(listener.auto_acknowledgement for listener in listeners.values())
    assert handler.client is app.client
    assert handler.allowed_user_ids == {OWNER}
    assert handler._semaphore._value == 3

    from slack_bolt.request.async_request import AsyncBoltRequest
    from slack_bolt.response import BoltResponse

    fake_client, run = FakeSlackClient(), FakeRun()
    handler.client, handler.run = fake_client, run
    context = SimpleNamespace(bot_user_id=BOT)

    async def scenario():
        for name, event in (("on_app_mention", mention()), ("on_message", dm())):
            body = {"type": "event_callback", "event_id": f"Ev-{name}", "event": event}
            request = AsyncBoltRequest(body=body, mode="socket_mode")
            listener = listeners[name]
            matched = [await m.async_matches(request, BoltResponse(status=200)) for m in listener.matchers]
            assert all(matched)
            await listener.ack_function(event=event, body=body, context=context)

    asyncio.run(scenario())
    assert handler.bot_user_id == BOT
    assert [c["prompt"] for c in run.calls] == ["오늘 일정 알려줘", "오늘 일정 알려줘"]


# ---------------------------------------------------------------- manifests

MANIFEST_DIR = Path(__file__).resolve().parents[1] / "slack_manifests"
# file name -> (persona, Korean app name, ASCII handle)
MANIFESTS = {
    "moongchi.yaml": ("mungchi", "비서실 고뭉치", "moongchi"),
    "update.yaml": ("update", "업뎃", "update"),
    "schedule.yaml": ("schedule", "일정", "schedule"),
}


def _manifest_list(lines: list[str], key: str) -> list[str]:
    start = next(i for i, line in enumerate(lines) if line.strip() == f"{key}:")
    indent = len(lines[start]) - len(lines[start].lstrip())
    items = []
    for line in lines[start + 1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= indent and not stripped.startswith("- "):
            break
        if stripped.startswith("- "):
            items.append(stripped[2:].strip())
    return items


def _manifest(name: str) -> str:
    return (MANIFEST_DIR / name).read_text(encoding="utf-8")


def test_manifest_directory_has_exactly_the_three_bots():
    assert sorted(path.name for path in MANIFEST_DIR.glob("*.yaml")) == sorted(MANIFESTS)
    assert not (MANIFEST_DIR.parent / "slack_manifest.yaml").exists()  # moved into slack_manifests/


@pytest.mark.parametrize("name", sorted(MANIFESTS))
def test_slack_manifest_requests_only_needed_scopes(name):
    text = _manifest(name)
    lines = text.splitlines()
    assert sorted(_manifest_list(lines, "bot")) == sorted(
        ["app_mentions:read", "chat:write", "im:history", "im:read", "im:write"]
    )
    assert sorted(_manifest_list(lines, "bot_events")) == ["app_mention", "message.im"]
    assert "socket_mode_enabled: true" in text
    assert "messages_tab_enabled: true" in text
    assert "messages_tab_read_only_enabled: false" in text
    assert "token_rotation_enabled: false" in text
    assert not any(line.strip() == "user:" for line in lines)  # no user-token scopes


def test_slack_manifests_share_identical_scopes_and_events():
    def surface(name):
        lines = _manifest(name).splitlines()
        return sorted(_manifest_list(lines, "bot")), sorted(_manifest_list(lines, "bot_events"))

    assert len({str(surface(name)) for name in MANIFESTS}) == 1


@pytest.mark.parametrize("name", sorted(MANIFESTS))
def test_slack_manifest_app_name_is_korean_persona(name):
    # PyYAML is not a dependency, so read display_information.name with a regex.
    text = _manifest(name)
    match = re.search(r"^display_information:\n(?:[ \t]+.*\n)*?[ \t]+name:[ \t]*(.+?)[ \t]*$", text, re.MULTILINE)
    assert match, "display_information.name not found"
    app_name = match.group(1).strip("\"'")
    persona, expected_name, _handle = MANIFESTS[name]
    assert app_name == expected_name == SLACK_APP_NAMES[persona]
    assert re.search(r"[가-힣]", app_name)
    description = re.search(r"^[ \t]+description:[ \t]*(.+?)[ \t]*$", text, re.MULTILINE)
    assert description and re.search(r"[가-힣]", description.group(1)) and len(description.group(1)) <= 140


@pytest.mark.parametrize("name", sorted(MANIFESTS))
def test_slack_manifest_bot_display_name_is_ascii_handle(name):
    # Slack derives the bot's @handle from display_name and rejects non-ASCII values.
    text = _manifest(name)
    match = re.search(r"^\s+display_name:\s*[\"']?([^\"'#\n]*?)[\"']?\s*(?:#.*)?$", text, re.MULTILINE)
    assert match, "features.bot_user.display_name not found"
    display_name = match.group(1)
    assert display_name.isascii()
    assert re.fullmatch(r"[a-z0-9][a-z0-9._-]*", display_name)
    persona, app_name, handle = MANIFESTS[name]
    assert display_name == handle == SLACK_HANDLES[persona]
    assert name == f"{handle}.yaml"
    # The comment explains why the handle is ASCII and how @<Korean name> still finds the bot.
    assert f"@{handle}" in text and "ASCII" in text and "자동완성" in text


def test_user_facing_slack_texts_use_moongchi_name_and_handle():
    assert PLACEHOLDER_TEXT == "🗂️ 고뭉치가 확인 중이에요..."
    assert "고뭉치" in CRASH_TEXT
    assert "/invite @moongchi" in slack_bot.SLACK_ERROR_HINTS["not_in_channel"]
    assert "@mungchi" not in slack_bot.SLACK_ERROR_HINTS["not_in_channel"]


# ---------------------------------------------------------------- secrets


def test_outgoing_slack_text_is_scrubbed(tmp_path, monkeypatch):
    ics = "https://calendar.google.com/calendar/ical/me%40gmail.com/private-0123abcd/basic.ics"
    monkeypatch.setenv("CALENDAR_ICS_URLS", ics)
    run = FakeRun(TurnResult(text=f"주소는 {ics} 이고 토큰은 {SLACK_TOKEN}", session_id=SESSION_1))
    handler, client, _ = make_handler(tmp_path, run=run)
    asyncio.run(handler.handle_event(mention(), event_id="Ev1", source="mention"))
    final = client.updates[-1]["text"]
    assert ics not in final and SLACK_TOKEN not in final
    assert final.startswith("주소는 ")


def test_scrub_filter_cleans_log_records():
    record = logging.LogRecord("slack_bolt", logging.ERROR, __file__, 1, "token %s", (SLACK_TOKEN,), None)
    try:
        raise RuntimeError(f"failed with {SLACK_TOKEN}")
    except RuntimeError:
        record.exc_info = sys.exc_info()
    assert slack_bot.ScrubFilter().filter(record)
    formatted = logging.Formatter().format(record)
    assert SLACK_TOKEN not in formatted
    assert "RuntimeError" in formatted


def test_session_store_write_errors_do_not_block_the_reply(tmp_path):
    class BrokenSessions(ThreadSessions):
        def set(self, channel, thread_ts, session_id, *, persona):
            raise OSError("disk full")

    handler, client, _ = make_handler(tmp_path)
    handler.sessions = BrokenSessions(tmp_path / "t.json")
    asyncio.run(handler.handle_event(mention(), event_id="Ev1", source="mention"))
    assert client.updates[-1]["text"] == "답변입니다."


# ---------------------------------------------------------------- personas (업뎃 / 일정 bots)


def test_per_persona_texts_use_the_right_names_and_particles():
    assert slack_bot.BOT_TEXTS["mungchi"].placeholder == "🗂️ 고뭉치가 확인 중이에요..."
    assert slack_bot.BOT_TEXTS["update"].placeholder == "📝 업뎃이 확인 중이에요..."
    assert slack_bot.BOT_TEXTS["schedule"].placeholder == "⏰ 일정이 확인 중이에요..."
    assert PLACEHOLDERS == {p: slack_bot.BOT_TEXTS[p].placeholder for p in ("mungchi", "update", "schedule")}
    # 고뭉치's texts are unchanged.
    assert slack_bot.EMPTY_ANSWER_TEXT == "고뭉치가 빈 답을 보냈어요. 다시 물어봐 주세요."
    assert slack_bot.FAILED_TEXT == "⚠️ 고뭉치가 답을 끝내지 못했어요."
    assert CRASH_TEXT == "⚠️ 고뭉치를 실행하지 못했어요 ({kind}). 잠시 후 다시 시도해 주세요."
    update, schedule = slack_bot.BOT_TEXTS["update"], slack_bot.BOT_TEXTS["schedule"]
    assert update.empty_answer.startswith("업뎃이 ") and schedule.empty_answer.startswith("일정이 ")
    assert update.failed == "⚠️ 업뎃이 답을 끝내지 못했어요."
    assert schedule.crash.format(kind="X") == "⚠️ 일정을 실행하지 못했어요 (X). 잠시 후 다시 시도해 주세요."
    assert compose_reply(TurnResult(text=""), "update") == update.empty_answer
    assert compose_reply(TurnResult(text="", failed=True), "schedule") == schedule.failed


@pytest.mark.parametrize(
    "persona,handle,bot_env,app_env",
    [
        ("mungchi", "moongchi", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"),
        ("update", "update", "SLACK_UPDATE_BOT_TOKEN", "SLACK_UPDATE_APP_TOKEN"),
        ("schedule", "schedule", "SLACK_SCHEDULE_BOT_TOKEN", "SLACK_SCHEDULE_APP_TOKEN"),
    ],
)
def test_error_hints_name_each_bots_handle_env_vars_and_manifest(persona, handle, bot_env, app_env):
    hints = slack_bot.slack_error_hints(persona)
    assert f"/invite @{handle} " in hints["not_in_channel"]
    assert f"{bot_env}과 {app_env}을 확인하세요" in hints["invalid_auth"]
    manifest = re.search(r"(slack_manifests/\S+\.yaml)", hints["missing_scope"]).group(1)
    assert (Path(__file__).resolve().parents[1] / manifest).is_file()
    exc = RuntimeError("boom")
    exc.response = {"error": "not_in_channel"}
    assert f"/invite @{handle}" in slack_bot.describe_slack_error(exc, persona)


@pytest.mark.parametrize(
    "persona,expected",
    [("update", "공저자 업데이트 확인해줘"), ("schedule", "오늘과 내일 일정 알려줘")],
)
def test_direct_bot_empty_mention_defaults(tmp_path, persona, expected):
    handler, client, run = make_handler(tmp_path, persona=persona)
    asyncio.run(handler.handle_event(mention(f"<@{BOT}>"), event_id="Ev1", source="mention"))
    assert run.calls[0]["prompt"] == expected
    assert run.calls[0]["persona"] == persona


@pytest.mark.parametrize("persona", ["update", "schedule"])
def test_direct_bot_shows_only_its_placeholder_then_the_answer(tmp_path, persona):
    handler, client, run = make_handler(tmp_path, persona=persona)  # FakeRun still emits status lines
    asyncio.run(handler.handle_event(mention(), event_id="Ev1", source="mention"))
    placeholder = slack_bot.BOT_TEXTS[persona].placeholder
    assert client.posts[0]["text"] == placeholder
    assert [u["text"] for u in client.updates] == ["답변입니다."]  # no "→ ...에게 맡기는 중" status edits
    call = run.calls[0]
    assert call["persona"] == persona and call["extra_system_prompt"] == SLACK_FORMAT_PROMPT
    assert handler.sessions.get(CHANNEL, "1700000000.000100", persona=persona) == SESSION_1
    assert handler.sessions.get(CHANNEL, "1700000000.000100", persona="mungchi") is None


@pytest.mark.parametrize("persona", ["update", "schedule"])
def test_direct_bots_refuse_strangers_and_never_run(tmp_path, persona):
    handler, client, run = make_handler(tmp_path, persona=persona)

    async def scenario():
        await handler.handle_event(mention(user=STRANGER), event_id="Ev1", source="mention")
        await handler.handle_event(dm(user=STRANGER), event_id="Ev2", source="dm")
        await handler.handle_event(mention(f"<@{BOT}>", user=STRANGER, ts="1.000001"), event_id="Ev3", source="mention")

    asyncio.run(scenario())
    assert run.calls == []
    assert [p["text"] for p in client.posts] == [REFUSAL_TEXT, REFUSAL_TEXT, REFUSAL_TEXT]
    assert client.updates == []


def test_bots_in_the_same_thread_keep_separate_sessions(tmp_path):
    sessions = ThreadSessions(tmp_path / "threads.json")
    root = "1700000000.000100"
    handlers = {}
    for persona, session in (("mungchi", SESSION_1), ("update", SESSION_2)):
        handler, _, run = make_handler(tmp_path, run=FakeRun(TurnResult(text="ok", session_id=session)), persona=persona)
        handler.sessions = sessions
        handlers[persona] = (handler, run)

    async def scenario():
        for persona, (handler, _run) in handlers.items():
            await handler.handle_event(mention(ts=root), event_id=f"E-{persona}", source="mention")
        for persona, (handler, _run) in handlers.items():
            await handler.handle_event(
                mention(ts=f"1700000000.0002{len(persona)}", thread_ts=root), event_id=f"F-{persona}", source="mention"
            )

    asyncio.run(scenario())
    assert [c["resume"] for c in handlers["mungchi"][1].calls] == [None, SESSION_1]
    assert [c["resume"] for c in handlers["update"][1].calls] == [None, SESSION_2]


def test_messages_from_any_of_our_bots_are_ignored(tmp_path):
    ours = {"UOTHERBOT"}
    handler, client, run = make_handler(tmp_path, persona="update", our_bot_user_ids=ours)
    assert handler.ignore_reason(mention(user="UOTHERBOT"), "mention") == "our_bot"
    asyncio.run(handler.handle_event(mention(user="UOTHERBOT"), event_id="Ev1", source="mention"))
    assert client.calls == [] and run.calls == []
    ours.add("ULATER")  # the set is shared and filled as bots connect
    assert handler.ignore_reason(dm(user="ULATER"), "dm") == "our_bot"


def test_all_bots_share_one_concurrency_cap(tmp_path):
    shared = asyncio.Semaphore(1)
    runs = []
    handlers = []
    for persona in ("mungchi", "update", "schedule"):
        run = FakeRun(TurnResult(text="ok", session_id=SESSION_1), statuses=(), delay=0.02)
        handler, _, run = make_handler(tmp_path / persona, run=run, persona=persona, semaphore=shared)
        runs.append(run)
        handlers.append(handler)
    active = {"now": 0, "max": 0}
    for run in runs:
        original = run.__call__

        async def tracked(prompt, _original=original, **kwargs):
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            try:
                return await _original(prompt, **kwargs)
            finally:
                active["now"] -= 1

        for handler in handlers:
            if handler.run is run:
                handler.run = tracked

    async def scenario():
        await asyncio.gather(
            *(h.handle_event(mention(ts=f"1700000000.00030{i}"), event_id=f"E{i}", source="mention") for i, h in enumerate(handlers))
        )

    asyncio.run(scenario())
    assert all(len(run.calls) == 1 for run in runs)
    assert active["max"] == 1  # SLACK_MAX_CONCURRENT applies across bots


def test_handler_rejects_unknown_persona(tmp_path):
    with pytest.raises(ValueError):
        SlackHandler(FakeSlackClient(), allowed_user_ids={OWNER}, sessions=ThreadSessions(tmp_path / "t.json"), persona="nobody")


# ---------------------------------------------------------------- multi-bot configuration


def _token(kind: str, tag: str) -> str:
    # Assembled at runtime so no token-shaped literal is committed.
    return "-".join([kind, "1", tag, "abcdef"])


ALL_BOTS_ENV = {
    "SLACK_BOT_TOKEN": _token("xoxb", "M"),
    "SLACK_APP_TOKEN": _token("xapp", "M"),
    "SLACK_UPDATE_BOT_TOKEN": _token("xoxb", "U"),
    "SLACK_UPDATE_APP_TOKEN": _token("xapp", "U"),
    "SLACK_SCHEDULE_BOT_TOKEN": _token("xoxb", "S"),
    "SLACK_SCHEDULE_APP_TOKEN": _token("xapp", "S"),
    "SLACK_ALLOWED_USER_IDS": OWNER,
}


def test_multi_bot_config_all_three_configured():
    cfg = config.load_slack_config(ALL_BOTS_ENV)
    assert config.slack_bot_problems(cfg) == []
    assert [bot.persona for bot in cfg.configured_bots] == ["mungchi", "update", "schedule"]
    assert cfg.bot("update").bot_token == ALL_BOTS_ENV["SLACK_UPDATE_BOT_TOKEN"]
    assert cfg.bot("schedule").app_token == ALL_BOTS_ENV["SLACK_SCHEDULE_APP_TOKEN"]
    assert (cfg.bot_token, cfg.app_token) == (ALL_BOTS_ENV["SLACK_BOT_TOKEN"], ALL_BOTS_ENV["SLACK_APP_TOKEN"])
    assert all(value not in repr(cfg) for key, value in ALL_BOTS_ENV.items() if "TOKEN" in key)


def test_multi_bot_config_any_single_bot_is_enough():
    for persona, (bot_env, app_env) in config.SLACK_BOT_ENV.items():
        env = {bot_env: ALL_BOTS_ENV[bot_env], app_env: ALL_BOTS_ENV[app_env], "SLACK_ALLOWED_USER_IDS": OWNER}
        cfg = config.load_slack_config(env)
        assert config.slack_bot_problems(cfg) == [], persona
        assert [bot.persona for bot in cfg.configured_bots] == [persona]


def test_multi_bot_config_none_configured():
    problems = "\n".join(config.slack_bot_problems(config.load_slack_config({"SLACK_ALLOWED_USER_IDS": OWNER})))
    assert "Slack 봇 토큰이 하나도 없습니다" in problems
    for bot_env, app_env in config.SLACK_BOT_ENV.values():
        assert bot_env in problems and app_env in problems


@pytest.mark.parametrize(
    "present,missing",
    [
        ("SLACK_UPDATE_BOT_TOKEN", "SLACK_UPDATE_APP_TOKEN"),
        ("SLACK_SCHEDULE_APP_TOKEN", "SLACK_SCHEDULE_BOT_TOKEN"),
        ("SLACK_APP_TOKEN", "SLACK_BOT_TOKEN"),
    ],
)
def test_multi_bot_config_half_configured_names_the_missing_variable(present, missing):
    env = {**ALL_BOTS_ENV}
    del env[missing]
    problems = config.slack_bot_problems(config.load_slack_config(env))
    assert problems[0] == f"빠진 환경변수: {missing}"
    assert any("토큰이 하나만 있습니다" in p for p in problems)
    assert not any(value in "\n".join(problems) for key, value in env.items() if "TOKEN" in key)


def test_multi_bot_config_requires_allowed_users_and_distinct_tokens():
    env = {k: v for k, v in ALL_BOTS_ENV.items() if k != "SLACK_ALLOWED_USER_IDS"}
    problems = "\n".join(config.slack_bot_problems(config.load_slack_config(env)))
    assert "빠진 환경변수: SLACK_ALLOWED_USER_IDS" in problems
    assert "SLACK_ALLOWED_USER_IDS가 비어 있어 봇을 시작하지 않습니다" in problems

    same = {**ALL_BOTS_ENV, "SLACK_UPDATE_BOT_TOKEN": ALL_BOTS_ENV["SLACK_BOT_TOKEN"]}
    problems = "\n".join(config.slack_bot_problems(config.load_slack_config(same)))
    assert "같은 토큰이 여러 변수에 들어 있습니다: SLACK_BOT_TOKEN, SLACK_UPDATE_BOT_TOKEN" in problems
    assert ALL_BOTS_ENV["SLACK_BOT_TOKEN"] not in problems

    swapped = {**ALL_BOTS_ENV, "SLACK_SCHEDULE_BOT_TOKEN": _token("xapp", "X")}
    problems = "\n".join(config.slack_bot_problems(config.load_slack_config(swapped)))
    assert "SLACK_SCHEDULE_BOT_TOKEN 값은 xoxb-로 시작" in problems and "SLACK_SCHEDULE_APP_TOKEN에 넣으세요" in problems


def test_new_slack_tokens_are_scrubbed(monkeypatch):
    for key, value in ALL_BOTS_ENV.items():
        monkeypatch.setenv(key, value)
    custom = "custom-update-bot-secret"
    monkeypatch.setenv("SLACK_UPDATE_BOT_TOKEN", custom)
    assert custom not in slack_bot.scrub(f"failed with {custom}")
    for name in ("SLACK_UPDATE_BOT_TOKEN", "SLACK_UPDATE_APP_TOKEN", "SLACK_SCHEDULE_BOT_TOKEN", "SLACK_SCHEDULE_APP_TOKEN"):
        assert name in config.SECRET_ENV_VARS


# ---------------------------------------------------------------- three Bolt apps


def _no_network(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)


def test_three_bolt_apps_build_offline_with_shared_state(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    cfg = config.load_slack_config({**ALL_BOTS_ENV, "SLACK_MAX_CONCURRENT": "3"})
    sessions = ThreadSessions(tmp_path / "t.json")
    apps = slack_bot.build_apps(cfg, sessions=sessions)
    assert [bot.persona for bot, _app, _handler in apps] == ["mungchi", "update", "schedule"]
    handlers = [handler for _bot, _app, handler in apps]
    for bot, app, handler in apps:
        listeners = {listener.ack_function.__name__ for listener in app._async_listeners}
        assert listeners == {"on_app_mention", "on_message"}
        assert all(listener.auto_acknowledgement for listener in app._async_listeners)
        assert app.process_before_response is False
        assert handler.client is app.client
        assert handler.persona == bot.persona
        assert handler.allowed_user_ids == {OWNER}
        assert handler.sessions is sessions
    assert len({id(h._semaphore) for h in handlers}) == 1 and handlers[0]._semaphore._value == 3
    assert len({id(h.our_bot_user_ids) for h in handlers}) == 1
    assert len({id(app.client) for _bot, app, _handler in apps}) == 3


def test_only_configured_bots_get_an_app(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    env = {k: v for k, v in ALL_BOTS_ENV.items() if not k.startswith(("SLACK_BOT", "SLACK_APP", "SLACK_UPDATE"))}
    apps = slack_bot.build_apps(config.load_slack_config(env), sessions=ThreadSessions(tmp_path / "t.json"))
    assert [bot.persona for bot, _app, _handler in apps] == ["schedule"]


class FakeSocket:
    instances: list["FakeSocket"] = []

    def __init__(self, app, app_token):
        self.app, self.app_token = app, app_token
        self.connected = self.closed = False
        FakeSocket.instances.append(self)

    async def connect_async(self):
        self.connected = True

    async def close_async(self):
        self.closed = True


def _fake_auth(user_id, error=None):
    async def auth_test():
        if error:
            from slack_sdk.errors import SlackApiError

            raise SlackApiError("auth failed", {"ok": False, "error": error})
        return {"ok": True, "user_id": user_id}

    return auth_test


def _patch_build_apps(monkeypatch, tmp_path, errors=None):
    real = slack_bot.build_apps
    built = []

    def fake_build_apps(cfg, **kwargs):
        apps = real(cfg, sessions=ThreadSessions(tmp_path / "t.json"))
        for bot, app, _handler in apps:
            app.client.auth_test = _fake_auth(f"UBOT{bot.persona.upper()}", (errors or {}).get(bot.persona))
        built.extend(apps)
        return apps

    monkeypatch.setattr(slack_bot, "build_apps", fake_build_apps)
    return built


def test_run_bots_starts_every_configured_bot_in_one_loop(tmp_path, monkeypatch, capsys):
    _no_network(monkeypatch)
    FakeSocket.instances = []
    built = _patch_build_apps(monkeypatch, tmp_path)
    env = {k: v for k, v in ALL_BOTS_ENV.items() if not k.startswith("SLACK_SCHEDULE")}
    cfg = config.load_slack_config(env)

    async def no_wait():
        return None

    assert asyncio.run(slack_bot.run_bots(cfg, socket_factory=FakeSocket, wait=no_wait)) == 0
    assert [s.app_token for s in FakeSocket.instances] == [env["SLACK_APP_TOKEN"], env["SLACK_UPDATE_APP_TOKEN"]]
    assert all(s.connected and s.closed for s in FakeSocket.instances)
    handlers = [handler for _bot, _app, handler in built]
    assert [h.bot_user_id for h in handlers] == ["UBOTMUNGCHI", "UBOTUPDATE"]
    assert handlers[0].our_bot_user_ids == {"UBOTMUNGCHI", "UBOTUPDATE"}
    err = capsys.readouterr().err
    assert "Slack 봇 2개를 시작했습니다" in err and "고뭉치(@moongchi), 업뎃(@update)" in err
    assert "토큰이 없어 켜지 않은 봇: 일정(@schedule): SLACK_SCHEDULE_BOT_TOKEN, SLACK_SCHEDULE_APP_TOKEN" in err
    assert not any(value in err for key, value in env.items() if "TOKEN" in key)


def test_run_bots_reports_which_bot_failed_to_connect(tmp_path, monkeypatch, capsys):
    _no_network(monkeypatch)
    FakeSocket.instances = []
    _patch_build_apps(monkeypatch, tmp_path, errors={"update": "invalid_auth"})
    cfg = config.load_slack_config(ALL_BOTS_ENV)

    async def must_not_wait():  # pragma: no cover
        raise AssertionError("must not serve after a failed connection")

    assert asyncio.run(slack_bot.run_bots(cfg, socket_factory=FakeSocket, wait=must_not_wait)) == 1
    err = capsys.readouterr().err
    assert "[오류] 업뎃 봇(@update)을 Slack에 연결하지 못했습니다" in err
    assert "SLACK_UPDATE_BOT_TOKEN과 SLACK_UPDATE_APP_TOKEN을 확인하세요" in err
    assert [s.closed for s in FakeSocket.instances] == [True]  # 고뭉치's connection is closed again


def test_slack_command_runs_all_configured_bots(monkeypatch):
    for key, value in ALL_BOTS_ENV.items():
        monkeypatch.setenv(key, value)
    seen = []

    async def fake_run_bots(cfg):
        seen.append([bot.persona for bot in cfg.configured_bots])
        return 0

    monkeypatch.setattr(slack_bot, "run_bots", fake_run_bots)
    monkeypatch.setattr(slack_bot, "setup_logging", lambda: None)
    assert main(["slack"]) == 0
    assert seen == [["mungchi", "update", "schedule"]]


# ---------------------------------------------------------------- credit shortcut (no LLM)


class FakeCredits:
    """Stands in for ``credits.slack_credit_text``: counts calls, returns a canned summary."""

    TEXT = "💳 *Chat KHU 크레딧*: 9,050.5 남음 / 10,000 (90.5%) · 11/01 갱신\n이번 달 사용: 949.5 (10/01–10/07, 94회)"

    def __init__(self, result=None):
        self.result = result if result is not None else self.TEXT
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.mark.parametrize("persona", ["mungchi", "update", "schedule"])
@pytest.mark.parametrize(
    "event,source",
    [
        (mention(f"<@{BOT}> 크레딧"), "mention"),
        (mention(f"<@{BOT}>  남은 크레딧 얼마나 남았어?"), "mention"),
        (dm("credits"), "dm"),
        (dm("사용량 보여줘"), "dm"),
    ],
)
def test_credit_shortcut_answers_without_an_agent_turn(tmp_path, persona, event, source):
    fake = FakeCredits()
    handler, client, run = make_handler(tmp_path, persona=persona, credit_text=fake)
    asyncio.run(handler.handle_event(event, event_id="Ev1", source=source))
    assert run.calls == []  # run_turn is never called: no LLM
    assert fake.calls == 1
    [post] = client.posts
    assert post["text"] == FakeCredits.TEXT
    assert post["thread_ts"] == event["ts"] and post["channel"] == event["channel"]
    assert client.updates == []  # no placeholder to edit
    # No session / thread-map entry for a shortcut reply.
    assert not (tmp_path / "threads.json").exists()
    assert handler.sessions.threads() == {}


def test_credit_shortcut_in_a_thread_replies_there_and_keeps_the_threads_session(tmp_path):
    root = "1700000000.000100"
    handler, client, run = make_handler(tmp_path, credit_text=FakeCredits())
    handler.sessions.set(CHANNEL, root, SESSION_1, persona="mungchi")
    asyncio.run(handler.handle_event(mention(f"<@{BOT}> 크레딧?", ts="1700000000.000500", thread_ts=root), event_id="E", source="mention"))
    assert run.calls == []
    assert client.posts[0]["thread_ts"] == root
    assert handler.sessions.threads() == {f"mungchi:{CHANNEL}:{root}": SESSION_1}


@pytest.mark.parametrize("persona", ["mungchi", "update", "schedule"])
def test_credit_shortcut_still_refuses_strangers(tmp_path, persona):
    fake = FakeCredits()
    handler, client, run = make_handler(tmp_path, persona=persona, credit_text=fake)

    async def scenario():
        await handler.handle_event(mention(f"<@{BOT}> 크레딧", user=STRANGER), event_id="Ev1", source="mention")
        await handler.handle_event(dm("크레딧", user=STRANGER), event_id="Ev2", source="dm")

    asyncio.run(scenario())
    assert fake.calls == 0 and run.calls == []
    assert [p["text"] for p in client.posts] == [REFUSAL_TEXT, REFUSAL_TEXT]


def test_longer_credit_questions_go_to_the_agent(tmp_path):
    fake = FakeCredits()
    handler, client, run = make_handler(tmp_path, credit_text=fake)
    asyncio.run(handler.handle_event(mention(f"<@{BOT}> 크레딧 아끼려면 어떻게 해?"), event_id="Ev1", source="mention"))
    assert fake.calls == 0
    assert [c["prompt"] for c in run.calls] == ["크레딧 아끼려면 어떻게 해?"]


def test_credit_shortcut_failure_is_a_short_korean_note_without_secrets(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_TOKEN)
    fake = FakeCredits(RuntimeError(f"boom {SLACK_TOKEN}"))
    handler, client, run = make_handler(tmp_path, credit_text=fake)
    with caplog.at_level(logging.ERROR, logger="mungchi.slack"):
        asyncio.run(handler.handle_event(mention(f"<@{BOT}> 크레딧"), event_id="Ev1", source="mention"))
    assert run.calls == []
    assert [p["text"] for p in client.posts] == [slack_bot.CREDIT_CRASH_TEXT.format(kind="RuntimeError")]
    assert SLACK_TOKEN not in caplog.text


def test_default_credit_text_calls_only_the_gateway(tmp_path, monkeypatch):
    import httpx

    from mungchi import credits

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://factchat-cloud.mindlogic.ai/v1/gateway/claude")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "gw-slack-test-token-0123456789")
    paths = []

    def handle(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/credits/"):
            return httpx.Response(200, json={"total": {"quota": 100, "used": 25, "remaining": 75}})
        return httpx.Response(200, json={"rows": []})

    real_client = httpx.Client
    monkeypatch.setattr(credits.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handle), timeout=kw.get("timeout")))
    handler, client, run = make_handler(tmp_path)  # default credit_text
    asyncio.run(handler.handle_event(dm("크레딧"), event_id="Ev1", source="dm"))
    assert run.calls == []
    assert paths == ["/v1/gateway/credits/", "/v1/gateway/usage/"]
    assert client.posts[0]["text"].startswith("💳 *Chat KHU 크레딧*: 75 남음 / 100 (75%)")
    assert "gw-slack-test-token" not in client.posts[0]["text"]


# ---------------------------------------------------------------- low-credit alert


GATEWAY_ENV = {
    "ANTHROPIC_BASE_URL": "https://factchat-cloud.mindlogic.ai/v1/gateway/claude",
    "ANTHROPIC_AUTH_TOKEN": "gw-alert-test-token-0123456789",
}
ALERT_NOW = datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc)


def low_report(remaining=850.0, renewal="2026-11-01T00:00:00+09:00"):
    from mungchi import credits

    payload = {
        "monthly_allocated": {"quota": 10000.0, "used": 10000.0 - remaining, "remaining": remaining, "renewal_date": renewal},
        "total": {"quota": 10000.0, "used": 10000.0 - remaining, "remaining": remaining},
    }
    return credits.CreditReport(balance=credits.parse_balance(payload))


class FakeFetch:
    def __init__(self, *reports):
        self.reports = list(reports)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        report = self.reports[min(self.calls, len(self.reports)) - 1]
        if isinstance(report, BaseException):
            raise report
        return report


def alert(client, store, fetch, env=None, users=(OWNER, "UOTHER1")):
    return asyncio.run(
        slack_bot.check_low_credits(client, users, env=env or GATEWAY_ENV, store=store, fetch=fetch, now=ALERT_NOW)
    )


def test_low_credits_dm_every_allowed_user_once_per_renewal_period(tmp_path):
    from mungchi.state import StateStore

    store = StateStore(tmp_path / "state.json")
    client = FakeSlackClient()
    assert alert(client, store, FakeFetch(low_report())) == "alerted"
    assert sorted(p["channel"] for p in client.posts) == sorted([OWNER, "UOTHER1"])  # DMs: channel=<user id>
    text = client.posts[0]["text"]
    assert text.startswith("⚠️ *Chat KHU 크레딧이 얼마 남지 않았어요* (남은 비율 8.5%, 알림 기준 10%)")
    assert "💳 *Chat KHU 크레딧*: 850 남음 / 10,000 (8.5%) · 11/01 갱신" in text
    assert "gw-alert-test-token" not in text
    assert store.credit_alert_period() == "2026-11-01T00:00:00+09:00"

    # The same period: no second DM, even an hour later with less left.
    assert alert(client, store, FakeFetch(low_report(remaining=300.0))) == "already"
    assert len(client.posts) == 2

    # A new renewal date (the next cycle) alerts again.
    assert alert(client, store, FakeFetch(low_report(renewal="2026-12-01T00:00:00+09:00"))) == "alerted"
    assert len(client.posts) == 4
    assert store.credit_alert_period() == "2026-12-01T00:00:00+09:00"


def test_enough_credits_or_a_disabled_alert_send_nothing(tmp_path):
    from mungchi.state import StateStore

    store = StateStore(tmp_path / "state.json")
    client = FakeSlackClient()
    assert alert(client, store, FakeFetch(low_report(remaining=1500.0))) == "ok"  # 15% left
    for value in ("0", ""):
        fetch = FakeFetch(low_report())
        assert alert(client, store, fetch, env={**GATEWAY_ENV, "CREDIT_ALERT_PERCENT": value}) == "disabled"
        assert fetch.calls == 0  # not even fetched
    assert alert(client, store, FakeFetch(low_report(remaining=1500.0)), env={**GATEWAY_ENV, "CREDIT_ALERT_PERCENT": "20"}) == "alerted"
    assert client.posts and "알림 기준 20%" in client.posts[0]["text"]


def test_other_gateways_are_skipped_silently(tmp_path, caplog):
    from mungchi.state import StateStore

    fetch = FakeFetch(low_report())
    client = FakeSlackClient()
    with caplog.at_level(logging.INFO, logger="mungchi"):
        outcome = alert(client, StateStore(tmp_path / "s.json"), fetch, env={"ANTHROPIC_API_KEY": "x" * 30})
    assert outcome == "unsupported" and fetch.calls == 0 and client.calls == []
    assert caplog.text == ""


def test_failed_checks_and_failed_dms_are_logged_scrubbed_and_never_raise(tmp_path, monkeypatch, caplog):
    from mungchi import credits
    from mungchi.state import StateStore

    store = StateStore(tmp_path / "state.json")
    token = GATEWAY_ENV["ANTHROPIC_AUTH_TOKEN"]
    for key, value in GATEWAY_ENV.items():
        monkeypatch.setenv(key, value)
    failing = credits.CreditReport(error=f"크레딧을 확인하지 못했습니다 (HTTP 401). {token}")
    with caplog.at_level(logging.WARNING, logger="mungchi.slack"):
        assert alert(FakeSlackClient(), store, FakeFetch(failing)) == "failed"
        assert alert(FakeSlackClient(), store, FakeFetch(credits.CreditReport(balance=credits.parse_balance({})))) == "failed"

    class BrokenSlack(FakeSlackClient):
        async def chat_postMessage(self, **kwargs):
            raise RuntimeError(f"slack down {token}")

    with caplog.at_level(logging.WARNING, logger="mungchi.slack"):
        assert alert(BrokenSlack(), store, FakeFetch(low_report())) == "failed"
    assert store.credit_alert_period() is None  # nobody got it: try again next hour
    assert "HTTP 401" in caplog.text and "slack down" in caplog.text
    assert token not in caplog.text


def test_alert_loop_checks_after_a_minute_then_hourly_and_survives_errors():
    sleeps, checks = [], []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 3:
            raise asyncio.CancelledError

    async def flaky_check(client, users, *, env=None):
        checks.append(set(users))
        raise RuntimeError("gateway exploded")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(slack_bot.credit_alert_loop("client", [OWNER], sleep=fake_sleep, check=flaky_check))
    assert sleeps == [60.0, 3600.0, 3600.0, 3600.0]
    assert checks == [{OWNER}] * 3


def test_run_bots_starts_the_alert_with_moongchis_client_and_stops_it(tmp_path, monkeypatch, capsys):
    _no_network(monkeypatch)
    FakeSocket.instances = []
    built = _patch_build_apps(monkeypatch, tmp_path)
    for key, value in GATEWAY_ENV.items():
        monkeypatch.setenv(key, value)
    started, stopped = [], []

    async def fake_alert(client, users):
        started.append((client, set(users)))
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(True)

    async def let_it_start():
        await asyncio.sleep(0)

    cfg = config.load_slack_config(ALL_BOTS_ENV)
    assert asyncio.run(slack_bot.run_bots(cfg, socket_factory=FakeSocket, wait=let_it_start, credit_alert=fake_alert)) == 0
    mungchi_app = next(app for bot, app, _h in built if bot.persona == "mungchi")
    assert started == [(mungchi_app.client, {OWNER})] and stopped == [True]
    assert "크레딧 잔액 알림: 남은 크레딧이 10% 아래로 내려가면 고뭉치(@moongchi) 봇이 DM으로 알립니다" in capsys.readouterr().err


def test_run_bots_uses_the_first_configured_bot_without_moongchi(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    FakeSocket.instances = []
    built = _patch_build_apps(monkeypatch, tmp_path)
    started = []

    async def fake_alert(client, users):
        started.append(client)

    async def let_it_start():
        await asyncio.sleep(0)

    env = {k: v for k, v in ALL_BOTS_ENV.items() if not k.startswith(("SLACK_BOT", "SLACK_APP"))}
    cfg = config.load_slack_config(env)
    assert asyncio.run(slack_bot.run_bots(cfg, socket_factory=FakeSocket, wait=let_it_start, credit_alert=fake_alert)) == 0
    assert [bot.persona for bot, _a, _h in built] == ["update", "schedule"]
    assert started == [built[0][1].client]


# ---------------------------------------------------------------- scheduled morning briefing (BRIEF_TIME)

SEOUL = ZoneInfo("Asia/Seoul")
MORNING = config.load_brief_schedule({"BRIEF_TIME": "07:00"})


class FakeClock:
    def __init__(self, moment: datetime):
        self.now = moment

    def __call__(self) -> datetime:
        return self.now


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """October 2026 in Seoul (10/08 is a Thursday)."""
    return datetime(2026, 10, day, hour, minute, tzinfo=SEOUL)


class DMSlackClient(FakeSlackClient):
    """Like Slack: posting to a member id lands in the bot's DM channel with that person (D...)."""

    async def chat_postMessage(self, **kwargs):
        response = await super().chat_postMessage(**kwargs)
        channel = kwargs["channel"]
        return {**response, "channel": "D" + channel[1:] if channel.startswith("U") else channel}


def tick(client, store, clock, run, *, state, destinations=(OWNER,), sessions=None, credit_fetch=None, schedule=MORNING):
    return asyncio.run(
        slack_bot.morning_brief_tick(
            client,
            list(destinations),
            schedule=schedule,
            state=state,
            store=store,
            clock=clock,
            run=run,
            sessions=sessions,
            credit_fetch=credit_fetch or (lambda: low_report(remaining=9050.5)),
        )
    )


def test_morning_brief_is_sent_once_and_recorded_before_posting(tmp_path):
    store = StateStore(tmp_path / "state.json")
    sessions = ThreadSessions(tmp_path / "threads.json")
    client = DMSlackClient()
    recorded_at_run: list[tuple[str | None, int]] = []

    class RecordingRun(FakeRun):
        async def __call__(self, prompt, **kwargs):
            recorded_at_run.append((store.last_brief_date(), len(client.posts)))
            return await super().__call__(prompt, **kwargs)

    run = RecordingRun(TurnResult(text="*① 오늘의 일정*\n• 일정 없음", session_id=SESSION_1), statuses=())
    clock = FakeClock(at(8, 6, 59))
    state = slack_bot.BriefLoopState()

    assert tick(client, store, clock, run, state=state, sessions=sessions) == "early"
    assert client.posts == [] and run.calls == []

    clock.now = at(8, 7, 0)
    assert tick(client, store, clock, run, state=state, sessions=sessions) == "sent"
    # last_brief_date was written before the run, and so before anything was posted.
    assert recorded_at_run == [("2026-10-08", 0)]
    [post] = client.posts
    assert post["channel"] == OWNER and "thread_ts" not in post  # a DM to the allowed user
    assert post["text"].startswith("☀️ *오늘의 브리핑 (10/08 목)*\n\n*① 오늘의 일정*\n• 일정 없음\n\n💳 *Chat KHU 크레딧*: 9,050.5 남음")
    assert run.calls[0]["briefing"] is True and run.calls[0]["persona"] == "mungchi"
    # Replying to 고뭉치 in the briefing's DM thread continues the briefing session.
    assert sessions.get("DOWNER1", post["_ts"], persona="mungchi") == SESSION_1

    # Later ticks the same day do nothing, in this process and after a restart (fresh state).
    for moment in (at(8, 7, 0), at(8, 7, 30), at(8, 11, 59)):
        clock.now = moment
        assert tick(client, store, clock, run, state=state) == "already"
        assert tick(client, store, clock, run, state=slack_bot.BriefLoopState()) == "already"
    assert len(client.posts) == 1 and len(run.calls) == 1

    # The next morning it goes out again.
    clock.now = at(9, 7, 0)
    assert tick(client, store, clock, run, state=state) == "sent"
    assert len(client.posts) == 2 and store.last_brief_date() == "2026-10-09"
    assert recorded_at_run[-1] == ("2026-10-09", 1)


def test_a_crash_in_the_middle_is_not_resent_after_a_restart(tmp_path):
    store = StateStore(tmp_path / "state.json")
    client = FakeSlackClient()
    killed = FakeRun(asyncio.CancelledError(), statuses=())  # e.g. the process is stopped mid-run
    with pytest.raises(asyncio.CancelledError):
        tick(client, store, FakeClock(at(8, 7, 5)), killed, state=slack_bot.BriefLoopState())
    assert store.last_brief_date() == "2026-10-08"
    # Restarted at 07:06: today's briefing counts as sent, so no duplicate.
    run = FakeRun()
    assert tick(client, store, FakeClock(at(8, 7, 6)), run, state=slack_bot.BriefLoopState()) == "already"
    assert run.calls == [] and client.posts == []


def test_a_late_start_catches_up_until_noon_then_skips_with_one_log_line(tmp_path, caplog):
    store = StateStore(tmp_path / "state.json")
    client, run = FakeSlackClient(), FakeRun(statuses=())
    # The Mac slept through 07:00 and woke at 08:10: the briefing still goes out.
    assert tick(client, store, FakeClock(at(8, 8, 10)), run, state=slack_bot.BriefLoopState()) == "sent"

    other = StateStore(tmp_path / "other.json")
    state = slack_bot.BriefLoopState()
    with caplog.at_level(logging.INFO, logger="mungchi.slack"):
        for moment in (at(8, 12, 30), at(8, 13, 0), at(8, 18, 0)):
            assert tick(client, other, FakeClock(moment), run, state=state) == "missed"
    skipped = [r for r in caplog.records if "건너뜁니다" in r.getMessage()]
    assert len(skipped) == 1
    assert "오늘(2026-10-08) 브리핑은 12:00 전까지 보내지 못해 건너뜁니다" in skipped[0].getMessage()
    assert other.last_brief_date() is None and len(client.posts) == 1


def test_weekdays_only_skips_the_weekend(tmp_path):
    store = StateStore(tmp_path / "state.json")
    client, run = FakeSlackClient(), FakeRun(statuses=())
    weekdays = config.load_brief_schedule({"BRIEF_TIME": "07:00", "BRIEF_DAYS": "weekdays"})
    assert tick(client, store, FakeClock(at(10, 7, 0)), run, state=slack_bot.BriefLoopState(), schedule=weekdays) == "day_off"
    assert client.posts == [] and store.last_brief_date() is None


def test_scheduled_agent_failure_still_posts_header_failure_line_and_credits(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_TOKEN)
    store = StateStore(tmp_path / "state.json")
    client = FakeSlackClient()
    run = FakeRun(RuntimeError(f"agent exploded {SLACK_TOKEN}"), statuses=())
    with caplog.at_level(logging.INFO, logger="mungchi.slack"):
        assert tick(client, store, FakeClock(at(8, 7, 0)), run, state=slack_bot.BriefLoopState()) == "failed"
    [post] = client.posts
    header, failure, credit_part = post["text"].split("\n\n")
    assert header == "☀️ *오늘의 브리핑 (10/08 목)*"
    assert failure == "⚠️ 오늘 브리핑을 만들지 못했어요 (RuntimeError). 실행 로그를 확인해 주세요."
    assert credit_part.startswith("💳 *Chat KHU 크레딧*: 9,050.5 남음")
    assert "아침 브리핑 실패 (2026-10-08)" in caplog.text
    assert SLACK_TOKEN not in caplog.text and SLACK_TOKEN not in post["text"]
    assert store.last_brief_date() == "2026-10-08"  # not retried every 30 seconds


def test_scheduled_briefing_with_a_credit_failure_adds_the_note(tmp_path):
    from mungchi import credits

    store = StateStore(tmp_path / "state.json")
    client = FakeSlackClient()
    failing = lambda: credits.CreditReport(error="크레딧을 확인하지 못했습니다 (HTTP 500).")  # noqa: E731
    run = FakeRun(TurnResult(text="*① 오늘의 일정*\n• 일정 없음", session_id=SESSION_1), statuses=())
    assert tick(client, store, FakeClock(at(8, 7, 0)), run, state=slack_bot.BriefLoopState(), credit_fetch=failing) == "sent"
    [post] = client.posts
    assert post["text"].endswith("• 일정 없음\n\n💳 *Chat KHU 크레딧*: ⚠️ 확인하지 못했어요 (HTTP 500)")


def test_unwritable_state_file_still_sends_only_once_per_process(tmp_path, caplog):
    class ReadOnlyStore(StateStore):
        def mark_brief_date(self, day):
            raise OSError("read-only file system")

    store = ReadOnlyStore(tmp_path / "state.json")
    client, run = FakeSlackClient(), FakeRun(statuses=())
    state = slack_bot.BriefLoopState()
    with caplog.at_level(logging.WARNING, logger="mungchi.slack"):
        assert tick(client, store, FakeClock(at(8, 7, 0)), run, state=state) == "sent"
        assert tick(client, store, FakeClock(at(8, 7, 1)), run, state=state) == "already"
    assert len(client.posts) == 1
    assert "last_brief_date)를 기록하지 못했습니다" in caplog.text


def test_brief_loop_checks_every_30_seconds_and_survives_errors():
    sleeps, ticks = [], []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 3:
            raise asyncio.CancelledError

    async def flaky_tick(client, destinations, *, schedule, state, env=None, **options):
        ticks.append((client, destinations, state))
        raise RuntimeError("clock exploded")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(slack_bot.morning_brief_loop("client", [OWNER], schedule=MORNING, sleep=fake_sleep, tick=flaky_tick))
    assert sleeps == [30.0] * 4  # checked right away, then every 30 seconds (wall clock read each time)
    assert len(ticks) == 4 and all(t[:2] == ("client", [OWNER]) for t in ticks)
    assert len({id(t[2]) for t in ticks}) == 1  # one state for the whole loop


def test_brief_loop_with_the_real_tick_posts_exactly_once_over_a_morning(tmp_path):
    store = StateStore(tmp_path / "state.json")
    client, run = FakeSlackClient(), FakeRun(statuses=())
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
                client,
                [OWNER],
                schedule=MORNING,
                sleep=advance,
                store=store,
                clock=clock,
                run=run,
                credit_fetch=lambda: low_report(9050.5),
            )
        )
    assert len(wakeups) > 600  # 06:58 → 12:30 in 30-second steps
    assert len(run.calls) == 1 and len(client.posts) == 1
    assert store.last_brief_date() == "2026-10-08"


def test_post_briefing_dms_every_allowed_user_from_one_run(tmp_path):
    client = DMSlackClient()
    sessions = ThreadSessions(tmp_path / "threads.json")
    run = FakeRun(TurnResult(text="*① 오늘의 일정*\n• 일정 없음", session_id=SESSION_1), statuses=())
    code = asyncio.run(
        post_briefing(client, [OWNER, "UOTHER1"], run=run, sessions=sessions, now=at(8, 7), credit_fetch=lambda: low_report(9050.5))
    )
    assert code == 0 and len(run.calls) == 1  # one agent run, however many people get it
    assert [p["channel"] for p in client.posts] == [OWNER, "UOTHER1"]
    assert client.posts[0]["text"] == client.posts[1]["text"]
    for post, dm_channel in zip(client.posts, ("DOWNER1", "DOTHER1")):
        assert sessions.get(dm_channel, post["_ts"], persona="mungchi") == SESSION_1


def test_post_briefing_keeps_going_when_one_destination_fails(tmp_path, caplog):
    class HalfBroken(FakeSlackClient):
        async def chat_postMessage(self, **kwargs):
            if kwargs["channel"] == OWNER:
                exc = RuntimeError("boom")
                exc.response = {"error": "channel_not_found"}
                raise exc
            return await super().chat_postMessage(**kwargs)

    client = HalfBroken()
    with caplog.at_level(logging.ERROR, logger="mungchi.slack"):
        code = asyncio.run(post_briefing(client, [OWNER, "UOTHER1"], run=FakeRun(statuses=()), now=at(8, 7), credit_fetch=lambda: low_report(9050.5)))
    assert code == 1
    assert [p["channel"] for p in client.posts] == ["UOTHER1"]
    assert f"브리핑을 Slack({OWNER})에 올리지 못했습니다: channel_not_found" in caplog.text


def test_brief_slack_without_a_channel_dms_the_allowed_users(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_TOKEN)
    monkeypatch.setenv("SLACK_ALLOWED_USER_IDS", f"{OWNER},UOTHER1")
    client = DMSlackClient()
    monkeypatch.setattr(slack_bot, "AsyncWebClient", lambda token: client)
    monkeypatch.setattr(slack_bot, "run_turn", FakeRun(TurnResult(text="본문", session_id=SESSION_1), statuses=()))
    monkeypatch.setattr(slack_bot, "setup_logging", lambda: None)
    assert main(["--brief", "--slack"]) == 0
    assert sorted(p["channel"] for p in client.posts) == sorted([OWNER, "UOTHER1"])
    assert "Slack에 오늘 브리핑을 올렸습니다 (DM (허용된 사용자 2명))." in capsys.readouterr().err
    # A manual test briefing never stops the scheduled one.
    assert StateStore(config.get_state_path()).last_brief_date() is None


# -- run_bots: where the scheduled briefing goes, and the startup line


def _run_bots_with_fake_scheduler(monkeypatch, tmp_path, env):
    _no_network(monkeypatch)
    FakeSocket.instances = []
    built = _patch_build_apps(monkeypatch, tmp_path)
    started = []

    async def fake_scheduler(client, destinations, **kwargs):
        started.append({"client": client, "destinations": list(destinations), **kwargs})
        await asyncio.Event().wait()

    async def no_alert(client, users):
        return None

    async def let_it_start():
        await asyncio.sleep(0)

    cfg = config.load_slack_config(env)
    code = asyncio.run(
        slack_bot.run_bots(
            cfg, socket_factory=FakeSocket, wait=let_it_start, credit_alert=no_alert, brief_scheduler=fake_scheduler
        )
    )
    assert code == 0
    return built, started


def test_run_bots_sends_the_morning_briefing_as_dms_by_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIEF_TIME", "07:00")
    built, started = _run_bots_with_fake_scheduler(monkeypatch, tmp_path, ALL_BOTS_ENV)
    mungchi_app, mungchi_handler = next((app, h) for bot, app, h in built if bot.persona == "mungchi")
    [call] = started
    assert call["client"] is mungchi_app.client  # 고뭉치's bot posts it
    assert call["destinations"] == [OWNER]  # SLACK_BRIEF_CHANNEL unset: a DM to each allowed user
    assert call["schedule"].describe() == "매일 07:00 (Asia/Seoul)"
    assert call["sessions"] is mungchi_handler.sessions and call["semaphore"] is mungchi_handler._semaphore
    err = capsys.readouterr().err
    assert "아침 브리핑: 매일 07:00 (Asia/Seoul) → DM (허용된 사용자 1명)" in err
    assert "12:00 전까지 보냅니다" in err


def test_run_bots_sends_the_morning_briefing_to_the_brief_channel(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIEF_TIME", "6:45")
    monkeypatch.setenv("BRIEF_DAYS", "weekdays")
    _built, started = _run_bots_with_fake_scheduler(monkeypatch, tmp_path, {**ALL_BOTS_ENV, "SLACK_BRIEF_CHANNEL": CHANNEL})
    assert started[0]["destinations"] == [CHANNEL]
    assert "아침 브리핑: 평일 06:45 (Asia/Seoul) → 채널 C0123ABCD" in capsys.readouterr().err


def test_run_bots_skips_the_morning_briefing_without_moongchi(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("BRIEF_TIME", "07:00")
    env = {k: v for k, v in ALL_BOTS_ENV.items() if not k.startswith(("SLACK_BOT", "SLACK_APP"))}
    with caplog.at_level(logging.WARNING, logger="mungchi.slack"):
        _built, started = _run_bots_with_fake_scheduler(monkeypatch, tmp_path, env)
    assert started == []
    assert "고뭉치 봇(SLACK_BOT_TOKEN, SLACK_APP_TOKEN)이 켜져 있지 않아 아침 브리핑을 보내지 않습니다" in caplog.text


def test_run_bots_without_or_with_a_bad_brief_time_runs_no_scheduler(tmp_path, monkeypatch, capsys, caplog):
    _built, started = _run_bots_with_fake_scheduler(monkeypatch, tmp_path, ALL_BOTS_ENV)
    assert started == []
    assert "아침 브리핑: 꺼짐 (BRIEF_TIME 미설정)" in capsys.readouterr().err

    monkeypatch.setenv("BRIEF_TIME", "7시")
    with caplog.at_level(logging.WARNING, logger="mungchi.slack"):
        _built, started = _run_bots_with_fake_scheduler(monkeypatch, tmp_path, ALL_BOTS_ENV)
    assert started == []
    assert "아침 브리핑: 꺼짐 (BRIEF_TIME 값이 잘못됨)" in capsys.readouterr().err
    assert "BRIEF_TIME 값 '7시'은(는) 쓸 수 없어 아침 브리핑을 끕니다" in caplog.text


def test_run_bots_turns_off_a_briefing_to_a_channel_name(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("BRIEF_TIME", "07:00")
    with caplog.at_level(logging.WARNING, logger="mungchi.slack"):
        _built, started = _run_bots_with_fake_scheduler(monkeypatch, tmp_path, {**ALL_BOTS_ENV, "SLACK_BRIEF_CHANNEL": "#general"})
    assert started == []
    assert "아침 브리핑을 보낼 수 없어 끕니다" in caplog.text and "채널 ID" in caplog.text


# ---------------------------------------------------------------- weather shortcut (no LLM)


class FakeWeatherText:
    """Stands in for ``weather.slack_weather_text``: counts calls, returns a canned line."""

    TEXT = "🌤️ *서울 날씨*: 대체로 맑음 · 최저 12° / 최고 23° · 강수확률 10% · 미세먼지 보통"

    def __init__(self, result=None):
        self.result = result if result is not None else self.TEXT
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.mark.parametrize("persona", ["mungchi", "update", "schedule"])
@pytest.mark.parametrize(
    "event,source",
    [
        (mention(f"<@{BOT}> 날씨"), "mention"),
        (mention(f"<@{BOT}>  오늘 서울 날씨 어때?"), "mention"),
        (dm("날씨 알려줘"), "dm"),
        (dm("오늘 날씨"), "dm"),
    ],
)
def test_weather_shortcut_answers_without_an_agent_turn(tmp_path, persona, event, source):
    fake, credit = FakeWeatherText(), FakeCredits()
    handler, client, run = make_handler(tmp_path, persona=persona, weather_text=fake, credit_text=credit)
    asyncio.run(handler.handle_event(event, event_id="Ev1", source=source))
    assert run.calls == []  # run_turn is never called: no LLM
    assert fake.calls == 1 and credit.calls == 0
    [post] = client.posts
    assert post["text"] == FakeWeatherText.TEXT
    assert post["thread_ts"] == event["ts"] and post["channel"] == event["channel"]
    assert client.updates == []  # no placeholder to edit
    # No session / thread-map entry for a shortcut reply.
    assert not (tmp_path / "threads.json").exists()
    assert handler.sessions.threads() == {}


def test_weather_shortcut_in_a_thread_replies_there_and_keeps_the_threads_session(tmp_path):
    root = "1700000000.000100"
    handler, client, run = make_handler(tmp_path, weather_text=FakeWeatherText())
    handler.sessions.set(CHANNEL, root, SESSION_1, persona="mungchi")
    asyncio.run(handler.handle_event(mention(f"<@{BOT}> 날씨?", ts="1700000000.000500", thread_ts=root), event_id="E", source="mention"))
    assert run.calls == []
    assert client.posts[0]["thread_ts"] == root
    assert handler.sessions.threads() == {f"mungchi:{CHANNEL}:{root}": SESSION_1}


@pytest.mark.parametrize("persona", ["mungchi", "update", "schedule"])
def test_weather_shortcut_still_refuses_strangers(tmp_path, persona):
    fake = FakeWeatherText()
    handler, client, run = make_handler(tmp_path, persona=persona, weather_text=fake)

    async def scenario():
        await handler.handle_event(mention(f"<@{BOT}> 날씨", user=STRANGER), event_id="Ev1", source="mention")
        await handler.handle_event(dm("오늘 날씨 어때?", user=STRANGER), event_id="Ev2", source="dm")

    asyncio.run(scenario())
    assert fake.calls == 0 and run.calls == []
    assert [p["text"] for p in client.posts] == [REFUSAL_TEXT, REFUSAL_TEXT]


@pytest.mark.parametrize("text", ["내일 비 오면 일정 바꿔야 할까?", "내일 날씨 어때?", "날씨 좋으면 산책 갈 시간 있어?"])
def test_longer_weather_questions_go_to_the_agent(tmp_path, text):
    fake = FakeWeatherText()
    handler, client, run = make_handler(tmp_path, weather_text=fake)
    asyncio.run(handler.handle_event(mention(f"<@{BOT}> {text}"), event_id="Ev1", source="mention"))
    assert fake.calls == 0
    assert [c["prompt"] for c in run.calls] == [text]


def test_weather_shortcut_failure_is_a_short_korean_note_without_secrets(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_TOKEN)
    fake = FakeWeatherText(RuntimeError(f"boom {SLACK_TOKEN}"))
    handler, client, run = make_handler(tmp_path, weather_text=fake)
    with caplog.at_level(logging.ERROR, logger="mungchi.slack"):
        asyncio.run(handler.handle_event(mention(f"<@{BOT}> 날씨"), event_id="Ev1", source="mention"))
    assert run.calls == []
    assert [p["text"] for p in client.posts] == [slack_bot.WEATHER_CRASH_TEXT.format(kind="RuntimeError")]
    assert slack_bot.WEATHER_CRASH_TEXT.format(kind="RuntimeError") == "⚠️ 날씨를 가져오지 못했어요 (RuntimeError). 잠시 후 다시 시도해 주세요."
    assert SLACK_TOKEN not in caplog.text


def test_default_weather_text_calls_only_open_meteo(tmp_path, monkeypatch):
    import httpx

    from mungchi import weather

    hosts = []

    def handle(request):
        hosts.append(request.url.host)
        if request.url.host == "api.open-meteo.com":
            return httpx.Response(200, json={"daily": {"weather_code": [0], "temperature_2m_min": [9.6], "temperature_2m_max": [21.4], "precipitation_probability_max": [0]}})
        return httpx.Response(200, json={"current": {"pm10": 12, "pm2_5": 5}})

    real_client = httpx.Client
    monkeypatch.setattr(weather.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handle), timeout=kw.get("timeout")))
    handler, client, run = make_handler(tmp_path)  # default weather_text
    asyncio.run(handler.handle_event(dm("날씨"), event_id="Ev1", source="dm"))
    assert run.calls == []
    assert hosts == ["api.open-meteo.com", "air-quality-api.open-meteo.com"]
    assert [p["text"] for p in client.posts] == ["☀️ *서울 날씨*: 맑음 · 최저 10° / 최고 21° · 강수확률 0% · 미세먼지 좋음"]


def test_default_weather_text_offline_is_the_short_note(tmp_path):
    handler, client, run = make_handler(tmp_path)  # conftest: no network
    asyncio.run(handler.handle_event(dm("날씨"), event_id="Ev1", source="dm"))
    assert run.calls == []
    assert [p["text"] for p in client.posts] == ["🌤️ *서울 날씨*: 가져오지 못했어요"]


# ---------------------------------------------------------------- the weather line in --brief --slack and the scheduled briefing


def _sunny():
    from mungchi import weather

    return weather.WeatherReport(
        label="서울",
        forecast=weather.Forecast(code=1, low=11.5, high=22.6, rain_chance=10),
        air=weather.AirQuality(pm10=42.3, pm2_5=12.0),
    )


SLACK_WEATHER_LINE = "🌤️ *서울 날씨*: 대체로 맑음 · 최저 12° / 최고 23° · 강수확률 10% · 미세먼지 보통"


def test_post_briefing_puts_the_weather_right_under_the_header(tmp_path):
    client = FakeSlackClient()
    run = FakeRun(TurnResult(text="*① 오늘의 일정*\n• 일정 없음", session_id=SESSION_1), statuses=())
    code = asyncio.run(
        post_briefing(client, CHANNEL, run=run, now=at(8, 7), env={}, credit_fetch=lambda: low_report(9050.5), weather_fetch=_sunny)
    )
    assert code == 0
    [post] = client.posts
    assert post["text"].startswith(f"☀️ *오늘의 브리핑 (10/08 목)*\n{SLACK_WEATHER_LINE}\n\n*① 오늘의 일정*\n• 일정 없음\n\n💳 ")
    assert "날씨" not in run.calls[0]["prompt"] and "날씨" not in run.calls[0]["extra_system_prompt"]


def test_brief_slack_cli_adds_the_weather_by_default(tmp_path, monkeypatch, capsys):
    import httpx

    from mungchi import weather

    monkeypatch.delenv("BRIEF_WEATHER")  # conftest turns it off; the default is on
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_TOKEN)
    monkeypatch.setenv("SLACK_BRIEF_CHANNEL", CHANNEL)
    client = FakeSlackClient()
    monkeypatch.setattr(slack_bot, "AsyncWebClient", lambda token: client)
    monkeypatch.setattr(slack_bot, "run_turn", FakeRun(TurnResult(text="본문", session_id=SESSION_1), statuses=()))
    monkeypatch.setattr(slack_bot, "setup_logging", lambda: None)

    def handle(request):
        if request.url.host == "api.open-meteo.com":
            return httpx.Response(200, json={"daily": {"weather_code": [3], "temperature_2m_min": [8], "temperature_2m_max": [15], "precipitation_probability_max": [30]}})
        return httpx.Response(500)

    real_client = httpx.Client
    monkeypatch.setattr(weather.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handle), timeout=kw.get("timeout")))
    assert main(["--brief", "--slack"]) == 0
    [post] = client.posts
    header, line = post["text"].split("\n\n")[0].splitlines()
    assert header.startswith("☀️ *오늘의 브리핑 (")
    assert line == "☁️ *서울 날씨*: 흐림 · 최저 8° / 최고 15° · 강수확률 30%"  # the dust request failed: left out


def test_scheduled_briefing_has_the_weather_under_the_header_even_when_the_agent_fails(tmp_path):
    def scheduled(run, store):
        return asyncio.run(
            slack_bot.morning_brief_tick(
                client,
                [OWNER],
                schedule=MORNING,
                state=slack_bot.BriefLoopState(),
                env={"BRIEF_TIME": "07:00"},
                store=store,
                clock=FakeClock(at(8, 7, 0)),
                run=run,
                credit_fetch=lambda: low_report(9050.5),
                weather_fetch=_sunny,
            )
        )

    client = FakeSlackClient()
    ok = FakeRun(TurnResult(text="*① 오늘의 일정*\n• 일정 없음", session_id=SESSION_1), statuses=())
    assert scheduled(ok, StateStore(tmp_path / "a.json")) == "sent"
    assert client.posts[0]["text"].startswith(f"☀️ *오늘의 브리핑 (10/08 목)*\n{SLACK_WEATHER_LINE}\n\n*① 오늘의 일정*")

    assert scheduled(FakeRun(RuntimeError("agent exploded"), statuses=()), StateStore(tmp_path / "b.json")) == "failed"
    head, failure, credit_part = client.posts[1]["text"].split("\n\n")
    assert head == f"☀️ *오늘의 브리핑 (10/08 목)*\n{SLACK_WEATHER_LINE}"
    assert failure == "⚠️ 오늘 브리핑을 만들지 못했어요 (RuntimeError). 실행 로그를 확인해 주세요."
    assert credit_part.startswith("💳 *Chat KHU 크레딧*: 9,050.5 남음")


def test_scheduled_briefing_with_brief_weather_off_has_no_weather_line(tmp_path):
    client = FakeSlackClient()
    fetched = []
    code = asyncio.run(
        slack_bot.morning_brief_tick(
            client,
            [OWNER],
            schedule=MORNING,
            state=slack_bot.BriefLoopState(),
            env={"BRIEF_TIME": "07:00", "BRIEF_WEATHER": "off"},
            store=StateStore(tmp_path / "state.json"),
            clock=FakeClock(at(8, 7, 0)),
            run=FakeRun(TurnResult(text="*① 오늘의 일정*\n• 일정 없음", session_id=SESSION_1), statuses=()),
            credit_fetch=lambda: low_report(9050.5),
            weather_fetch=lambda: fetched.append(1) or _sunny(),
        )
    )
    assert code == "sent" and fetched == []
    assert client.posts[0]["text"].startswith("☀️ *오늘의 브리핑 (10/08 목)*\n\n*① 오늘의 일정*")
