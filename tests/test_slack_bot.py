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
from mungchi.briefing import DUE, brief_due, briefing_prompt
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
from mungchi.slack_format import PLACEHOLDER_TEXT, SLACK_FORMAT_PROMPT
from mungchi.state import StateStore, ThreadSessions
from mungchi.phrases import BOTH_LEADS, CREDIT_LEADS, PLACEHOLDER_POOLS, WEATHER_LEADS

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

    async def chat_postEphemeral(self, **kwargs):
        self.calls.append(("ephemeral", dict(kwargs)))
        return {"ok": True}

    @property
    def ephemerals(self) -> list[dict]:
        return [kw for kind, kw in self.calls if kind == "ephemeral"]

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
        # The conversation key each run was bound to (where a calendar proposal would wait).
        self.keys: list[str | None] = []
        # The images each run was given (None: a text-only run, as before).
        self.images: list[list | None] = []
        self.active = 0
        self.max_active = 0

    async def __call__(
        self,
        prompt,
        *,
        resume=None,
        on_status=None,
        extra_system_prompt="",
        persona="mungchi",
        briefing=False,
        conversation_key=None,
        images=None,
    ):
        self.images.append(images)
        self.calls.append(
            {
                "prompt": prompt,
                "resume": resume,
                "extra_system_prompt": extra_system_prompt,
                "persona": persona,
                "briefing": briefing,
            }
        )
        self.keys.append(conversation_key)
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


def without_lead(text: str, leads, persona: str = "mungchi") -> str:
    """A shortcut reply's data lines: the first line must be one of ``persona``'s lead-ins."""
    lead, body = text.split("\n", 1)
    assert lead in leads[persona], lead
    return body


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
    assert first[1]["text"] in PLACEHOLDER_POOLS["mungchi"]  # one of 고뭉치's varied first replies
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
    status_updates = [u for u in client.updates if u["text"].startswith(first[1]["text"] + "\n")]
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
    handler, client, run = make_handler(tmp_path)
    asyncio.run(handler.handle_event(mention(f"<@{BOT}>"), event_id="Ev1", source="mention"))
    [call] = run.calls
    assert call["prompt"].startswith("업뎃과 '일정' 에이전트에게 일을 맡겨서 오늘(")
    # The code-driven briefing, like the morning one: a briefing run that adds weather and credits by code.
    assert call["briefing"] is True and "프로그램이 따로 붙이니" in call["prompt"]
    assert client.updates[-1]["text"].startswith("☀️ *오늘의 브리핑 (")
    assert "💳 *Chat KHU 크레딧*" in client.updates[-1]["text"]


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
    placeholder, answer = [p["text"] for p in client.posts]
    assert placeholder in PLACEHOLDER_POOLS["mungchi"] and answer == "답변입니다."


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
    assert set(listeners) == {"on_app_mention", "on_message", "on_calendar_action"}
    # Events are acked by Bolt; the button listener acks itself, first thing.
    assert listeners["on_app_mention"].auto_acknowledgement and listeners["on_message"].auto_acknowledgement
    assert not listeners["on_calendar_action"].auto_acknowledgement
    listeners.pop("on_calendar_action")
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
        ["app_mentions:read", "chat:write", "files:read", "im:history", "im:read", "im:write"]
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
    assert PLACEHOLDER_TEXT == "잠시만요, 금방 확인해 볼게요 🗂️" == PLACEHOLDER_POOLS["mungchi"][0]
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
    assert PLACEHOLDER_POOLS == {p: slack_bot.BOT_TEXTS[p].placeholders for p in ("mungchi", "update", "schedule")}
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
    assert client.posts[0]["text"] in slack_bot.BOT_TEXTS[persona].placeholders
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
        listeners = {listener.ack_function.__name__: listener for listener in app._async_listeners}
        assert set(listeners) == {"on_app_mention", "on_message", "on_calendar_action"}  # buttons on every bot
        assert listeners["on_app_mention"].auto_acknowledgement and listeners["on_message"].auto_acknowledgement
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
    assert without_lead(post["text"], CREDIT_LEADS, persona) == FakeCredits.TEXT  # the data lines are unchanged
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
    assert without_lead(client.posts[0]["text"], CREDIT_LEADS).startswith("💳 *Chat KHU 크레딧*: 75 남음 / 100 (75%)")
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
    assert without_lead(post["text"], WEATHER_LEADS, persona) == FakeWeatherText.TEXT  # the data line is unchanged
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


@pytest.mark.parametrize("persona", ["mungchi", "update", "schedule"])
@pytest.mark.parametrize(
    "text",
    ["날씨는?", "오늘의 날씨", "지금 날씨 어때?", "서울 날씨 좀 알려줘", "날씨 알려줄래?", "오늘 서울 날씨 어떄", "날씨 알려주세요!", "날씨 🙏", "날씨 :pray:"],
)
def test_broader_weather_phrasings_use_the_shortcut(tmp_path, persona, text):
    fake, credit = FakeWeatherText(), FakeCredits()
    handler, client, run = make_handler(tmp_path, persona=persona, weather_text=fake, credit_text=credit)

    async def scenario():
        await handler.handle_event(mention(f"<@{BOT}> {text}"), event_id="Ev1", source="mention")
        await handler.handle_event(dm(text), event_id="Ev2", source="dm")

    asyncio.run(scenario())
    assert run.calls == []  # never an agent turn, so never an LLM call
    assert fake.calls == 2 and credit.calls == 0
    assert [without_lead(p["text"], WEATHER_LEADS, persona) for p in client.posts] == [FakeWeatherText.TEXT] * 2
    assert handler.sessions.threads() == {}


@pytest.mark.parametrize("persona", ["mungchi", "update", "schedule"])
def test_broader_weather_phrasings_still_check_the_allow_list_first(tmp_path, persona, monkeypatch):
    monkeypatch.setattr(slack_bot.weather, "configured_label", lambda: pytest.fail("the allow-list comes first"))
    fake = FakeWeatherText()
    handler, client, run = make_handler(tmp_path, persona=persona, weather_text=fake)

    async def scenario():
        await handler.handle_event(mention(f"<@{BOT}> 서울 날씨 좀 알려줘", user=STRANGER), event_id="Ev1", source="mention")
        await handler.handle_event(dm("날씨 🙏", user=STRANGER), event_id="Ev2", source="dm")

    asyncio.run(scenario())
    assert fake.calls == 0 and run.calls == []
    assert [p["text"] for p in client.posts] == [REFUSAL_TEXT, REFUSAL_TEXT]


def test_the_configured_weather_label_works_in_the_shortcut(tmp_path, monkeypatch):
    fake = FakeWeatherText()
    handler, client, run = make_handler(tmp_path, weather_text=fake)
    asyncio.run(handler.handle_event(dm("부산 날씨 어때?"), event_id="Ev1", source="dm"))
    assert fake.calls == 0 and [c["prompt"] for c in run.calls] == ["부산 날씨 어때?"]  # 서울 is the default
    monkeypatch.setenv("WEATHER_LABEL", "부산")
    asyncio.run(handler.handle_event(dm("부산 날씨 어때?", ts="1700000000.000300"), event_id="Ev2", source="dm"))
    assert fake.calls == 1 and len(run.calls) == 1


@pytest.mark.parametrize("text", ["날씨 좋은 날 야외 미팅 잡아줘", "이번 주말 날씨에 맞춰 일정 정리해줘"])
def test_weather_questions_that_need_the_agent_still_go_there(tmp_path, text):
    fake, credit = FakeWeatherText(), FakeCredits()
    handler, client, run = make_handler(tmp_path, persona="schedule", weather_text=fake, credit_text=credit)
    asyncio.run(handler.handle_event(mention(f"<@{BOT}> {text}"), event_id="Ev1", source="mention"))
    assert fake.calls == 0 and credit.calls == 0
    assert [(c["prompt"], c["persona"]) for c in run.calls] == [(text, "schedule")]


def test_a_message_without_a_shortcut_logs_only_its_length_at_debug(tmp_path, caplog):
    handler, client, run = make_handler(tmp_path, weather_text=FakeWeatherText(), credit_text=FakeCredits())
    with caplog.at_level(logging.DEBUG, logger="mungchi.slack"):
        asyncio.run(handler.handle_event(mention(f"<@{BOT}> 날씨 좋은 날 야외 미팅 잡아줘"), event_id="Ev1", source="mention"))
        asyncio.run(handler.handle_event(dm("크레딧 아끼려면?"), event_id="Ev2", source="dm"))
    lines = [r for r in caplog.records if "바로 답변에 해당하지 않아" in r.getMessage()]
    assert [r.levelno for r in lines] == [logging.DEBUG, logging.DEBUG]
    assert lines[0].getMessage() == "고뭉치: 바로 답변에 해당하지 않아 에이전트에게 넘깁니다 (글자 수 17, 날씨 포함: 예, 크레딧 포함: 아니오)"
    assert lines[1].getMessage().endswith("(글자 수 9, 날씨 포함: 아니오, 크레딧 포함: 예)")
    for text in ("야외 미팅", "아끼려면"):
        assert text not in caplog.text  # never the message itself
    # A shortcut that matched logs no such line.
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="mungchi.slack"):
        asyncio.run(handler.handle_event(dm("날씨", ts="1700000000.000300"), event_id="Ev3", source="dm"))
    assert "바로 답변에 해당하지 않아" not in caplog.text


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
    assert [without_lead(p["text"], WEATHER_LEADS) for p in client.posts] == ["☀️ *서울 날씨*: 맑음 · 최저 10° / 최고 21° · 강수확률 0% · 미세먼지 좋음"]


def test_default_weather_text_offline_is_the_short_note(tmp_path):
    handler, client, run = make_handler(tmp_path)  # conftest: no network
    asyncio.run(handler.handle_event(dm("날씨"), event_id="Ev1", source="dm"))
    assert run.calls == []
    assert [p["text"] for p in client.posts] == ["🌤️ *서울 날씨*: 가져오지 못했어요"]  # a failure line gets no lead-in


# ---------------------------------------------------------------- "토큰" and the combined weather + credit shortcut (no LLM)

COMBINED_TEXT = f"{FakeWeatherText.TEXT}\n\n{FakeCredits.TEXT}"


@pytest.mark.parametrize("persona", ["mungchi", "update", "schedule"])
@pytest.mark.parametrize(
    "event,source",
    [
        (dm("뭉치야 날씨랑 토큰 좀 말해봐"), "dm"),  # what the user actually sent
        (mention(f"<@{BOT}> 날씨하고 크레딧"), "mention"),
        (mention(f"<@{BOT}> 오늘 날씨랑 남은 토큰 알려줘"), "mention"),
        (dm("토큰이랑 날씨 🙏"), "dm"),
    ],
)
def test_weather_and_credits_together_are_answered_by_code(tmp_path, persona, event, source):
    fake_weather, fake_credits = FakeWeatherText(), FakeCredits()
    handler, client, run = make_handler(tmp_path, persona=persona, weather_text=fake_weather, credit_text=fake_credits)
    asyncio.run(handler.handle_event(event, event_id="Ev1", source=source))
    assert run.calls == []  # run_turn is never called: no LLM
    assert fake_weather.calls == 1 and fake_credits.calls == 1
    [post] = client.posts
    # The weather line, a blank line, then the credit summary, in one threaded reply.
    assert without_lead(post["text"], BOTH_LEADS, persona) == COMBINED_TEXT
    assert without_lead(post["text"], BOTH_LEADS, persona).split("\n\n") == [FakeWeatherText.TEXT, FakeCredits.TEXT]
    assert post["thread_ts"] == event["ts"] and post["channel"] == event["channel"]
    assert client.updates == []  # no placeholder
    assert not (tmp_path / "threads.json").exists()
    assert handler.sessions.threads() == {}


def test_weather_and_credits_are_fetched_concurrently(tmp_path):
    import threading

    # Each fetch waits until the other one has started: run one after the other, both would time out.
    barrier = threading.Barrier(2, timeout=5)

    def weather_text():
        barrier.wait()
        return FakeWeatherText.TEXT

    def credit_text():
        barrier.wait()
        return FakeCredits.TEXT

    handler, client, run = make_handler(tmp_path, weather_text=weather_text, credit_text=credit_text)
    asyncio.run(handler.handle_event(dm("날씨랑 토큰"), event_id="Ev1", source="dm"))
    assert [without_lead(p["text"], BOTH_LEADS) for p in client.posts] == [COMBINED_TEXT]
    assert run.calls == []


@pytest.mark.parametrize("persona", ["mungchi", "update", "schedule"])
def test_the_combined_shortcut_still_refuses_strangers(tmp_path, persona, monkeypatch):
    monkeypatch.setattr(slack_bot.weather, "configured_label", lambda: pytest.fail("the allow-list comes first"))
    fake_weather, fake_credits = FakeWeatherText(), FakeCredits()
    handler, client, run = make_handler(tmp_path, persona=persona, weather_text=fake_weather, credit_text=fake_credits)

    async def scenario():
        await handler.handle_event(dm("뭉치야 날씨랑 토큰 좀 말해봐", user=STRANGER), event_id="Ev1", source="dm")
        await handler.handle_event(mention(f"<@{BOT}> 토큰 좀 알려줘", user=STRANGER), event_id="Ev2", source="mention")

    asyncio.run(scenario())
    assert fake_weather.calls == 0 and fake_credits.calls == 0 and run.calls == []
    assert [p["text"] for p in client.posts] == [REFUSAL_TEXT, REFUSAL_TEXT]


def test_one_failing_half_of_the_combined_reply_is_a_short_note(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_TOKEN)
    fake_weather, fake_credits = FakeWeatherText(), FakeCredits(RuntimeError(f"boom {SLACK_TOKEN}"))
    handler, client, run = make_handler(tmp_path, weather_text=fake_weather, credit_text=fake_credits)
    with caplog.at_level(logging.ERROR, logger="mungchi.slack"):
        asyncio.run(handler.handle_event(dm("날씨랑 토큰"), event_id="Ev1", source="dm"))
    assert run.calls == []
    assert [without_lead(p["text"], BOTH_LEADS) for p in client.posts] == [
        FakeWeatherText.TEXT + "\n\n" + slack_bot.CREDIT_CRASH_TEXT.format(kind="RuntimeError")
    ]
    assert SLACK_TOKEN not in caplog.text

    client.calls.clear()
    handler = make_handler(tmp_path, client=client, weather_text=FakeWeatherText(""), credit_text=FakeCredits())[0]
    asyncio.run(handler.handle_event(dm("날씨랑 토큰", ts="1700000000.000300"), event_id="Ev2", source="dm"))
    assert [without_lead(p["text"], BOTH_LEADS) for p in client.posts] == [
        slack_bot.WEATHER_CRASH_TEXT.format(kind="빈 응답") + "\n\n" + FakeCredits.TEXT
    ]


@pytest.mark.parametrize("persona", ["mungchi", "update", "schedule"])
@pytest.mark.parametrize(
    "text", ["토큰", "토큰 좀 알려줘", "남은 토큰", "토큰 얼마나 남았어?", "토큰 사용량", "잔여 토큰", "뭉치야 토큰"]
)
def test_token_means_the_credits(tmp_path, persona, text):
    fake_weather, fake_credits = FakeWeatherText(), FakeCredits()
    handler, client, run = make_handler(tmp_path, persona=persona, weather_text=fake_weather, credit_text=fake_credits)
    asyncio.run(handler.handle_event(dm(text), event_id="Ev1", source="dm"))
    assert run.calls == [] and fake_weather.calls == 0 and fake_credits.calls == 1
    assert [without_lead(p["text"], CREDIT_LEADS, persona) for p in client.posts] == [FakeCredits.TEXT]
    assert handler.sessions.threads() == {}


@pytest.mark.parametrize(
    "text",
    ["토큰 아끼려면 어떻게 해?", "토큰이 뭐야?", "날씨 좋은 날 야외 미팅 잡아줘", "이번 주 Dropbox 변경이랑 날씨", "내일 비 오면 일정 바꿔야 할까?"],
)
def test_questions_that_need_the_agent_still_go_there(tmp_path, text):
    fake_weather, fake_credits = FakeWeatherText(), FakeCredits()
    handler, client, run = make_handler(tmp_path, weather_text=fake_weather, credit_text=fake_credits)
    asyncio.run(handler.handle_event(dm(text), event_id="Ev1", source="dm"))
    assert fake_weather.calls == 0 and fake_credits.calls == 0
    assert [c["prompt"] for c in run.calls] == [text]


# ---------------------------------------------------------------- running-version record on startup


def test_run_bots_prints_and_records_the_code_version(tmp_path, monkeypatch, capsys):
    _no_network(monkeypatch)
    FakeSocket.instances = []
    _patch_build_apps(monkeypatch, tmp_path)
    store = StateStore(tmp_path / "state.json")
    store.mark_brief_date("2026-10-07")  # other keys are kept

    async def no_wait():
        return None

    cfg = config.load_slack_config(ALL_BOTS_ENV)
    code = asyncio.run(
        slack_bot.run_bots(
            cfg,
            socket_factory=FakeSocket,
            wait=no_wait,
            credit_alert=lambda *a: asyncio.sleep(0),
            code_version=lambda: "abc1234",
            store=store,
        )
    )
    assert code == 0
    assert "코드 버전: abc1234" in capsys.readouterr().err.splitlines()
    version, since = store.running()
    assert version == "abc1234" and since is not None and since.tzinfo is not None
    assert store.last_brief_date() == "2026-10-07"


def test_the_version_record_uses_git_and_never_stops_the_bots(tmp_path, monkeypatch, capsys, caplog):
    from mungchi import version

    store = StateStore(tmp_path / "state.json")
    git_calls = []

    def fake_git(args):
        git_calls.append(list(args))
        return "def5678\n" if "rev-parse" in args else " M src/mungchi/slack_bot.py\n"

    monkeypatch.setattr(version, "run_git", fake_git)  # the default lookup, on a fake git
    now = datetime(2026, 10, 7, 0, 30, tzinfo=timezone.utc)
    assert asyncio.run(slack_bot.record_code_version(store=store, now=now)) == "def5678-dirty"
    assert "코드 버전: def5678-dirty" in capsys.readouterr().err
    assert store.running() == ("def5678-dirty", now)
    assert [call[2:] for call in git_calls] == [
        ["rev-parse", "--short", "HEAD"],
        ["--no-optional-locks", "status", "--porcelain", "--untracked-files=no"],
    ]
    assert all(call[:2] == ["-C", str(version.detect_repo_dir())] for call in git_calls)

    # A broken version lookup or an unwritable state file only logs a warning.
    def broken():
        raise RuntimeError("git exploded")

    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a folder", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="mungchi.slack"):
        assert asyncio.run(slack_bot.record_code_version(broken, StateStore(blocked / "state.json"))) == "v0.1.0"
    assert "코드 버전: v0.1.0" in capsys.readouterr().err
    assert "코드 버전을 확인하지 못했습니다" in caplog.text
    assert "실행 중인 코드 버전을 상태 파일에 기록하지 못했습니다" in caplog.text


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
    # The weather never goes through the model: the prompt only says code adds it.
    assert "대체로 맑음" not in run.calls[0]["prompt"] and "미세먼지" not in run.calls[0]["prompt"]
    assert "날씨" not in run.calls[0]["extra_system_prompt"]


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


# ---------------------------------------------------------------- 고뭉치's briefing on request (the morning briefing's builder)

BRIEF_ANSWER = (
    "*① 오늘의 일정*\n• 15:00–16:00 랩 미팅\n\n"
    "*② Dropbox 업데이트*\n• 공저자 변경 없음 (Dropbox): 지난 브리핑(10/07 07:00) 이후 바뀐 파일이 없어요"
)
REQUEST_TIME = at(8, 13, 5)  # 10/08 13:05: the 07:00 briefing was skipped (bot started after 12:00)


class RecordingBuilder:
    """``build_briefing`` with a fixed clock, weather and credits; records how the handler called it."""

    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(dict(kwargs))
        return await slack_bot.build_briefing(
            **kwargs, now=REQUEST_TIME, env={}, credit_fetch=lambda: low_report(9050.5), weather_fetch=_sunny
        )


@pytest.mark.parametrize(
    "event,source",
    [
        (dm("오늘 건너뛴 브리핑 좀 해봐"), "dm"),  # what the user actually sent
        (mention(f"<@{BOT}> 브리핑 해줘"), "mention"),
        (mention(f"<@{BOT}> 뭉치야 아침 브리핑 보여줘"), "mention"),
        (mention(f"<@{BOT}>"), "mention"),  # a bare mention
        (dm(""), "dm"),  # an empty DM
    ],
)
def test_a_briefing_request_gets_the_full_code_driven_briefing(tmp_path, event, source):
    builder = RecordingBuilder()
    run = FakeRun(TurnResult(text=BRIEF_ANSWER, session_id=SESSION_1))
    handler, client, run = make_handler(tmp_path, run=run, briefing_builder=builder)
    store = StateStore(config.get_state_path())
    store.mark_brief_date("2026-10-07")  # yesterday's morning briefing; today's was skipped

    asyncio.run(handler.handle_event(event, event_id="Ev1", source=source))

    # The morning briefing's builder, in Slack format, bounded like the morning run.
    [built] = builder.calls
    assert built["slack"] is True and built["run_timeout"] == slack_bot.BRIEF_RUN_TIMEOUT_SECONDS
    # One agent run in briefing mode (the Dropbox checkpoint moves), told that code adds weather and credits.
    [call] = run.calls
    assert call["briefing"] is True and call["persona"] == "mungchi" and call["resume"] is None
    assert call["extra_system_prompt"] == SLACK_FORMAT_PROMPT
    assert call["prompt"] == briefing_prompt(REQUEST_TIME)
    # The usual placeholder in the thread, progress lines, then the briefing in its place.
    thread = event["ts"]
    placeholder = client.posts[0]
    assert placeholder["text"] in PLACEHOLDER_POOLS["mungchi"]
    assert placeholder["channel"] == event["channel"] and placeholder["thread_ts"] == thread
    assert any("→ 업뎃에게 맡기는 중..." in update["text"] for update in client.updates[:-1])
    assert len(client.posts) == 1 and client.updates[-1]["ts"] == placeholder["_ts"]
    text = client.updates[-1]["text"]
    # Header, weather line, ① ②, credits: each exactly once.
    assert text.startswith(f"☀️ *오늘의 브리핑 (10/08 목)*\n{SLACK_WEATHER_LINE}\n\n{BRIEF_ANSWER}\n\n💳 *Chat KHU 크레딧*: 9,050.5 남음")
    assert text.count("서울 날씨") == 1 and text.count("💳") == 1 and text.count("오늘의 브리핑") == 1
    # last_brief_date is not written: the next scheduled briefing still goes out.
    assert store.last_brief_date() == "2026-10-07"
    assert brief_due(MORNING, at(9, 7, 0), store.last_brief_date()) == DUE
    # The thread is mapped to the briefing's session, like --brief --slack.
    assert handler.sessions.get(event["channel"], thread, persona="mungchi") == SESSION_1


def test_a_reply_in_the_briefing_thread_continues_the_briefing_session(tmp_path):
    run = FakeRun(TurnResult(text=BRIEF_ANSWER, session_id=SESSION_1), TurnResult(text="랩 미팅은 3층이에요.", session_id=SESSION_1))
    handler, client, run = make_handler(tmp_path, run=run, briefing_builder=RecordingBuilder())
    root = "1700000000.000200"

    async def scenario():
        await handler.handle_event(dm("브리핑", ts=root), event_id="Ev1", source="dm")
        await handler.handle_event(dm("랩 미팅 어디서 해?", ts="1700000000.000300", thread_ts=root), event_id="Ev2", source="dm")

    asyncio.run(scenario())
    # The follow-up is an ordinary turn (no briefing mode) that resumes the briefing's session.
    assert [(c["briefing"], c["resume"]) for c in run.calls] == [(True, None), (False, SESSION_1)]
    assert run.calls[1]["prompt"] == "랩 미팅 어디서 해?"
    assert {p["thread_ts"] for p in client.posts} == {root}


def test_briefing_requests_from_strangers_are_refused(tmp_path):
    builder = RecordingBuilder()
    handler, client, run = make_handler(tmp_path, briefing_builder=builder)

    async def scenario():
        await handler.handle_event(dm("오늘 건너뛴 브리핑 좀 해봐", user=STRANGER), event_id="Ev1", source="dm")
        await handler.handle_event(mention(f"<@{BOT}>", user=STRANGER), event_id="Ev2", source="mention")
        await handler.handle_event(mention(f"<@{BOT}> 브리핑", user=STRANGER, ts="1.000001"), event_id="Ev3", source="mention")

    asyncio.run(scenario())
    assert builder.calls == [] and run.calls == []
    assert [p["text"] for p in client.posts] == [REFUSAL_TEXT] * 3
    assert client.updates == [] and handler.sessions.threads() == {}


@pytest.mark.parametrize("text", ["브리핑 형식 바꿔줘", "브리핑에 날씨 빼줘", "어제 브리핑에서 말한 파일 뭐였지?"])
def test_other_messages_about_briefings_go_to_the_agent(tmp_path, text):
    builder = RecordingBuilder()
    handler, client, run = make_handler(tmp_path, briefing_builder=builder)
    asyncio.run(handler.handle_event(dm(text), event_id="Ev1", source="dm"))
    assert builder.calls == []
    assert [(c["prompt"], c["briefing"]) for c in run.calls] == [(text, False)]


@pytest.mark.parametrize("persona", ["update", "schedule"])
def test_direct_bots_never_build_the_briefing(tmp_path, persona):
    builder = RecordingBuilder()
    handler, client, run = make_handler(tmp_path, persona=persona, briefing_builder=builder)

    async def scenario():
        await handler.handle_event(dm("브리핑"), event_id="Ev1", source="dm")
        await handler.handle_event(dm("오늘 건너뛴 브리핑 좀 해봐", ts="1700000000.000300"), event_id="Ev2", source="dm")
        await handler.handle_event(mention(f"<@{BOT}>"), event_id="Ev3", source="mention")

    asyncio.run(scenario())
    assert builder.calls == []
    assert [(c["prompt"], c["persona"], c["briefing"]) for c in run.calls] == [
        ("브리핑", persona, False),
        ("오늘 건너뛴 브리핑 좀 해봐", persona, False),
        (slack_bot.EMPTY_MENTION_PROMPTS[persona], persona, False),
    ]


def test_a_failed_briefing_on_request_still_answers_in_the_thread(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_TOKEN)
    run = FakeRun(RuntimeError(f"boom {SLACK_TOKEN}"))
    handler, client, run = make_handler(tmp_path, run=run, briefing_builder=RecordingBuilder())
    with caplog.at_level(logging.ERROR, logger="mungchi.slack"):
        asyncio.run(handler.handle_event(dm("브리핑"), event_id="Ev1", source="dm"))
    # Like the morning briefing: header, weather, a short failure line and the credits still go out.
    head, failure, credit_part = client.updates[-1]["text"].split("\n\n")
    assert head == f"☀️ *오늘의 브리핑 (10/08 목)*\n{SLACK_WEATHER_LINE}"
    assert failure == "⚠️ 오늘 브리핑을 만들지 못했어요 (RuntimeError). 실행 로그를 확인해 주세요."
    assert credit_part.startswith("💳 *Chat KHU 크레딧*: 9,050.5 남음")
    assert handler.sessions.threads() == {}
    assert "브리핑을 만들지 못했습니다" in caplog.text and SLACK_TOKEN not in caplog.text

    async def broken(**kwargs):
        raise OSError(f"disk {SLACK_TOKEN}")

    handler.briefing_builder = broken
    with caplog.at_level(logging.ERROR, logger="mungchi.slack"):
        asyncio.run(handler.handle_event(dm("브리핑", ts="1700000000.000300"), event_id="Ev2", source="dm"))
    assert client.updates[-1]["text"] == "⚠️ 오늘 브리핑을 만들지 못했어요 (OSError). 실행 로그를 확인해 주세요."
    assert SLACK_TOKEN not in caplog.text


def test_the_default_briefing_on_request_is_the_shared_builder(tmp_path, monkeypatch):
    monkeypatch.delenv("BRIEF_WEATHER")  # conftest turns it off; the default is on (offline here: the short note)
    run = FakeRun(TurnResult(text=BRIEF_ANSWER, session_id=SESSION_1), statuses=())
    handler, client, run = make_handler(tmp_path, run=run)  # no builder given: briefing.build_briefing
    asyncio.run(handler.handle_event(dm("오늘 건너뛴 브리핑 좀 해봐"), event_id="Ev1", source="dm"))
    text = client.updates[-1]["text"]
    assert text.startswith("☀️ *오늘의 브리핑 (")
    assert f"\n🌤️ *서울 날씨*: 가져오지 못했어요\n\n{BRIEF_ANSWER}\n\n" in text
    assert text.endswith("💳 *Chat KHU 크레딧*: 확인 안 함 (Chat KHU 게이트웨이를 쓰지 않아요)")
    assert run.calls[0]["briefing"] is True
    assert StateStore(config.get_state_path()).last_brief_date() is None


# ---------------------------------------------------------------- calendar proposals: "네" / "아니요" by code

from datetime import timedelta  # noqa: E402

from mungchi.state import utcnow  # noqa: E402
from mungchi.tools.event_proposals import create_proposal_events, normalize_events, slack_conversation_key  # noqa: E402

ROOT = "1700000000.000200"  # the DM (or channel message) the proposal was shown under
# Far in the future, so the real clock never makes them past; the year is shown since it is not this one.
NOTE_EVENTS = [
    {"title": "신임교수모임 (10월)", "date": "2099-10-22", "start_time": "12:00", "notes": "발표: 홍길동 교수님"},
    {"title": "신임교수모임 (11월)", "date": "2099-11-19", "start_time": "12:00"},
]


class CalendarWrites:
    """The Calendar app adapter as far as creating events goes."""

    def __init__(self, fail_titles=(), delay=0.0):
        self.fail_titles = set(fail_titles)
        self.delay = delay
        self.created: list[str] = []
        self.calendars: list[str | None] = []  # calendar_name per created event

    def authorization_status(self):
        return "granted"

    def create_event(self, title, start, end, all_day, location=None, notes=None, calendar_name=None):
        if self.delay:
            import time as _time

            _time.sleep(self.delay)
        self.created.append(title)
        self.calendars.append(calendar_name)
        if title in self.fail_titles:
            return {"ok": False, "id": None, "calendar": "", "error": "읽기 전용 캘린더예요"}
        return {"ok": True, "id": f"EV-{len(self.created)}", "calendar": calendar_name or "연구", "error": None}


def store_proposal(store, persona, channel, thread_ts, events=NOTE_EVENTS, now=None):
    items, problems = normalize_events(events, tz=ZoneInfo("Asia/Seoul"), now=datetime(2099, 1, 1, tzinfo=timezone.utc))
    assert problems == []
    key = slack_conversation_key(persona, channel, thread_ts)
    proposal = {"id": "p1", "calendar": "연구", "calendar_label": "연구", "events": [e.to_state() for e in items]}
    store.save_pending_proposal(key, proposal, now or utcnow())
    return key


def proposal_handler(tmp_path, persona="schedule", app=None, run=None, **kwargs):
    app = app or CalendarWrites()
    store = StateStore(tmp_path / "state.json")

    def create(proposal):
        return create_proposal_events(
            proposal, env={"TIMEZONE": "Asia/Seoul"}, adapter_factory=lambda tz: app, platform="darwin"
        )

    handler, client, run = make_handler(
        tmp_path, run=run, persona=persona, proposals=store, create_events=create, **kwargs
    )
    return handler, client, run, app, store


@pytest.mark.parametrize("persona", ["update", "schedule", "mungchi"])
def test_yes_in_the_thread_creates_the_events_by_code_without_an_agent_turn(tmp_path, persona):
    handler, client, run, app, store = proposal_handler(tmp_path, persona)
    key = store_proposal(store, persona, DM, ROOT)
    asyncio.run(handler.handle_event(dm("네", ts="1700000000.000300", thread_ts=ROOT), event_id="Ev1", source="dm"))
    assert run.calls == []  # never the model
    assert app.created == ["신임교수모임 (10월)", "신임교수모임 (11월)"]
    [post] = client.posts
    assert post["thread_ts"] == ROOT and post["channel"] == DM
    assert post["text"] == (
        "✅ 캘린더에 추가했어요\n"
        "• 2099/10/22(목) 12:00–13:00 신임교수모임 (10월) · 캘린더: 연구\n"
        "• 2099/11/19(목) 12:00–13:00 신임교수모임 (11월) · 캘린더: 연구"
    )
    assert store.pending_proposal(key, utcnow()) is None  # cleared
    # A second "네" has nothing left to confirm: it is an ordinary message for the agent.
    asyncio.run(handler.handle_event(dm("네", ts="1700000000.000400", thread_ts=ROOT), event_id="Ev2", source="dm"))
    assert len(app.created) == 2 and [c["prompt"] for c in run.calls] == ["네"]


@pytest.mark.parametrize("text", ["<@UBOT> 추가해 줘", "<@UBOT> ㅇㅇ", "<@UBOT> OK", "<@UBOT> :+1:", "<@UBOT> 넵!"])
def test_yes_by_mention_in_a_channel_thread(tmp_path, text):
    handler, client, run, app, store = proposal_handler(tmp_path)
    store_proposal(store, "schedule", CHANNEL, ROOT)
    asyncio.run(handler.handle_event(mention(text, ts="1700000000.000500", thread_ts=ROOT), event_id="Ev1", source="mention"))
    assert run.calls == [] and len(app.created) == 2
    assert client.posts[-1]["text"].startswith("✅ 캘린더에 추가했어요") and client.posts[-1]["thread_ts"] == ROOT


@pytest.mark.parametrize("text", ["아니요", "취소", "no"])
def test_no_cancels_without_creating_anything(tmp_path, text):
    handler, client, run, app, store = proposal_handler(tmp_path)
    key = store_proposal(store, "schedule", DM, ROOT)
    asyncio.run(handler.handle_event(dm(text, ts="1700000000.000300", thread_ts=ROOT), event_id="Ev1", source="dm"))
    assert [p["text"] for p in client.posts] == ["취소했어요"]
    assert app.created == [] and run.calls == [] and store.pending_proposal(key, utcnow()) is None


@pytest.mark.parametrize("text", ["시간은 1시로 바꿔줘", "네 근데 11월 것만", "이 날 다른 일정 있어?"])
def test_anything_else_goes_to_the_agent_and_replaces_the_proposal(tmp_path, text):
    handler, client, run, app, store = proposal_handler(tmp_path)
    key = store_proposal(store, "schedule", DM, ROOT)
    asyncio.run(handler.handle_event(dm(text, ts="1700000000.000300", thread_ts=ROOT), event_id="Ev1", source="dm"))
    assert [c["prompt"] for c in run.calls] == [text]
    assert run.calls[0]["resume"] is None and run.keys == [key]  # the agent may re-propose in this thread
    assert app.created == []
    # The old preview can no longer be confirmed: the agent re-proposes if needed.
    assert store.pending_proposal(key, utcnow()) is None


def test_every_agent_run_is_bound_to_its_own_thread_and_bot(tmp_path):
    handler, _client, run, _app, _store = proposal_handler(tmp_path, "update")
    asyncio.run(handler.handle_event(dm("메모 붙여 넣음", ts="1700000000.000700"), event_id="Ev1", source="dm"))
    asyncio.run(handler.handle_event(mention("<@UBOT> 메모", ts="1700000000.000800"), event_id="Ev2", source="mention"))
    assert run.keys == [
        slack_conversation_key("update", DM, "1700000000.000700"),
        slack_conversation_key("update", CHANNEL, "1700000000.000800"),
    ]


def test_a_proposal_never_leaks_to_another_thread_or_bot(tmp_path):
    handler, client, run, app, store = proposal_handler(tmp_path, "schedule")
    key = store_proposal(store, "schedule", DM, ROOT)
    # Same bot, another thread.
    asyncio.run(handler.handle_event(dm("네", ts="1700000000.000900"), event_id="Ev1", source="dm"))
    # Another bot, same thread (its own handler, same state file).
    update, update_client, update_run, _app, _store = proposal_handler(tmp_path, "update", app=app)
    asyncio.run(update.handle_event(dm("네", ts="1700000000.000950", thread_ts=ROOT), event_id="Ev2", source="dm"))
    assert app.created == []
    assert [c["prompt"] for c in run.calls] == ["네"] and [c["prompt"] for c in update_run.calls] == ["네"]
    assert store.pending_proposal(key, utcnow()) is not None  # still waiting in its own thread


def test_an_expired_proposal_is_ignored(tmp_path):
    handler, client, run, app, store = proposal_handler(tmp_path)
    key = store_proposal(store, "schedule", DM, ROOT, now=utcnow() - timedelta(hours=25))
    asyncio.run(handler.handle_event(dm("네", ts="1700000000.000300", thread_ts=ROOT), event_id="Ev1", source="dm"))
    assert app.created == [] and [c["prompt"] for c in run.calls] == ["네"]
    assert store.pending_proposal(key, utcnow()) is None


def test_the_allow_list_comes_before_any_proposal(tmp_path):
    handler, client, run, app, store = proposal_handler(tmp_path)
    key = store_proposal(store, "schedule", DM, ROOT)
    asyncio.run(handler.handle_event(dm("네", user=STRANGER, ts="1700000000.000300", thread_ts=ROOT), event_id="Ev1", source="dm"))
    assert [p["text"] for p in client.posts] == [REFUSAL_TEXT]
    assert app.created == [] and run.calls == [] and store.pending_proposal(key, utcnow()) is not None


def test_partial_failures_and_items_without_a_time_are_reported_per_event(tmp_path):
    app = CalendarWrites(fail_titles={"신임교수모임 (11월)"})
    handler, client, run, app, store = proposal_handler(tmp_path, app=app)
    events = [*NOTE_EVENTS, {"title": "세미나", "date": "2099-11-26", "start_time": None}]
    store_proposal(store, "schedule", DM, ROOT, events=events)
    asyncio.run(handler.handle_event(dm("네", ts="1700000000.000300", thread_ts=ROOT), event_id="Ev1", source="dm"))
    assert app.created == ["신임교수모임 (10월)", "신임교수모임 (11월)"]  # never the one without a time
    assert client.posts[-1]["text"].splitlines() == [
        "✅ 캘린더에 추가했어요",
        "• 2099/10/22(목) 12:00–13:00 신임교수모임 (10월) · 캘린더: 연구",
        "❌ 2099/11/19(목) 12:00–13:00 신임교수모임 (11월) 추가 실패: 읽기 전용 캘린더예요",
        "⏭️ 2099/11/26(목) 시간 미정 세미나: 시작 시각이 없어 추가하지 않았어요. 시각을 알려 주시면 다시 제안할게요.",
    ]
    assert run.calls == []


def test_a_crash_while_creating_is_a_short_note(tmp_path, caplog):
    handler, client, run, app, store = proposal_handler(tmp_path)
    store_proposal(store, "schedule", DM, ROOT)

    def broken(proposal):
        raise RuntimeError(f"EventKit down {SLACK_TOKEN}")

    handler.create_events = broken
    with caplog.at_level(logging.ERROR, logger="mungchi.slack"):
        asyncio.run(handler.handle_event(dm("네", ts="1700000000.000300", thread_ts=ROOT), event_id="Ev1", source="dm"))
    assert client.posts[-1]["text"] == "❌ 캘린더에 추가하지 못했어요: 오류가 났어요 (RuntimeError: EventKit down ***)"
    assert SLACK_TOKEN not in client.posts[-1]["text"] and run.calls == []


class ProposingRun(FakeRun):
    """Like run_turn whose agent calls propose_calendar_events: stores a proposal under the run's key, slowly."""

    def __init__(self, store, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.store = store

    async def __call__(self, prompt, **kwargs):
        key = kwargs.get("conversation_key")
        if key:
            items, _ = normalize_events(NOTE_EVENTS[:1], tz=ZoneInfo("Asia/Seoul"), now=datetime(2099, 1, 1, tzinfo=timezone.utc))
            self.store.save_pending_proposal(key, {"id": prompt, "calendar": None, "events": [e.to_state() for e in items]}, utcnow())
        return await super().__call__(prompt, **kwargs)


def test_a_yes_sent_while_the_turn_is_still_running_confirms_nothing(tmp_path):
    store = StateStore(tmp_path / "state.json")
    run = ProposingRun(store, statuses=(), delay=0.05)
    handler, client, run, app, store = proposal_handler(tmp_path, run=run)

    async def scenario():
        first = asyncio.create_task(handler.handle_event(dm("메모: 10월 22일(목) 오후 12시"), event_id="Ev1", source="dm"))
        await asyncio.sleep(0.01)  # the agent is working; the preview is not shown yet
        await handler.handle_event(dm("네", ts="1700000000.000300", thread_ts=ROOT), event_id="Ev2", source="dm")
        await first

    asyncio.run(scenario())
    assert app.created == []
    assert [c["prompt"] for c in run.calls] == ["메모: 10월 22일(목) 오후 12시", "네"]
    # After the preview is shown, "네" confirms the proposal the agent made then.
    asyncio.run(handler.handle_event(dm("네", ts="1700000000.000400", thread_ts=ROOT), event_id="Ev3", source="dm"))
    assert app.created == ["신임교수모임 (10월)"] and len(run.calls) == 2


def test_two_quick_yeses_create_the_events_once(tmp_path):
    app = CalendarWrites(delay=0.05)
    handler, client, run, app, store = proposal_handler(tmp_path, app=app)
    store_proposal(store, "schedule", DM, ROOT)

    async def scenario():
        await asyncio.gather(
            handler.handle_event(dm("네", ts="1700000000.000300", thread_ts=ROOT), event_id="Ev1", source="dm"),
            handler.handle_event(dm("응", ts="1700000000.000301", thread_ts=ROOT), event_id="Ev2", source="dm"),
        )

    asyncio.run(scenario())
    assert app.created == ["신임교수모임 (10월)", "신임교수모임 (11월)"]
    assert [p["text"].splitlines()[0] for p in client.posts] == ["✅ 캘린더에 추가했어요"]
    assert run.calls == []


def test_code_only_shortcuts_keep_the_proposal_but_a_briefing_replaces_it(tmp_path):
    handler, client, run, app, store = proposal_handler(
        tmp_path, "mungchi", weather_text=lambda: "🌤️ 맑음", briefing_builder=RecordingBuilder()
    )
    key = store_proposal(store, "mungchi", DM, ROOT)
    asyncio.run(handler.handle_event(dm("날씨", ts="1700000000.000300", thread_ts=ROOT), event_id="Ev1", source="dm"))
    assert store.pending_proposal(key, utcnow()) is not None
    asyncio.run(handler.handle_event(dm("브리핑", ts="1700000000.000400", thread_ts=ROOT), event_id="Ev2", source="dm"))
    assert store.pending_proposal(key, utcnow()) is None and app.created == []


# ---------------------------------------------------------------- calendar categories: text answers and buttons

import json  # noqa: E402

CATEGORY_ROWS = [
    {"label": c.label, "calendar": c.calendar, "aliases": list(c.aliases)} for c in config.get_calendar_categories({})
]
LABELS = [row["label"] for row in CATEGORY_ROWS]


def store_category_proposal(store, persona, channel, thread_ts, *, suggested="Event-KHU", events=NOTE_EVENTS, pid="pid-1", now=None):
    items, problems = normalize_events(events, tz=ZoneInfo("Asia/Seoul"), now=datetime(2099, 1, 1, tzinfo=timezone.utc))
    assert problems == []
    key = slack_conversation_key(persona, channel, thread_ts)
    proposal = {
        "id": pid,
        "calendar": None,
        "calendar_label": "",
        "categories": CATEGORY_ROWS,
        "suggested_category": suggested,
        "events": [e.to_state() for e in items],
    }
    store.save_pending_proposal(key, proposal, now or utcnow())
    return key


def click(value, *, user=OWNER, channel=DM, message_ts="1700000100.000009", thread_ts=ROOT, action_id="mungchi_cal_pick_5"):
    """A block_actions payload as Slack sends it for a button in a thread."""
    return {
        "type": "block_actions",
        "user": {"id": user},
        "channel": {"id": channel},
        "container": {"type": "message", "message_ts": message_ts, "channel_id": channel, "thread_ts": thread_ts},
        "message": {"ts": message_ts, "thread_ts": thread_ts, "text": "카테고리를 골라주세요"},
        "actions": [{"action_id": action_id, "type": "button", "value": json.dumps(value)}],
    }


@pytest.mark.parametrize(
    "text,label",
    [("2", "Teaching"), ("<@UBOT> khu", "Event-KHU"), ("연구", "Research"), ("Research로 넣어줘", "Research"), ("네", "Event-KHU")],
)
def test_a_category_reply_creates_the_events_in_that_calendar_without_an_agent_turn(tmp_path, text, label):
    handler, client, run, app, store = proposal_handler(tmp_path)
    key = store_category_proposal(store, "schedule", DM, ROOT)
    asyncio.run(handler.handle_event(dm(text, ts="1700000000.000300", thread_ts=ROOT), event_id="Ev1", source="dm"))
    assert run.calls == []  # never the model
    assert app.created == ["신임교수모임 (10월)", "신임교수모임 (11월)"] and app.calendars == [label, label]
    [post] = client.posts
    assert post["thread_ts"] == ROOT and post["text"] == (
        f"✅ {label} 캘린더에 추가했어요\n"
        "• 2099/10/22(목) 12:00–13:00 신임교수모임 (10월)\n"
        "• 2099/11/19(목) 12:00–13:00 신임교수모임 (11월)"
    )
    assert store.pending_proposal(key, utcnow()) is None


def test_an_unclear_category_reply_is_asked_about_and_the_proposal_stays(tmp_path):
    handler, client, run, app, store = proposal_handler(tmp_path)
    key = store_category_proposal(store, "schedule", DM, ROOT, suggested=None)
    for i, text in enumerate(("event", "네", "9")):
        asyncio.run(handler.handle_event(dm(text, ts=f"1700000000.00030{i}", thread_ts=ROOT), event_id=f"Ev{i}", source="dm"))
    assert [p["text"].split(":")[0] for p in client.posts] == [
        "'event'에 맞는 카테고리가 여러 개예요",
        "추천한 카테고리가 없어요. 번호나 이름으로 골라 주세요",
        "1~5 가운데 번호로 골라 주세요",
    ]
    assert app.created == [] and run.calls == [] and store.pending_proposal(key, utcnow()) is not None
    asyncio.run(handler.handle_event(dm("4", ts="1700000000.000310", thread_ts=ROOT), event_id="Ev9", source="dm"))
    assert app.calendars == ["Event-Outside", "Event-Outside"]


def test_no_cancels_and_other_text_goes_to_the_agent_with_categories(tmp_path):
    handler, client, run, app, store = proposal_handler(tmp_path)
    key = store_category_proposal(store, "schedule", DM, ROOT)
    asyncio.run(handler.handle_event(dm("아니요", ts="1700000000.000300", thread_ts=ROOT), event_id="Ev1", source="dm"))
    assert [p["text"] for p in client.posts] == ["취소했어요"] and store.pending_proposal(key, utcnow()) is None
    store_category_proposal(store, "schedule", DM, ROOT)
    asyncio.run(handler.handle_event(dm("1번은 Research, 2번은 Event-KHU", ts="1700000000.000400", thread_ts=ROOT), event_id="Ev2", source="dm"))
    assert [c["prompt"] for c in run.calls] == ["1번은 Research, 2번은 Event-KHU"] and app.created == []
    assert store.pending_proposal(key, utcnow()) is None  # the agent re-proposes


def test_the_category_buttons_payload():
    proposal = {"id": "abc123", "categories": CATEGORY_ROWS, "suggested_category": "Event-KHU", "events": [{}]}
    text, blocks = slack_bot.category_blocks(proposal)
    assert text == "카테고리를 골라주세요 (추천: Event-KHU)"
    section, actions = blocks
    assert section == {"type": "section", "text": {"type": "mrkdwn", "text": text}}
    assert actions["type"] == "actions" and actions["block_id"] == "mungchi_cal_abc123"
    buttons = actions["elements"]
    assert [b["text"]["text"] for b in buttons] == [*LABELS, "취소"]
    assert [b["action_id"] for b in buttons] == [f"mungchi_cal_pick_{i}" for i in range(1, 6)] + ["mungchi_cal_cancel"]
    assert len({b["action_id"] for b in buttons}) == len(buttons)  # unique within the block, as Slack requires
    assert all(b["type"] == "button" and b["text"]["type"] == "plain_text" for b in buttons)
    assert [b.get("style") for b in buttons] == [None, None, None, None, "primary", None]  # only the suggestion
    assert [json.loads(b["value"]) for b in buttons] == [
        *({"proposal_id": "abc123", "category": label} for label in LABELS),
        {"proposal_id": "abc123", "cancel": True},
    ]
    assert slack_bot.CATEGORY_ACTION_RE.match(buttons[0]["action_id"]) and slack_bot.CATEGORY_ACTION_RE.match(buttons[-1]["action_id"])
    # No suggestion: no primary button; every event with its own category: one 추가 button.
    _, plain = slack_bot.category_blocks({**proposal, "suggested_category": None})
    assert all("style" not in b for b in plain[1]["elements"])
    assigned = {**proposal, "events": [{"category": "Research"}]}
    text, blocks = slack_bot.category_blocks(assigned)
    assert text == "일정마다 정한 카테고리로 추가할까요?"
    assert [(b["text"]["text"], b.get("style")) for b in blocks[1]["elements"]] == [("추가", "primary"), ("취소", None)]
    # The yes / no flow (no categories) and a proposal without an id get no buttons.
    assert slack_bot.category_blocks({"id": "x", "events": [{}]}) is None
    assert slack_bot.category_blocks({**proposal, "id": ""}) is None and slack_bot.category_blocks(None) is None


class CategoryProposingRun(FakeRun):
    """Like run_turn whose agent calls propose_calendar_events with categories on."""

    def __init__(self, store, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.store = store

    async def __call__(self, prompt, **kwargs):
        key = kwargs.get("conversation_key")
        if key:
            _p, _persona, channel, thread_ts = key.split(":", 3)
            store_category_proposal(self.store, _persona, channel, thread_ts, pid=f"pid-{len(self.calls) + 1}")
        return await super().__call__(prompt, **kwargs)


def _proposing_handler(tmp_path, persona="update", **kwargs):
    store = StateStore(tmp_path / "state.json")
    run = CategoryProposingRun(store, TurnResult(text="• 10/22(목) …\n카테고리를 골라주세요 …", session_id=SESSION_1), statuses=())
    return proposal_handler(tmp_path, persona, run=run, **kwargs)


def test_an_agent_proposal_gets_category_buttons_under_the_answer(tmp_path):
    handler, client, run, app, store = _proposing_handler(tmp_path)
    asyncio.run(handler.handle_event(dm("메모: 10월 22일(목) 오후 12시", ts=ROOT), event_id="Ev1", source="dm"))
    placeholder, buttons = client.posts
    assert client.updates[-1]["ts"] == placeholder["_ts"] and "blocks" not in client.updates[-1]  # the answer as before
    assert client.updates[-1]["text"].startswith("• 10/22(목)")
    assert buttons["thread_ts"] == ROOT and buttons["text"] == "카테고리를 골라주세요 (추천: Event-KHU)"
    assert buttons["blocks"] == slack_bot.category_blocks(store.pending_proposal(slack_conversation_key("update", DM, ROOT), utcnow()))[1]
    assert handler._button_messages[slack_conversation_key("update", DM, ROOT)] == (DM, buttons["_ts"], "pid-1")


def test_no_buttons_without_a_category_proposal(tmp_path):
    handler, client, run, app, store = proposal_handler(tmp_path)
    asyncio.run(handler.handle_event(dm("내일 일정은?", ts=ROOT), event_id="Ev1", source="dm"))
    assert all("blocks" not in p for p in client.posts) and len(client.posts) == 1
    # The yes / no flow (no categories) stays text-only too.
    old = ProposingRun(store, statuses=())
    handler.run = old
    asyncio.run(handler.handle_event(dm("메모", ts="1700000000.000900"), event_id="Ev2", source="dm"))
    assert all("blocks" not in p for p in client.posts)


def test_a_click_by_the_owner_creates_the_events_and_replaces_the_buttons(tmp_path):
    handler, client, run, app, store = _proposing_handler(tmp_path)
    asyncio.run(handler.handle_event(dm("메모", ts=ROOT), event_id="Ev1", source="dm"))
    buttons_ts = client.posts[-1]["_ts"]
    asyncio.run(handler.handle_action(click({"proposal_id": "pid-1", "category": "Research"}, message_ts=buttons_ts)))
    assert app.created == ["신임교수모임 (10월)", "신임교수모임 (11월)"] and app.calendars == ["Research", "Research"]
    update = client.updates[-1]
    assert update["ts"] == buttons_ts and update["channel"] == DM and update["blocks"] == []  # no buttons left
    assert update["text"].startswith("✅ Research 캘린더에 추가했어요\n• 2099/10/22(목) 12:00–13:00 신임교수모임 (10월)")
    assert client.ephemerals == [] and len(run.calls) == 1
    assert store.pending_proposal(slack_conversation_key("update", DM, ROOT), utcnow()) is None
    # Clicking again (the message was already replaced, or a stale copy): nothing more is created.
    asyncio.run(handler.handle_action(click({"proposal_id": "pid-1", "category": "Family"}, message_ts=buttons_ts)))
    assert len(app.created) == 2
    assert client.ephemerals[-1] == {"channel": DM, "user": OWNER, "thread_ts": ROOT, "text": "이미 처리됐거나 만료된 요청이에요"}


def test_the_cancel_button_cancels(tmp_path):
    handler, client, run, app, store = proposal_handler(tmp_path)
    key = store_category_proposal(store, "schedule", DM, ROOT)
    asyncio.run(handler.handle_action(click({"proposal_id": "pid-1", "cancel": True}, action_id="mungchi_cal_cancel")))
    assert app.created == [] and store.pending_proposal(key, utcnow()) is None
    assert client.updates[-1]["text"] == "취소했어요" and client.updates[-1]["blocks"] == []


def test_a_click_by_someone_else_is_refused_ephemerally_and_creates_nothing(tmp_path):
    handler, client, run, app, store = proposal_handler(tmp_path)
    key = store_category_proposal(store, "schedule", CHANNEL, ROOT)
    asyncio.run(handler.handle_action(click({"proposal_id": "pid-1", "category": "Family"}, user=STRANGER, channel=CHANNEL)))
    assert app.created == [] and client.updates == [] and client.posts == []
    assert client.ephemerals == [{"channel": CHANNEL, "user": STRANGER, "thread_ts": ROOT, "text": REFUSAL_TEXT}]
    assert store.pending_proposal(key, utcnow()) is not None  # still waiting for the owner


@pytest.mark.parametrize(
    "value",
    [
        {"proposal_id": "old-id", "category": "Family"},  # an earlier preview's buttons
        {"proposal_id": "pid-1", "category": "Lecture"},  # not offered
        {"proposal_id": "pid-1", "confirm": True},  # not every event has its own category
        {"category": "Family"},
        "not json",
    ],
)
def test_a_stale_or_unknown_click_does_nothing(tmp_path, value):
    handler, client, run, app, store = proposal_handler(tmp_path)
    key = store_category_proposal(store, "schedule", DM, ROOT)
    body = click(value)
    if value == "not json":
        body["actions"][0]["value"] = "{not json"
    asyncio.run(handler.handle_action(body))
    assert app.created == [] and client.updates == [] and run.calls == []
    assert [e["text"] for e in client.ephemerals] == [slack_bot.STALE_ACTION_TEXT]
    assert store.pending_proposal(key, utcnow())["id"] == "pid-1"


def test_an_expired_proposal_or_another_thread_is_stale(tmp_path):
    handler, client, run, app, store = proposal_handler(tmp_path)
    store_category_proposal(store, "schedule", DM, ROOT, now=utcnow() - timedelta(hours=25))
    asyncio.run(handler.handle_action(click({"proposal_id": "pid-1", "category": "Family"})))
    store_category_proposal(store, "schedule", DM, "1700000000.999999")
    asyncio.run(handler.handle_action(click({"proposal_id": "pid-1", "category": "Family"})))  # this thread has none
    assert app.created == [] and len(client.ephemerals) == 2


def test_a_double_click_creates_the_events_once(tmp_path):
    app = CalendarWrites(delay=0.05)
    handler, client, run, app, store = proposal_handler(tmp_path, app=app)
    store_category_proposal(store, "schedule", DM, ROOT)

    async def scenario():
        await asyncio.gather(
            handler.handle_action(click({"proposal_id": "pid-1", "category": "Family"})),
            handler.handle_action(click({"proposal_id": "pid-1", "category": "Research"})),
            handler.handle_event(dm("2", ts="1700000000.000300", thread_ts=ROOT), event_id="Ev1", source="dm"),
        )

    asyncio.run(scenario())
    assert app.created == ["신임교수모임 (10월)", "신임교수모임 (11월)"] and app.calendars == ["Family", "Family"]
    assert [u["text"].splitlines()[0] for u in client.updates] == ["✅ Family 캘린더에 추가했어요"]
    assert client.posts == [] and run.calls == []


def test_a_text_answer_closes_the_buttons(tmp_path):
    handler, client, run, app, store = _proposing_handler(tmp_path)
    asyncio.run(handler.handle_event(dm("메모", ts=ROOT), event_id="Ev1", source="dm"))
    buttons_ts = client.posts[-1]["_ts"]
    asyncio.run(handler.handle_event(dm("3", ts="1700000000.000300", thread_ts=ROOT), event_id="Ev2", source="dm"))
    assert app.calendars == ["Research", "Research"]
    assert client.posts[-1]["text"].startswith("✅ Research 캘린더에 추가했어요")
    closed = client.updates[-1]
    assert closed == {"channel": DM, "ts": buttons_ts, "text": slack_bot.BUTTONS_ANSWERED_TEXT, "blocks": []}
    assert handler._button_messages == {}


def test_a_new_message_closes_the_old_buttons_and_the_new_proposal_gets_new_ones(tmp_path):
    handler, client, run, app, store = _proposing_handler(tmp_path)
    asyncio.run(handler.handle_event(dm("메모", ts=ROOT), event_id="Ev1", source="dm"))
    first_buttons = client.posts[-1]["_ts"]
    asyncio.run(handler.handle_event(dm("시간은 1시로 바꿔줘", ts="1700000000.000300", thread_ts=ROOT), event_id="Ev2", source="dm"))
    assert {"channel": DM, "ts": first_buttons, "text": slack_bot.BUTTONS_REPLACED_TEXT, "blocks": []} in client.updates
    second_buttons = client.posts[-1]
    assert json.loads(second_buttons["blocks"][1]["elements"][0]["value"])["proposal_id"] == "pid-2"
    # The old buttons' proposal is gone: a click on them does nothing.
    asyncio.run(handler.handle_action(click({"proposal_id": "pid-1", "category": "Family"}, message_ts=first_buttons)))
    assert app.created == [] and client.ephemerals[-1]["text"] == slack_bot.STALE_ACTION_TEXT


def test_buttons_route_through_bolt_with_an_ack_first(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    cfg = config.load_slack_config({**ALL_BOTS_ENV})
    apps = slack_bot.build_apps(cfg, sessions=ThreadSessions(tmp_path / "t.json"))
    _bot, app, handler = apps[1]  # 업뎃
    store = StateStore(tmp_path / "state.json")
    created = CalendarWrites()
    handler.client, handler.proposals = FakeSlackClient(), store
    handler.create_events = lambda proposal: create_proposal_events(
        proposal, env={"TIMEZONE": "Asia/Seoul"}, adapter_factory=lambda tz: created, platform="darwin"
    )
    store_category_proposal(store, "update", DM, ROOT)
    [listener] = [x for x in app._async_listeners if x.ack_function.__name__ == "on_calendar_action"]

    from slack_bolt.request.async_request import AsyncBoltRequest
    from slack_bolt.response import BoltResponse

    body = click({"proposal_id": "pid-1", "category": "Teaching"})
    acks: list[str] = []

    async def ack():
        acks.append("ack" if not created.created else "late")

    async def scenario():
        request = AsyncBoltRequest(body=body, mode="socket_mode")
        assert all([await m.async_matches(request, BoltResponse(status=200)) for m in listener.matchers])
        other = AsyncBoltRequest(body={**body, "actions": [{"action_id": "someone_else", "value": "{}"}]}, mode="socket_mode")
        assert not all([await m.async_matches(other, BoltResponse(status=200)) for m in listener.matchers])
        await listener.ack_function(ack=ack, body=body)

    asyncio.run(scenario())
    assert acks == ["ack"] and created.calendars == ["Teaching", "Teaching"]


@pytest.mark.parametrize("name", sorted(MANIFESTS))
def test_slack_manifests_enable_interactivity_for_the_buttons(name):
    text = _manifest(name)
    assert re.search(r"^  interactivity:\n    is_enabled: true$", text, re.MULTILINE)
    assert "request_url" not in text  # Socket Mode: no public URL


# ---------------------------------------------------------------- photos -> calendar

import io  # noqa: E402

import httpx  # noqa: E402
from PIL import Image  # noqa: E402

from mungchi import images as image_prep  # noqa: E402


def jpeg_bytes(size=(2400, 1800), color="white") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return buffer.getvalue()


def slack_file(name="poster.jpg", mimetype="image/jpeg", size=50_000, **extra):
    file = {
        "id": f"F-{name}",
        "name": name,
        "mimetype": mimetype,
        "size": size,
        "url_private": f"https://files.slack.com/files-pri/T1-F1/{name}",
        "url_private_download": f"https://files.slack.com/files-pri/T1-F1/download/{name}",
    }
    return {**file, **extra}


class FakeFetcher:
    """Stands in for download_slack_file: records (url, token) and returns image bytes."""

    def __init__(self, data=None, error=None):
        self.data = data or jpeg_bytes()
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, url, token):
        self.calls.append((url, token))
        if self.error is not None:
            raise self.error
        return self.data


def photo_handler(tmp_path, persona="update", run=None, fetcher=None, **kwargs):
    fetcher = fetcher or FakeFetcher()
    handler, client, run, app, store = proposal_handler(
        tmp_path, persona, run=run, bot_token=SLACK_TOKEN, file_fetcher=fetcher, **kwargs
    )
    return handler, client, run, fetcher, store


def photo_dm(text="", files=None, **extra):
    return dm(text, subtype="file_share", files=files if files is not None else [slack_file()], **extra)


def test_file_share_messages_in_dms_and_mentions_with_files_are_handled(tmp_path):
    handler, client, run, fetcher, store = photo_handler(tmp_path)
    assert handler.ignore_reason(photo_dm(), "dm") is None
    assert handler.ignore_reason(mention("<@UBOT> 이거", files=[slack_file()]), "mention") is None
    assert handler.ignore_reason(mention("<@UBOT>", subtype="file_share", files=[slack_file()]), "mention") is None
    for subtype in ("message_changed", "message_deleted", "channel_join", "bot_message", "thread_broadcast"):
        assert handler.ignore_reason(dm(subtype=subtype, files=[slack_file()]), "dm") == "subtype"
    assert handler.ignore_reason({**photo_dm(), "channel_type": "channel"}, "dm") == "not_dm"


def test_a_photo_in_a_dm_goes_to_the_agent_with_the_images_then_buttons(tmp_path):
    store = StateStore(tmp_path / "state.json")
    run = CategoryProposingRun(store, TurnResult(text="• 10/22(목) 12:00–13:00 신임교수모임 (10월)\n카테고리를 골라주세요 …", session_id=SESSION_1), statuses=())
    handler, client, run, fetcher, store = photo_handler(tmp_path, run=run)
    asyncio.run(handler.handle_event(photo_dm(ts=ROOT, client_msg_id="m-photo"), event_id="Ev1", source="dm"))
    assert [c["prompt"] for c in run.calls] == [""]  # run_turn fills in "이 이미지에 있는 일정을 캘린더에 등록해줘"
    [images_given] = run.images
    [(media_type, data)] = images_given
    assert media_type == "image/jpeg" and Image.open(io.BytesIO(data)).size == (1568, 1176)  # shrunk before sending
    assert fetcher.calls == [("https://files.slack.com/files-pri/T1-F1/download/poster.jpg", SLACK_TOKEN)]
    assert run.keys == [slack_conversation_key("update", DM, ROOT)] and run.calls[0]["persona"] == "update"
    placeholder, buttons = client.posts
    assert placeholder["text"] in PLACEHOLDER_POOLS["update"] and client.updates[-1]["text"].startswith("• 10/22(목)")
    assert buttons["blocks"][1]["type"] == "actions"  # the same category buttons as for a pasted note
    # The same message delivered again (retry) is not downloaded twice.
    asyncio.run(handler.handle_event(photo_dm(ts=ROOT, client_msg_id="m-photo"), event_id="Ev1", source="dm"))
    assert len(fetcher.calls) == 1 and len(run.calls) == 1


def test_a_mention_with_a_photo_and_text_sends_both(tmp_path):
    handler, client, run, fetcher, store = photo_handler(tmp_path, persona="schedule")
    event = mention("<@UBOT> 이 포스터 일정 Research로", files=[slack_file("a.png", "image/png"), slack_file("b.webp", "image/webp")])
    asyncio.run(handler.handle_event(event, event_id="Ev1", source="mention"))
    assert [c["prompt"] for c in run.calls] == ["이 포스터 일정 Research로"]
    assert len(run.images[0]) == 2 and len(fetcher.calls) == 2


def test_strangers_photos_are_never_downloaded(tmp_path):
    handler, client, run, fetcher, store = photo_handler(tmp_path)
    asyncio.run(handler.handle_event(photo_dm(user=STRANGER), event_id="Ev1", source="dm"))
    asyncio.run(handler.handle_event(mention("<@UBOT>", user=STRANGER, files=[slack_file()], ts="1700000000.000777"), event_id="Ev2", source="mention"))
    assert fetcher.calls == [] and run.calls == []
    assert REFUSAL_TEXT in [p["text"] for p in client.posts]


def test_at_most_five_images_are_read_and_skipped_files_are_noted(tmp_path):
    handler, client, run, fetcher, store = photo_handler(tmp_path)
    files = [slack_file(f"{i}.jpg") for i in range(6)] + [
        slack_file("notice.pdf", "application/pdf"),
        slack_file("huge.png", "image/png", size=image_prep.MAX_FILE_BYTES + 1),
    ]
    asyncio.run(handler.handle_event(photo_dm(files=files), event_id="Ev1", source="dm"))
    assert [url.rsplit("/", 1)[1] for url, _token in fetcher.calls] == ["0.jpg", "1.jpg", "2.jpg", "3.jpg", "4.jpg"]
    assert len(run.images[0]) == 5
    note = client.posts[0]["text"]
    assert "사진은 한 번에 5장까지만 읽어요" in note and "20MB보다 큰 사진은 읽지 않았어요: huge.png" in note
    assert "읽지 않은 파일: notice.pdf" in note


def test_a_pdf_gets_the_images_only_note(tmp_path):
    handler, client, run, fetcher, store = photo_handler(tmp_path)
    asyncio.run(handler.handle_event(photo_dm(files=[slack_file("notice.pdf", "application/pdf")]), event_id="Ev1", source="dm"))
    assert fetcher.calls == [] and run.calls == []
    [note] = client.posts
    assert note["text"].startswith("지금은 사진(JPG·PNG·GIF·WebP·HEIC)만 읽을 수 있어요")
    # With text, the text still goes to the agent as before (without images).
    asyncio.run(handler.handle_event(photo_dm("이 공지 일정 알려줘", files=[slack_file("n.pdf", "application/pdf")], ts="1700000000.000300"), event_id="Ev2", source="dm"))
    assert [c["prompt"] for c in run.calls] == ["이 공지 일정 알려줘"] and run.images == [None]


def test_moongchi_points_photos_to_update_or_schedule(tmp_path):
    handler, client, run, fetcher, store = photo_handler(tmp_path, persona="mungchi", briefing_builder=RecordingBuilder())
    asyncio.run(handler.handle_event(photo_dm(), event_id="Ev1", source="dm"))
    asyncio.run(handler.handle_event(mention("<@UBOT> 이거 등록해줘", files=[slack_file()], ts="1700000000.000555"), event_id="Ev2", source="mention"))
    assert [p["text"] for p in client.posts] == ["사진 속 일정 등록은 @업뎃이나 @일정에게 보내주세요"] * 2
    assert run.calls == [] and fetcher.calls == [] and handler.briefing_builder.calls == []
    # A file that is not a photo and no text: a note, never the briefing.
    asyncio.run(handler.handle_event(photo_dm(files=[slack_file("a.pdf", "application/pdf")], ts="1700000000.000600"), event_id="Ev3", source="dm"))
    assert client.posts[-1]["text"].startswith("지금은 사진") and handler.briefing_builder.calls == []


def test_heic_without_pillow_heif_is_a_korean_note_without_an_agent_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(image_prep, "_heif_registered", False)
    heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64
    handler, client, run, fetcher, store = photo_handler(tmp_path, fetcher=FakeFetcher(data=heic))
    asyncio.run(handler.handle_event(photo_dm(files=[slack_file("IMG_0001.HEIC", "image/heic")]), event_id="Ev1", source="dm"))
    assert run.calls == []
    reply = client.updates[-1]["text"]
    assert reply.startswith("보낸 사진을 하나도 읽지 못했어요.") and "IMG_0001.HEIC: HEIC 사진은 아직 읽을 수 없어요" in reply


def test_one_failed_photo_is_noted_under_the_answer(tmp_path):
    class HalfBroken(FakeFetcher):
        async def __call__(self, url, token):
            if url.endswith("bad.jpg"):
                self.calls.append((url, token))
                return b"garbage"
            return await super().__call__(url, token)

    handler, client, run, fetcher, store = photo_handler(tmp_path, fetcher=HalfBroken())
    asyncio.run(handler.handle_event(photo_dm(files=[slack_file("good.jpg"), slack_file("bad.jpg")]), event_id="Ev1", source="dm"))
    assert len(run.images[0]) == 1
    assert client.updates[-1]["text"].endswith("⚠️ bad.jpg: 사진을 읽지 못했어요. JPG나 PNG로 다시 보내 주세요.")


def test_missing_files_read_scope_is_explained(tmp_path):
    fetcher = FakeFetcher(error=slack_bot.FileDownloadError("files:read 권한이 없어요", missing_scope=True))
    handler, client, run, fetcher, store = photo_handler(tmp_path, persona="schedule", fetcher=fetcher)
    asyncio.run(handler.handle_event(photo_dm(), event_id="Ev1", source="dm"))
    assert run.calls == []
    assert "봇에 files:read 권한이 없어 사진을 받지 못했어요. slack_manifests/schedule.yaml대로" in client.updates[-1]["text"]


def test_a_file_without_its_address_is_looked_up_with_files_info(tmp_path):
    class InfoClient(FakeSlackClient):
        async def files_info(self, **kwargs):
            self.calls.append(("files_info", dict(kwargs)))
            return {"ok": True, "file": slack_file("full.jpg")}

    handler, client, run, fetcher, store = photo_handler(tmp_path, client=InfoClient())
    partial = {"id": "F-full.jpg", "name": "full.jpg", "mimetype": "image/jpeg", "file_access": "check_file_info"}
    asyncio.run(handler.handle_event(photo_dm(files=[partial]), event_id="Ev1", source="dm"))
    assert ("files_info", {"file": "F-full.jpg"}) in client.calls
    assert fetcher.calls == [("https://files.slack.com/files-pri/T1-F1/download/full.jpg", SLACK_TOKEN)]


def test_build_app_gives_the_handler_its_own_bot_token(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    cfg = config.load_slack_config(ALL_BOTS_ENV)
    tokens = {bot.persona: bot.bot_token for bot in cfg.bots}
    for bot, _app, handler in slack_bot.build_apps(cfg, sessions=ThreadSessions(tmp_path / "t.json")):
        assert handler.bot_token == tokens[bot.persona] and handler.file_fetcher is slack_bot.download_slack_file


# -- the real downloader, on a fake transport


def _transport(handler):
    seen: list[httpx.Request] = []

    def handle(request):
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(handle), seen


def test_download_sends_the_bot_token_only_to_slack(tmp_path):
    transport, seen = _transport(lambda request: httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8data"))
    url = "https://files.slack.com/files-pri/T1-F1/download/poster.jpg"
    data = asyncio.run(slack_bot.download_slack_file(url, SLACK_TOKEN, transport=transport))
    assert data == b"\xff\xd8data"
    [request] = seen
    assert request.headers["authorization"] == f"Bearer {SLACK_TOKEN}" and str(request.url) == url
    for bad in ("http://files.slack.com/x.jpg", "https://evil.example.com/x.jpg", "https://slack.com.evil.io/x.jpg", "ftp://files.slack.com/x"):
        with pytest.raises(slack_bot.FileDownloadError, match="Slack 파일 주소가 아니에요"):
            asyncio.run(slack_bot.download_slack_file(bad, SLACK_TOKEN, transport=transport))
    assert len(seen) == 1  # never sent anywhere else


def test_download_failures_are_short_and_never_carry_the_token(caplog):
    url = "https://files.slack.com/files-pri/T1-F1/download/poster.jpg"
    login_page, _ = _transport(lambda r: httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=b"<html>"))
    with pytest.raises(slack_bot.FileDownloadError) as caught:
        asyncio.run(slack_bot.download_slack_file(url, SLACK_TOKEN, transport=login_page))
    assert caught.value.missing_scope and str(caught.value) == "files:read 권한이 없어요"
    missing, _ = _transport(lambda r: httpx.Response(404))
    with pytest.raises(slack_bot.FileDownloadError, match="HTTP 404"):
        asyncio.run(slack_bot.download_slack_file(url, SLACK_TOKEN, transport=missing))
    big, _ = _transport(lambda r: httpx.Response(200, headers={"content-type": "image/png"}, content=b"x" * 2048))
    with pytest.raises(slack_bot.FileDownloadError, match="20MB보다 큰 사진"):
        asyncio.run(slack_bot.download_slack_file(url, SLACK_TOKEN, transport=big, max_bytes=1024))

    def leaky(request):
        raise httpx.ConnectError(f"cannot connect with {request.headers['authorization']} to {request.url}", request=request)

    broken, _ = _transport(leaky)
    with pytest.raises(slack_bot.FileDownloadError) as caught:
        asyncio.run(slack_bot.download_slack_file(url, SLACK_TOKEN, transport=broken))
    assert str(caught.value) == "ConnectError"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__  # the httpx error (with the URL) is dropped
    assert SLACK_TOKEN not in str(caught.value) and url not in str(caught.value)


def test_a_failing_download_in_the_handler_never_logs_or_posts_the_token(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("SLACK_UPDATE_BOT_TOKEN", SLACK_TOKEN)

    async def leaky_fetch(url, token):
        raise RuntimeError(f"boom Authorization: Bearer {token} at {url}")

    handler, client, run, fetcher, store = photo_handler(tmp_path, fetcher=leaky_fetch)
    with caplog.at_level(logging.INFO, logger="mungchi.slack"):
        asyncio.run(handler.handle_event(photo_dm(), event_id="Ev1", source="dm"))
    texts = [c[1].get("text", "") for c in client.calls]
    assert run.calls == [] and any("poster.jpg: 사진을 받지 못했어요 (RuntimeError)" in t for t in texts)
    assert SLACK_TOKEN not in caplog.text and all(SLACK_TOKEN not in t for t in texts)
    assert "files-pri" not in caplog.text  # the address is not logged either


def test_a_redirect_to_another_host_never_gets_the_token():
    def handle(request):
        if request.url.host == "files.slack.com":
            return httpx.Response(302, headers={"location": "https://cdn.example.net/signed/poster.jpg"})
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8ok")

    transport, seen = _transport(handle)
    data = asyncio.run(slack_bot.download_slack_file("https://files.slack.com/files-pri/T1-F1/download/p.jpg", SLACK_TOKEN, transport=transport))
    assert data == b"\xff\xd8ok"
    first, second = seen
    assert first.headers["authorization"] == f"Bearer {SLACK_TOKEN}" and "authorization" not in second.headers
