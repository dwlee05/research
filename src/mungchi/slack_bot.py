"""Slack front end: the Socket Mode bots (``python -m mungchi slack``) and
briefing delivery (``python -m mungchi --brief --slack``).

One process runs up to three Slack apps, one per persona: 고뭉치 (@moongchi,
the orchestrator), 업뎃 (@update) and 일정 (@schedule), each with its own
``AsyncApp`` and Socket Mode connection. They share the allow-list, the
thread -> session map (keyed per persona) and one concurrency cap.

``SlackHandler`` holds all event logic for one bot and talks to Slack only
through an injected ``AsyncWebClient``-like object, so it can be tested
without Bolt.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
import traceback
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Mapping

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from . import config
from .main import TurnResult, briefing_prompt, run_turn
from .personas import MUNGCHI, PERSONA_LABELS, PERSONAS, SCHEDULE, SLACK_HANDLES, UPDATE, josa
from .slack_format import (
    PLACEHOLDER_TEXT,
    PLACEHOLDERS,
    SLACK_FORMAT_PROMPT,
    brief_header,
    chunk_text,
    strip_mention,
    to_mrkdwn,
)
from .state import ThreadSessions
from .tools.common import safe_error, scrub

log = logging.getLogger("mungchi.slack")

RunTurn = Callable[..., Awaitable[TurnResult]]

REFUSAL_TEXT = "죄송하지만 이 봇은 소유자만 사용할 수 있어요."
FRESH_SESSION_NOTE = "이 스레드의 이전 대화는 이어 갈 수 없어서, 다음 메시지부터는 새 대화로 시작해요."
BRIEF_CRASH_TEXT = "⚠️ 오늘 브리핑을 만들지 못했어요 ({kind}). 실행 로그를 확인해 주세요."

# What a bare mention (no text) asks for. 고뭉치's default is today's briefing.
EMPTY_MENTION_PROMPTS = {
    UPDATE: "공저자 업데이트 확인해줘",
    SCHEDULE: "오늘과 내일 일정 알려줘",
}

STATUS_INTERVAL_SECONDS = 1.0
MAX_STATUS_LINES = 6
# Room for the reason, an excerpt of the API's error text and a "→ ..." hint.
MAX_ERROR_CHARS = 600
MAX_REMEMBERED_EVENTS = 1_000


def slack_error_hints(persona: str = MUNGCHI) -> dict[str, str]:
    """Korean hints for common Slack API errors, naming this bot's env vars and handle."""
    bot_env, app_env = config.SLACK_BOT_ENV[persona]
    handle = SLACK_HANDLES[persona]
    if persona == MUNGCHI:
        channel_not_found = "채널을 찾을 수 없습니다. SLACK_BRIEF_CHANNEL의 채널 ID를 확인하고, 비공개 채널이면 봇을 먼저 초대하세요."
    else:
        channel_not_found = f"채널을 찾을 수 없습니다. 비공개 채널이면 /invite @{handle} 로 봇을 먼저 초대하세요."
    return {
        "invalid_auth": f"토큰이 올바르지 않습니다. {bot_env}과 {app_env}을 확인하세요.",
        "not_authed": f"토큰이 없습니다. {bot_env}을 확인하세요.",
        "account_inactive": "토큰이 더 이상 유효하지 않습니다. 앱을 다시 설치하고 새 토큰을 받으세요.",
        "token_revoked": "토큰이 취소되었습니다. 앱을 다시 설치하고 새 토큰을 받으세요.",
        "not_in_channel": f"봇이 채널에 없습니다. 채널에서 /invite @{handle} 로 초대하세요.",
        "channel_not_found": channel_not_found,
        "missing_scope": f"앱 권한이 부족합니다. slack_manifests/{handle}.yaml대로 권한을 주고 앱을 다시 설치하세요.",
        "is_archived": "보관된 채널에는 올릴 수 없습니다.",
    }


@dataclass(frozen=True)
class BotTexts:
    """User-facing Slack texts of one bot (Korean particles follow its name)."""

    persona: str
    label: str
    handle: str
    placeholder: str
    empty_answer: str
    failed: str
    crash: str  # format with kind=<exception class name>
    error_hints: Mapping[str, str]


def bot_texts(persona: str) -> BotTexts:
    label = PERSONA_LABELS[persona]
    subject, obj = josa(label, "이", "가"), josa(label, "을", "를")
    return BotTexts(
        persona=persona,
        label=label,
        handle=SLACK_HANDLES[persona],
        placeholder=PLACEHOLDERS[persona],
        empty_answer=f"{subject} 빈 답을 보냈어요. 다시 물어봐 주세요.",
        failed=f"⚠️ {subject} 답을 끝내지 못했어요.",
        crash=f"⚠️ {obj} 실행하지 못했어요 ({{kind}}). 잠시 후 다시 시도해 주세요.",
        error_hints=slack_error_hints(persona),
    )


BOT_TEXTS = {persona: bot_texts(persona) for persona in PERSONAS}

# 고뭉치's texts under their original names.
EMPTY_ANSWER_TEXT = BOT_TEXTS[MUNGCHI].empty_answer
FAILED_TEXT = BOT_TEXTS[MUNGCHI].failed
CRASH_TEXT = BOT_TEXTS[MUNGCHI].crash
SLACK_ERROR_HINTS = slack_error_hints(MUNGCHI)


# ---------------------------------------------------------------- helpers


class RecentKeys:
    """Bounded memory of recently seen keys (oldest forgotten first)."""

    def __init__(self, maxlen: int = MAX_REMEMBERED_EVENTS):
        self.maxlen = maxlen
        self._keys: OrderedDict[str, None] = OrderedDict()

    def __len__(self) -> int:
        return len(self._keys)

    def seen(self, *keys: str | None) -> bool:
        """True if any of ``keys`` was seen before. Records all of them."""
        keys_ = [key for key in keys if key]
        hit = any(key in self._keys for key in keys_)
        for key in keys_:
            self._keys[key] = None
            self._keys.move_to_end(key)
        while len(self._keys) > self.maxlen:
            self._keys.popitem(last=False)
        return hit


def compose_reply(result: TurnResult, persona: str = MUNGCHI) -> str:
    """Answer text plus a short Korean error note if the turn failed."""
    texts = BOT_TEXTS[persona]
    text = (result.text or "").strip()
    if result.failed:
        reason = scrub(result.error or "")[:MAX_ERROR_CHARS]
        note = f"⚠️ {reason}" if reason else texts.failed
        return f"{text}\n\n{note}" if text else note
    return text or texts.empty_answer


def to_slack_chunks(text: str) -> list[str]:
    """Final outgoing text: secrets scrubbed, mrkdwn safety net, Slack-sized chunks."""
    return chunk_text(to_mrkdwn(scrub(text)))


def remember_session(
    sessions: ThreadSessions, channel: str, thread_ts: str, session_id: str | None, *, persona: str
) -> None:
    """Store (or with ``session_id=None`` drop) a thread's session for ``persona``.

    A disk error never blocks a reply.
    """
    try:
        if session_id:
            sessions.set(channel, thread_ts, session_id, persona=persona)
        else:
            sessions.forget(channel, thread_ts, persona=persona)
    except OSError as exc:
        log.warning("스레드와 대화의 연결을 저장하지 못했습니다: %s", safe_error(exc))


def describe_slack_error(exc: BaseException, persona: str = MUNGCHI) -> str:
    code = ""
    response = getattr(exc, "response", None)
    with contextlib.suppress(Exception):
        code = str(response.get("error") or "") if response is not None else ""
    hint = BOT_TEXTS[persona].error_hints.get(code)
    if hint:
        return scrub(f"{code} — {hint}")
    return scrub(code) if code else safe_error(exc)


def _log_exception(message: str, exc: BaseException) -> None:
    details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log.error("%s\n%s", message, scrub(details))


class ScrubFilter(logging.Filter):
    """Removes secrets from every log record (message, args and traceback)."""

    def filter(self, record: logging.LogRecord) -> bool:
        with contextlib.suppress(Exception):
            record.msg = scrub(record.getMessage())
            record.args = None
        if record.exc_info:
            record.exc_text = scrub("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = scrub(record.stack_info)
        return True


def setup_logging() -> None:
    """Log to stderr through ``ScrubFilter`` (once per process)."""
    root = logging.getLogger()
    if any(isinstance(f, ScrubFilter) for h in root.handlers for f in h.filters):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(ScrubFilter())
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger("mungchi").setLevel(logging.INFO)


# ---------------------------------------------------------------- status


class StatusUpdater:
    """Shows status lines under the placeholder with ``chat_update``.

    Updates closer together than ``interval`` seconds are coalesced into one
    trailing update. Slack errors are logged and otherwise ignored.
    """

    def __init__(
        self,
        client: Any,
        channel: str,
        ts: str,
        *,
        interval: float = STATUS_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        placeholder: str = PLACEHOLDER_TEXT,
    ):
        self.client = client
        self.channel = channel
        self.ts = ts
        self.placeholder = placeholder
        self.interval = interval
        self.clock = clock
        self.lines: list[str] = []
        self._last: float | None = None
        self._pending: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._closed_event = asyncio.Event()

    def text(self) -> str:
        return "\n".join([self.placeholder, *self.lines[-MAX_STATUS_LINES:]])

    async def __call__(self, line: str) -> None:
        line = scrub(line or "").strip()
        if self._closed or not line or (self.lines and self.lines[-1] == line):
            return
        self.lines.append(line)
        if self._pending is not None:
            return  # the scheduled update will include this line
        wait = 0.0 if self._last is None else self._last + self.interval - self.clock()
        if wait <= 0:
            await self._push()
        else:
            self._pending = asyncio.create_task(self._push_later(wait))

    async def _push_later(self, wait: float) -> None:
        try:
            await asyncio.wait_for(self._closed_event.wait(), timeout=wait)
        except asyncio.TimeoutError:
            self._pending = None
            await self._push()

    async def _push(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._last = self.clock()
            try:
                await self.client.chat_update(channel=self.channel, ts=self.ts, text=self.text())
            except Exception as exc:  # noqa: BLE001 - progress display is best effort
                log.warning("진행 상황을 표시하지 못했습니다: %s", safe_error(exc))

    async def close(self) -> None:
        """Stop updating; waits for an in-flight update so it cannot overwrite the answer."""
        self._closed = True
        self._closed_event.set()
        task, self._pending = self._pending, None
        if task is not None:
            await task
        async with self._lock:
            pass


# ---------------------------------------------------------------- handler


class SlackHandler:
    """Turns one bot's Slack events into agent turns and posts the replies in threads.

    ``persona`` picks the agent behind the bot: 고뭉치 (``mungchi``) or 업뎃 /
    일정 answering directly (``update`` / ``schedule``). ``semaphore`` and
    ``our_bot_user_ids`` are shared between the bots of one process.
    """

    def __init__(
        self,
        client: Any,
        *,
        allowed_user_ids: Iterable[str],
        sessions: ThreadSessions,
        persona: str = MUNGCHI,
        run: RunTurn | None = None,
        bot_user_id: str | None = None,
        max_concurrent: int = config.DEFAULT_SLACK_MAX_CONCURRENT,
        semaphore: asyncio.Semaphore | None = None,
        our_bot_user_ids: set[str] | None = None,
        status_interval: float = STATUS_INTERVAL_SECONDS,
    ):
        self.allowed_user_ids = frozenset(allowed_user_ids)
        if not self.allowed_user_ids:
            # Never run without an allow-list: every bot reads private data.
            raise ValueError("allowed_user_ids must not be empty")
        if persona not in PERSONAS:
            raise ValueError(f"unknown persona: {persona!r}")
        self.persona = persona
        self.texts = BOT_TEXTS[persona]
        self.client = client
        self.sessions = sessions
        self.run = run
        self.bot_user_id = bot_user_id
        # User ids of all bots in this process; their messages are never answered.
        self.our_bot_user_ids = our_bot_user_ids if our_bot_user_ids is not None else set()
        self.status_interval = status_interval
        self._semaphore = semaphore or asyncio.Semaphore(max(1, max_concurrent))
        self._locks: dict[str, list[Any]] = {}
        self._seen = RecentKeys()
        self._refused = RecentKeys()

    # -- filtering

    def ignore_reason(self, event: Mapping[str, Any], source: str) -> str | None:
        """Why an event is skipped, or None if it should be handled."""
        if source == "dm" and event.get("channel_type") != "im":
            return "not_dm"
        if event.get("subtype"):
            return "subtype"
        if event.get("bot_id") or event.get("bot_profile"):
            return "bot"
        user = event.get("user")
        if not user:
            return "no_user"
        if self.bot_user_id and user == self.bot_user_id:
            return "self"
        if user in self.our_bot_user_ids:
            return "our_bot"
        if not event.get("channel") or not event.get("ts"):
            return "malformed"
        return None

    def is_allowed(self, user: str | None) -> bool:
        return bool(user) and user in self.allowed_user_ids

    def default_prompt(self) -> str:
        """What a bare mention asks for: today's briefing for 고뭉치, a fixed request otherwise."""
        return EMPTY_MENTION_PROMPTS.get(self.persona) or briefing_prompt()

    def build_prompt(self, text: str) -> str:
        """The user's text without this bot's mention; empty means ``default_prompt()``."""
        return strip_mention(text, self.bot_user_id) or self.default_prompt()

    # -- entry point

    async def handle_event(self, event: Mapping[str, Any], *, event_id: str | None = None, source: str) -> None:
        if self.ignore_reason(event, source):
            return
        channel, ts = str(event["channel"]), str(event["ts"])
        msg_id = event.get("client_msg_id")
        # The same message can arrive twice (Slack retries, or as both
        # app_mention and message.im), so dedupe on every id we have.
        if self._seen.seen(
            f"event:{event_id}" if event_id else None,
            f"msg:{msg_id}" if msg_id else None,
            f"ts:{channel}:{ts}",
        ):
            return
        thread_ts = str(event.get("thread_ts") or ts)
        user = str(event["user"])
        if not self.is_allowed(user):
            await self._refuse(channel, thread_ts, user)
            return
        await self._answer(channel, thread_ts, self.build_prompt(str(event.get("text") or "")))

    async def _refuse(self, channel: str, thread_ts: str, user: str) -> None:
        log.warning("%s 봇: 허용되지 않은 사용자(%s)의 요청을 거절했습니다.", self.texts.label, user)
        if self._refused.seen(f"{channel}:{thread_ts}"):
            return  # refuse once per thread
        await self._post(channel, thread_ts, REFUSAL_TEXT)

    # -- running a turn

    @contextlib.asynccontextmanager
    async def _thread_lock(self, key: str) -> AsyncIterator[None]:
        entry = self._locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._locks.pop(key, None)

    async def _answer(self, channel: str, thread_ts: str, prompt: str) -> None:
        placeholder = await self._post(channel, thread_ts, self.texts.placeholder)
        # Messages in one thread run in order per bot; all bots share the cost cap.
        async with self._thread_lock(f"{self.persona}:{channel}:{thread_ts}"):
            async with self._semaphore:
                await self._run_and_reply(channel, thread_ts, placeholder, prompt)

    async def _run_and_reply(self, channel: str, thread_ts: str, placeholder: str | None, prompt: str) -> None:
        persona, label = self.persona, self.texts.label
        resume = self.sessions.get(channel, thread_ts, persona=persona)
        # Only 고뭉치 shows progress ("→ 업뎃에게 맡기는 중..."); 업뎃 and 일정
        # keep their placeholder until the answer replaces it.
        updater = (
            StatusUpdater(
                self.client, channel, placeholder, interval=self.status_interval, placeholder=self.texts.placeholder
            )
            if placeholder and persona == MUNGCHI
            else None
        )
        log.info("%s: 스레드 %s:%s 처리 시작 (%s)", label, channel, thread_ts, "이어서" if resume else "새 대화")
        run = self.run or run_turn
        result: TurnResult | None = None
        crash: Exception | None = None
        try:
            result = await run(
                prompt, resume=resume, on_status=updater, extra_system_prompt=SLACK_FORMAT_PROMPT, persona=persona
            )
        except Exception as exc:  # noqa: BLE001 - reported in Slack without details
            crash = exc
        finally:
            if updater is not None:
                await updater.close()

        if result is None:
            _log_exception(f"{label} 실행 중 오류가 났습니다.", crash or RuntimeError("no result"))
            reply = self.texts.crash.format(kind=type(crash).__name__)
            if resume:
                # The stored session may be unusable (e.g. its transcript is
                # gone); start fresh next time instead of failing forever.
                remember_session(self.sessions, channel, thread_ts, None, persona=persona)
                reply += "\n" + FRESH_SESSION_NOTE
            await self._finish(channel, thread_ts, placeholder, [reply])
            return

        reply = compose_reply(result, persona)
        if result.session_id:
            remember_session(self.sessions, channel, thread_ts, result.session_id, persona=persona)
        elif resume and result.failed:
            remember_session(self.sessions, channel, thread_ts, None, persona=persona)
            reply += "\n" + FRESH_SESSION_NOTE
        if result.failed:
            log.warning("%s 답을 끝내지 못했습니다: %s", josa(label, "이", "가"), scrub(result.error or ""))
        await self._finish(channel, thread_ts, placeholder, to_slack_chunks(reply))
        log.info("%s: 스레드 %s:%s 답변 완료", label, channel, thread_ts)

    # -- Slack I/O (failures are logged, never raised)

    async def _finish(self, channel: str, thread_ts: str, placeholder: str | None, chunks: list[str]) -> None:
        chunks = chunks or [self.texts.empty_answer]
        first, rest = chunks[0], chunks[1:]
        if not (placeholder and await self._update(channel, placeholder, first)):
            await self._post(channel, thread_ts, first)
        for chunk in rest:
            await self._post(channel, thread_ts, chunk)

    async def _post(self, channel: str, thread_ts: str, text: str) -> str | None:
        try:
            response = await self.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=text, unfurl_links=False, unfurl_media=False
            )
        except Exception as exc:  # noqa: BLE001
            log.error("%s 봇: Slack 메시지를 보내지 못했습니다: %s", self.texts.label, describe_slack_error(exc, self.persona))
            return None
        return response.get("ts")

    async def _update(self, channel: str, ts: str, text: str) -> bool:
        try:
            await self.client.chat_update(channel=channel, ts=ts, text=text)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 봇: Slack 메시지를 고치지 못했습니다: %s", self.texts.label, describe_slack_error(exc, self.persona))
            return False
        return True


# ---------------------------------------------------------------- Bolt app


def register_listeners(app: Any, handler: SlackHandler) -> None:
    """Wire Bolt events to ``handler``. Bolt acks each event before the listener runs."""

    @app.event("app_mention")
    async def on_app_mention(event: dict[str, Any], body: dict[str, Any], context: Any) -> None:
        handler.bot_user_id = handler.bot_user_id or getattr(context, "bot_user_id", None)
        await handler.handle_event(event, event_id=body.get("event_id"), source="mention")

    @app.event("message")
    async def on_message(event: dict[str, Any], body: dict[str, Any], context: Any) -> None:
        handler.bot_user_id = handler.bot_user_id or getattr(context, "bot_user_id", None)
        await handler.handle_event(event, event_id=body.get("event_id"), source="dm")

    @app.error
    async def on_error(error: Exception) -> None:
        log.error("Slack 이벤트를 처리하지 못했습니다: %s", safe_error(error))


def build_app(
    cfg: config.SlackConfig,
    *,
    persona: str = MUNGCHI,
    sessions: ThreadSessions | None = None,
    run: RunTurn | None = None,
    semaphore: asyncio.Semaphore | None = None,
    our_bot_user_ids: set[str] | None = None,
) -> tuple[Any, SlackHandler]:
    """Create one bot's Bolt app and its handler. Does not contact Slack."""
    from slack_bolt.async_app import AsyncApp

    bot = cfg.bot(persona)
    app = AsyncApp(token=bot.bot_token, name="mungchi" if persona == MUNGCHI else f"mungchi.{persona}")
    handler = SlackHandler(
        app.client,
        persona=persona,
        allowed_user_ids=cfg.allowed_user_ids,
        sessions=sessions or ThreadSessions(config.get_slack_threads_path()),
        run=run,
        max_concurrent=cfg.max_concurrent,
        semaphore=semaphore,
        our_bot_user_ids=our_bot_user_ids,
    )
    register_listeners(app, handler)
    return app, handler


def build_apps(
    cfg: config.SlackConfig,
    *,
    sessions: ThreadSessions | None = None,
    run: RunTurn | None = None,
) -> list[tuple[config.SlackBotConfig, Any, SlackHandler]]:
    """One Bolt app per configured bot, sharing the cost cap, the thread map and the bot-id set."""
    semaphore = asyncio.Semaphore(max(1, cfg.max_concurrent))
    sessions = sessions or ThreadSessions(config.get_slack_threads_path())
    our_bot_user_ids: set[str] = set()
    apps = []
    for bot in cfg.configured_bots:
        app, handler = build_app(
            cfg,
            persona=bot.persona,
            sessions=sessions,
            run=run,
            semaphore=semaphore,
            our_bot_user_ids=our_bot_user_ids,
        )
        apps.append((bot, app, handler))
    return apps


def _describe_bot(bot: config.SlackBotConfig) -> str:
    return f"{bot.label}(@{bot.handle})"


async def _wait_forever() -> None:
    await asyncio.Event().wait()


async def run_bots(
    cfg: config.SlackConfig,
    *,
    socket_factory: Callable[[Any, str], Any] | None = None,
    wait: Callable[[], Awaitable[None]] = _wait_forever,
) -> int:
    """Connect every configured bot over Socket Mode and serve them in one event loop."""
    if socket_factory is None:
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

        socket_factory = AsyncSocketModeHandler
    bots = build_apps(cfg)
    sockets: list[Any] = []
    try:
        for bot, app, handler in bots:
            try:
                auth = await app.client.auth_test()
                handler.bot_user_id = auth.get("user_id")
                if handler.bot_user_id:
                    handler.our_bot_user_ids.add(handler.bot_user_id)
                socket = socket_factory(app, bot.app_token)
                sockets.append(socket)
                await socket.connect_async()
            except SlackApiError as exc:
                print(
                    f"[오류] {bot.label} 봇(@{bot.handle})을 Slack에 연결하지 못했습니다: "
                    f"{describe_slack_error(exc, bot.persona)}",
                    file=sys.stderr,
                )
                return 1
        started = ", ".join(_describe_bot(bot) for bot, _app, _handler in bots)
        print(
            f"Slack 봇 {len(bots)}개를 시작했습니다 (Socket Mode, 허용된 사용자 {len(cfg.allowed_user_ids)}명): "
            f"{started}. 멈추려면 Ctrl+C를 누르세요.",
            file=sys.stderr,
        )
        idle = [bot for bot in cfg.bots if not bot.configured]
        if idle:
            names = ", ".join(f"{_describe_bot(bot)}: {bot.bot_env}, {bot.app_env}" for bot in idle)
            print(f"토큰이 없어 켜지 않은 봇: {names}", file=sys.stderr)
        await wait()
    finally:
        for socket in sockets:
            with contextlib.suppress(Exception):
                await socket.close_async()
    return 0


# ---------------------------------------------------------------- briefing


async def post_briefing(
    client: Any,
    channel: str,
    *,
    run: RunTurn | None = None,
    sessions: ThreadSessions | None = None,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    on_status: Callable[[str], Any] | None = None,
) -> int:
    """Run today's briefing and post it to ``channel``.

    The first message carries the header line and the start of the briefing,
    so it reads without opening anything; longer briefings continue as
    replies in that message's thread. The thread is mapped to 고뭉치's
    briefing session, so mentioning @moongchi in it continues the conversation.
    """
    tz = config.get_timezone(env)
    now = (now or datetime.now(tz)).astimezone(tz)
    header = brief_header(now)
    run = run or run_turn
    try:
        result = await run(
            briefing_prompt(now, env), on_status=on_status, extra_system_prompt=SLACK_FORMAT_PROMPT, persona=MUNGCHI
        )
    except Exception as exc:  # noqa: BLE001
        _log_exception("브리핑을 만들지 못했습니다.", exc)
        await client.chat_postMessage(
            channel=channel,
            text=f"{header}\n\n" + BRIEF_CRASH_TEXT.format(kind=type(exc).__name__),
            unfurl_links=False,
            unfurl_media=False,
        )
        return 1

    chunks = to_slack_chunks(f"{header}\n\n{compose_reply(result)}")
    first = await client.chat_postMessage(channel=channel, text=chunks[0], unfurl_links=False, unfurl_media=False)
    root_channel = first.get("channel") or channel
    root_ts = first.get("ts")
    for chunk in chunks[1:]:
        await client.chat_postMessage(
            channel=root_channel, thread_ts=root_ts, text=chunk, unfurl_links=False, unfurl_media=False
        )
    if sessions is not None and result.session_id and root_ts:
        remember_session(sessions, root_channel, root_ts, result.session_id, persona=MUNGCHI)
    if result.failed:
        print(f"[오류] 브리핑이 완전하지 않습니다: {scrub(result.error or '')}", file=sys.stderr)
    return 1 if result.failed else 0


# ---------------------------------------------------------------- CLI glue


def _print_problems(title: str, problems: list[str]) -> None:
    lines = [f"[오류] {title}", *(f"- {problem}" for problem in problems)]
    lines.append(f".env에 값을 넣은 뒤 다시 실행하세요. {config.SLACK_README_HINT}")
    print("\n".join(lines), file=sys.stderr)


def _print_status(line: str) -> None:
    print(line, file=sys.stderr)


def run_bot_cli(env: Mapping[str, str] | None = None) -> int:
    cfg = config.load_slack_config(env)
    problems = config.slack_bot_problems(cfg)
    if problems:
        _print_problems("Slack 봇을 시작할 수 없습니다.", problems)
        return 1
    setup_logging()
    try:
        return asyncio.run(run_bots(cfg))
    except SlackApiError as exc:
        print(f"[오류] Slack에 연결하지 못했습니다: {describe_slack_error(exc)}", file=sys.stderr)
        return 1


def post_briefing_cli(env: Mapping[str, str] | None = None) -> int:
    cfg = config.load_slack_config(env)
    problems = config.slack_brief_problems(cfg)
    if problems:
        _print_problems("브리핑을 Slack에 올릴 수 없습니다.", problems)
        return 1
    setup_logging()

    async def deliver() -> int:
        client = AsyncWebClient(token=cfg.bot_token)
        sessions = ThreadSessions(config.get_slack_threads_path(env))
        return await post_briefing(client, cfg.brief_channel, sessions=sessions, env=env, on_status=_print_status)

    try:
        code = asyncio.run(deliver())
    except SlackApiError as exc:
        print(f"[오류] Slack에 브리핑을 올리지 못했습니다: {describe_slack_error(exc)}", file=sys.stderr)
        return 1
    if code == 0:
        print("Slack에 오늘 브리핑을 올렸습니다.", file=sys.stderr)
    return code
