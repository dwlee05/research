"""Slack front end: the Socket Mode bots (``python -m mungchi slack``), the
scheduled morning briefing they send (``BRIEF_TIME``) and briefing delivery
(``python -m mungchi --brief --slack``). Short credit and weather questions
are answered by code, without an agent turn.

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
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from . import briefing, config, credits, weather
from .briefing import BRIEF_CRASH_TEXT, build_briefing
from .main import TurnResult, briefing_prompt, run_turn
from .personas import MUNGCHI, PERSONA_LABELS, PERSONAS, SCHEDULE, SLACK_HANDLES, UPDATE, josa
from .slack_format import (
    PLACEHOLDER_TEXT,
    PLACEHOLDERS,
    SLACK_FORMAT_PROMPT,
    chunk_text,
    strip_mention,
    to_mrkdwn,
)
from .state import StateStore, ThreadSessions, utcnow
from .tools.common import safe_error, scrub

log = logging.getLogger("mungchi.slack")

RunTurn = Callable[..., Awaitable[TurnResult]]
# Returns the Slack text for the credit shortcut (blocking: run in a worker thread).
CreditText = Callable[[], str]
# Returns the Slack text for the weather shortcut (blocking: run in a worker thread).
WeatherText = Callable[[], str]

REFUSAL_TEXT = "죄송하지만 이 봇은 소유자만 사용할 수 있어요."
FRESH_SESSION_NOTE = "이 스레드의 이전 대화는 이어 갈 수 없어서, 다음 메시지부터는 새 대화로 시작해요."
CREDIT_CRASH_TEXT = "⚠️ 크레딧을 확인하지 못했어요 ({kind}). 잠시 후 다시 시도해 주세요."
WEATHER_CRASH_TEXT = "⚠️ 날씨를 가져오지 못했어요 ({kind}). 잠시 후 다시 시도해 주세요."

# Low-credit alert inside the running bots: first check shortly after start, then hourly.
CREDIT_CHECK_FIRST_DELAY_SECONDS = 60.0
CREDIT_CHECK_INTERVAL_SECONDS = 3_600.0

# Scheduled morning briefing: the wall clock is read again every 30 seconds
# (never one long sleep until BRIEF_TIME: on macOS the monotonic clock stops
# while the Mac sleeps). One briefing run may take at most 15 minutes.
BRIEF_CHECK_INTERVAL_SECONDS = 30.0
BRIEF_RUN_TIMEOUT_SECONDS = 15 * 60.0
NO_MUNGCHI_BRIEF_WARNING = (
    "고뭉치 봇(SLACK_BOT_TOKEN, SLACK_APP_TOKEN)이 켜져 있지 않아 아침 브리핑을 보내지 않습니다. "
    "아침 브리핑은 고뭉치 봇이 보냅니다."
)

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
        credit_text: CreditText | None = None,
        weather_text: WeatherText | None = None,
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
        # The credit shortcut: gateway endpoints only, never an agent turn.
        self.credit_text = credit_text or credits.slack_credit_text
        # The weather shortcut: Open-Meteo only, never an agent turn.
        self.weather_text = weather_text or weather.slack_weather_text
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
        # The allow-list comes first: strangers get the refusal, whatever they asked.
        if not self.is_allowed(user):
            await self._refuse(channel, thread_ts, user)
            return
        # The user's text without this bot's mention; empty means ``default_prompt()``.
        request = strip_mention(str(event.get("text") or ""), self.bot_user_id)
        if credits.is_credit_query(request):
            await self._answer_credits(channel, thread_ts)
            return
        if weather.is_weather_query(request):
            await self._answer_weather(channel, thread_ts)
            return
        await self._answer(channel, thread_ts, request or self.default_prompt())

    async def _refuse(self, channel: str, thread_ts: str, user: str) -> None:
        log.warning("%s 봇: 허용되지 않은 사용자(%s)의 요청을 거절했습니다.", self.texts.label, user)
        if self._refused.seen(f"{channel}:{thread_ts}"):
            return  # refuse once per thread
        await self._post(channel, thread_ts, REFUSAL_TEXT)

    # -- credit shortcut (no agent turn, no LLM call, no session)

    async def _answer_credits(self, channel: str, thread_ts: str) -> None:
        """Reply in the thread with the credit summary; the thread -> session map is not touched."""
        log.info("%s: 스레드 %s:%s 크레딧 바로 답변 (에이전트 실행 없음)", self.texts.label, channel, thread_ts)
        try:
            text = await asyncio.to_thread(self.credit_text)
        except Exception as exc:  # noqa: BLE001 - reported in Slack without details
            _log_exception("크레딧을 확인하지 못했습니다.", exc)
            text = CREDIT_CRASH_TEXT.format(kind=type(exc).__name__)
        for chunk in to_slack_chunks(text) or [CREDIT_CRASH_TEXT.format(kind="빈 응답")]:
            await self._post(channel, thread_ts, chunk)

    # -- weather shortcut (no agent turn, no LLM call, no session)

    async def _answer_weather(self, channel: str, thread_ts: str) -> None:
        """Reply in the thread with today's weather line; the thread -> session map is not touched."""
        log.info("%s: 스레드 %s:%s 날씨 바로 답변 (에이전트 실행 없음)", self.texts.label, channel, thread_ts)
        try:
            text = await asyncio.to_thread(self.weather_text)
        except Exception as exc:  # noqa: BLE001 - reported in Slack without details
            _log_exception("날씨를 가져오지 못했습니다.", exc)
            text = WEATHER_CRASH_TEXT.format(kind=type(exc).__name__)
        for chunk in to_slack_chunks(text) or [WEATHER_CRASH_TEXT.format(kind="빈 응답")]:
            await self._post(channel, thread_ts, chunk)

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


# ---------------------------------------------------------------- low-credit alert


async def check_low_credits(
    client: Any,
    user_ids: Iterable[str],
    *,
    env: Mapping[str, str] | None = None,
    store: StateStore | None = None,
    fetch: Callable[[], credits.CreditReport] | None = None,
    now: datetime | None = None,
) -> str:
    """One low-credit check: DM every allowed user once per renewal period when credits run low.

    Returns what happened: ``"disabled"`` (CREDIT_ALERT_PERCENT empty or 0),
    ``"unsupported"`` (not the Chat KHU gateway: skipped silently), ``"failed"``
    (logged, scrubbed), ``"ok"`` (enough left), ``"already"`` (alerted this
    period) or ``"alerted"``. Only the gateway's credit endpoints are called,
    never a model. Raises nothing for a failed check or a failed DM.
    """
    threshold = config.get_credit_alert_percent(env)
    if threshold <= 0:
        return "disabled"
    if credits.gateway_root(env) is None:
        return "unsupported"
    report = await asyncio.to_thread(fetch or (lambda: credits.fetch_report(env)))
    if not report.supported:
        return "unsupported"
    if not report.ok or report.balance is None:
        log.warning("크레딧을 확인하지 못해 잔액 알림을 건너뜁니다: %s", scrub(" ".join((report.error or "").split())))
        return "failed"
    left = credits.remaining_percent(report.balance)
    if left is None:
        log.warning("남은 크레딧 비율을 알 수 없어 잔액 알림을 건너뜁니다 (total.quota/remaining 없음).")
        return "failed"
    if left >= threshold:
        return "ok"
    now = now or utcnow()
    store = store or StateStore(config.get_state_path(env))
    period = credits.alert_period(report.balance, now)
    if store.credit_alert_period() == period:
        return "already"
    chunks = to_slack_chunks(credits.alert_text(report, threshold, env=env, now=now))
    sent = 0
    for user in sorted(user_ids):
        try:
            for chunk in chunks:
                await client.chat_postMessage(channel=user, text=chunk, unfurl_links=False, unfurl_media=False)
        except Exception as exc:  # noqa: BLE001 - try the others, retry next hour if nobody got it
            log.error("크레딧 알림 DM을 보내지 못했습니다(%s): %s", user, describe_slack_error(exc))
        else:
            sent += 1
    if not sent:
        return "failed"
    try:
        store.mark_credit_alert(period, now)
    except OSError as exc:
        log.warning("크레딧 알림을 보낸 기록을 저장하지 못했습니다: %s", safe_error(exc))
    log.info(
        "남은 크레딧 %s%%: 알림 기준(%s%%) 아래라 %d명에게 DM을 보냈습니다.",
        credits.fmt_number(left),
        credits.fmt_number(threshold),
        sent,
    )
    return "alerted"


async def credit_alert_loop(
    client: Any,
    user_ids: Iterable[str],
    *,
    env: Mapping[str, str] | None = None,
    first_delay: float = CREDIT_CHECK_FIRST_DELAY_SECONDS,
    interval: float = CREDIT_CHECK_INTERVAL_SECONDS,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    check: Callable[..., Awaitable[str]] = check_low_credits,
) -> None:
    """Check the credits ``first_delay`` seconds after start, then every ``interval`` seconds.

    Runs until cancelled. A failed check is logged (scrubbed) and never stops
    the loop or the bots.
    """
    users = frozenset(user_ids)
    delay = first_delay
    while True:
        await sleep(delay)
        delay = interval
        try:
            await check(client, users, env=env)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the bots keep running whatever happens here
            log.warning("크레딧 잔액을 확인하지 못했습니다 (봇은 그대로 돕니다): %s", safe_error(exc))


def _alert_bot(bots: list[tuple[config.SlackBotConfig, Any, SlackHandler]]) -> tuple[config.SlackBotConfig, Any]:
    """The bot that sends the alert: 고뭉치 when configured, else the first configured bot."""
    for bot, app, _handler in bots:
        if bot.persona == MUNGCHI:
            return bot, app
    bot, app, _handler = bots[0]
    return bot, app


# ---------------------------------------------------------------- scheduled morning briefing


@dataclass
class BriefLoopState:
    """What the morning briefing scheduler remembers between checks (in memory, per process).

    ``attempted`` is the local date a briefing was started for: it is never
    started twice in one process, even when the state file cannot be written.
    ``skip_logged`` is the date the "too late, skipped" line was logged for.
    """

    attempted: str | None = None
    skip_logged: str | None = None


async def morning_brief_tick(
    client: Any,
    destinations: Sequence[str],
    *,
    schedule: config.BriefSchedule,
    state: BriefLoopState,
    env: Mapping[str, str] | None = None,
    store: StateStore | None = None,
    clock: Callable[[], datetime] | None = None,
    run: RunTurn | None = None,
    sessions: ThreadSessions | None = None,
    credit_fetch: Callable[[], credits.CreditReport] | None = None,
    weather_fetch: Callable[[], weather.WeatherReport] | None = None,
    semaphore: asyncio.Semaphore | None = None,
    run_timeout: float | None = BRIEF_RUN_TIMEOUT_SECONDS,
) -> str:
    """One look at the clock: send today's morning briefing if it is due (``briefing.brief_due``).

    Returns that outcome, or ``"sent"`` / ``"failed"`` after a briefing.
    ``last_brief_date`` is written *before* posting, so a crash in the middle
    never sends the same day's briefing twice after a restart. A failure is
    logged (scrubbed) and not retried that day.
    """
    now = (clock or utcnow)().astimezone(schedule.timezone)
    today = now.date().isoformat()
    if state.attempted == today:
        return briefing.ALREADY
    store = store or StateStore(config.get_state_path(env))
    status = briefing.brief_due(schedule, now, store.last_brief_date())
    if status == briefing.MISSED and state.skip_logged != today:
        state.skip_logged = today
        log.info(
            "아침 브리핑: 오늘(%s) 브리핑은 %s 보내지 못해 건너뜁니다 (Mac이 잠자고 있었거나 봇이 늦게 켜짐). 다음 브리핑: %s",
            today,
            schedule.catchup_text(),
            schedule.describe(),
        )
    if status != briefing.DUE:
        return status

    state.attempted = today
    try:
        store.mark_brief_date(today)
    except OSError as exc:
        log.warning("아침 브리핑 날짜(last_brief_date)를 기록하지 못했습니다 (이 실행에서는 다시 보내지 않습니다): %s", safe_error(exc))
    log.info("아침 브리핑을 보냅니다 (%s %s, %d곳).", today, f"{now:%H:%M}", len(destinations))
    try:
        async with semaphore if semaphore is not None else contextlib.nullcontext():
            code = await post_briefing(
                client,
                list(destinations),
                run=run,
                sessions=sessions,
                now=now,
                env=env,
                credit_fetch=credit_fetch,
                weather_fetch=weather_fetch,
                run_timeout=run_timeout,
            )
    except Exception as exc:  # noqa: BLE001 - one scrubbed line, the bots keep running
        log.error("아침 브리핑 실패 (%s): %s", today, safe_error(exc))
        return "failed"
    if code != 0:
        log.warning("아침 브리핑 실패 (%s): 브리핑을 끝내지 못했거나 Slack에 올리지 못했습니다. 위 로그를 보세요.", today)
        return "failed"
    log.info("아침 브리핑을 보냈습니다 (%s).", today)
    return "sent"


async def morning_brief_loop(
    client: Any,
    destinations: Sequence[str],
    *,
    schedule: config.BriefSchedule,
    env: Mapping[str, str] | None = None,
    interval: float = BRIEF_CHECK_INTERVAL_SECONDS,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    tick: Callable[..., Awaitable[str]] = morning_brief_tick,
    **options: Any,
) -> None:
    """Check the wall clock now and every ``interval`` seconds; send the briefing when due.

    Runs until cancelled. Anything that goes wrong is logged (scrubbed) and
    never stops the loop or the bots.
    """
    state = BriefLoopState()
    targets = list(destinations)
    while True:
        try:
            await tick(client, targets, schedule=schedule, state=state, env=env, **options)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the bots keep running whatever happens here
            log.warning("아침 브리핑 시각을 확인하지 못했습니다 (봇은 그대로 돕니다): %s", safe_error(exc))
        await sleep(interval)


def morning_brief_target(
    cfg: config.SlackConfig, bots: list[tuple[config.SlackBotConfig, Any, SlackHandler]]
) -> tuple[tuple[config.SlackBotConfig, Any, SlackHandler] | None, str]:
    """``(고뭉치's bot, "")``, or ``(None, Korean warning)`` when the briefing cannot be sent."""
    for bot, app, handler in bots:
        if bot.persona == MUNGCHI:
            problems = config.slack_brief_problems(cfg)
            if problems:
                return None, "아침 브리핑을 보낼 수 없어 끕니다: " + " ".join(problems)
            return (bot, app, handler), ""
    return None, NO_MUNGCHI_BRIEF_WARNING


def _start_morning_brief(
    cfg: config.SlackConfig,
    bots: list[tuple[config.SlackBotConfig, Any, SlackHandler]],
    scheduler: Callable[..., Awaitable[None]],
) -> asyncio.Task[None] | None:
    """Print the schedule line and start ``scheduler`` with 고뭉치's client; None when off. Never raises."""
    try:
        schedule = config.load_brief_schedule()
        for warning in schedule.warnings:
            log.warning("아침 브리핑 설정: %s", warning)
        if not schedule.enabled:
            print(f"아침 브리핑: {schedule.describe()}", file=sys.stderr)
            return None
        target, warning = morning_brief_target(cfg, bots)
        if target is None:
            log.warning("%s", warning)
            return None
        _bot, app, handler = target
        print(
            f"아침 브리핑: {schedule.describe()} → {config.describe_brief_destination(cfg)} "
            f"(그 시각에 Mac이 잠자고 있었으면 깨어난 뒤 {schedule.catchup_text()} 보냅니다)",
            file=sys.stderr,
        )
        return asyncio.create_task(
            scheduler(
                app.client,
                config.brief_destinations(cfg),
                schedule=schedule,
                sessions=handler.sessions,
                semaphore=handler._semaphore,
            )
        )
    except Exception as exc:  # noqa: BLE001 - the bots run without the briefing
        log.warning("아침 브리핑을 시작하지 못했습니다 (봇은 그대로 돕니다): %s", safe_error(exc))
        return None


async def run_bots(
    cfg: config.SlackConfig,
    *,
    socket_factory: Callable[[Any, str], Any] | None = None,
    wait: Callable[[], Awaitable[None]] = _wait_forever,
    credit_alert: Callable[..., Awaitable[None]] | None = None,
    brief_scheduler: Callable[..., Awaitable[None]] | None = None,
) -> int:
    """Connect every configured bot over Socket Mode and serve them in one event loop.

    While they run, ``credit_alert`` (default ``credit_alert_loop``) watches the
    Chat KHU credits and DMs the allowed users through 고뭉치's bot (or the
    first configured bot) when they run low, and ``brief_scheduler`` (default
    ``morning_brief_loop``, only with ``BRIEF_TIME`` and 고뭉치's bot) sends
    the morning briefing through 고뭉치's bot.
    """
    if socket_factory is None:
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

        socket_factory = AsyncSocketModeHandler
    bots = build_apps(cfg)
    sockets: list[Any] = []
    alert_task: asyncio.Task[None] | None = None
    brief_task: asyncio.Task[None] | None = None
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
        if bots:
            alert_bot, alert_app = _alert_bot(bots)
            threshold = config.get_credit_alert_percent()
            if threshold > 0 and credits.gateway_root() is not None:
                print(
                    f"크레딧 잔액 알림: 남은 크레딧이 {credits.fmt_number(threshold)}% 아래로 내려가면 "
                    f"{_describe_bot(alert_bot)} 봇이 DM으로 알립니다 (1시간마다 확인, 갱신 주기마다 한 번).",
                    file=sys.stderr,
                )
            alert_task = asyncio.create_task((credit_alert or credit_alert_loop)(alert_app.client, cfg.allowed_user_ids))
            brief_task = _start_morning_brief(cfg, bots, brief_scheduler or morning_brief_loop)
        await wait()
    finally:
        for task in (alert_task, brief_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        for socket in sockets:
            with contextlib.suppress(Exception):
                await socket.close_async()
    return 0


# ---------------------------------------------------------------- briefing


async def _post_brief_chunks(client: Any, channel: str, chunks: list[str]) -> tuple[str, str | None] | None:
    """Post one briefing to ``channel``: the first chunk, the rest in its thread.

    Returns ``(channel, ts)`` of the first message (for a DM, the DM channel
    Slack answers with), or None when it could not be posted. Logs, never raises.
    """
    try:
        first = await client.chat_postMessage(channel=channel, text=chunks[0], unfurl_links=False, unfurl_media=False)
    except Exception as exc:  # noqa: BLE001 - the other destinations still get theirs
        log.error("브리핑을 Slack(%s)에 올리지 못했습니다: %s", channel, describe_slack_error(exc))
        return None
    root_channel = str(first.get("channel") or channel)
    root_ts = first.get("ts")
    for chunk in chunks[1:]:
        try:
            await client.chat_postMessage(
                channel=root_channel, thread_ts=root_ts, text=chunk, unfurl_links=False, unfurl_media=False
            )
        except Exception as exc:  # noqa: BLE001
            log.error("브리핑의 나머지를 스레드(%s)에 올리지 못했습니다: %s", channel, describe_slack_error(exc))
            break
    return root_channel, root_ts


async def post_briefing(
    client: Any,
    channel: str | Sequence[str],
    *,
    run: RunTurn | None = None,
    sessions: ThreadSessions | None = None,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    on_status: Callable[[str], Any] | None = None,
    credit_fetch: Callable[[], credits.CreditReport] | None = None,
    weather_fetch: Callable[[], weather.WeatherReport] | None = None,
    run_timeout: float | None = None,
) -> int:
    """Run today's briefing once and post it to ``channel`` (a channel id, or several, e.g. one DM per user).

    The briefing is ``briefing.build_briefing``'s: header with the weather
    line under it (by code), 고뭉치's ① 오늘의 일정 and ② Dropbox 업데이트,
    then the credits appended by code. If the run fails, the header, the
    weather line, a short failure line and the credits still go out. The
    first message reads without opening anything; longer briefings continue
    in that message's thread. Each thread is mapped to
    고뭉치's briefing session, so replying to @moongchi there continues it.

    This is a briefing run (``briefing=True``): its Dropbox check looks at the
    time since the last briefing and moves that checkpoint. A later mention in
    the thread is an ordinary turn again. Returns 0 when the briefing
    completed and reached every destination, else 1 (details are logged).
    """
    destinations = [channel] if isinstance(channel, str) else [c for c in channel if c]
    result = await build_briefing(
        run=run or run_turn,
        now=now,
        env=env,
        slack=True,
        on_status=on_status,
        credit_fetch=credit_fetch,
        weather_fetch=weather_fetch,
        run_timeout=run_timeout,
    )
    if result.crash is not None:
        _log_exception("브리핑을 만들지 못했습니다.", result.crash)
    elif result.failed:
        log.warning("브리핑이 완전하지 않습니다: %s", scrub((result.result.error if result.result else "") or ""))
    chunks = to_slack_chunks(result.text) or [to_mrkdwn(result.header)]
    delivered = 0
    for destination in destinations:
        posted = await _post_brief_chunks(client, destination, chunks)
        if posted is None:
            continue
        delivered += 1
        root_channel, root_ts = posted
        if sessions is not None and result.session_id and root_ts:
            remember_session(sessions, root_channel, root_ts, result.session_id, persona=MUNGCHI)
    return 0 if destinations and delivered == len(destinations) and not result.failed else 1


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
    """``python -m mungchi --brief --slack``: send today's briefing now, where the morning briefing goes.

    ``SLACK_BRIEF_CHANNEL``, else a DM to every user in ``SLACK_ALLOWED_USER_IDS``.
    Does not touch ``last_brief_date``: a test run never stops the scheduled one.
    """
    cfg = config.load_slack_config(env)
    problems = config.slack_brief_problems(cfg)
    if problems:
        _print_problems("브리핑을 Slack에 올릴 수 없습니다.", problems)
        return 1
    setup_logging()
    where = config.describe_brief_destination(cfg)

    async def deliver() -> int:
        client = AsyncWebClient(token=cfg.bot_token)
        sessions = ThreadSessions(config.get_slack_threads_path(env))
        return await post_briefing(
            client, config.brief_destinations(cfg), sessions=sessions, env=env, on_status=_print_status
        )

    try:
        code = asyncio.run(deliver())
    except SlackApiError as exc:
        print(f"[오류] Slack에 브리핑을 올리지 못했습니다: {describe_slack_error(exc)}", file=sys.stderr)
        return 1
    if code == 0:
        print(f"Slack에 오늘 브리핑을 올렸습니다 ({where}).", file=sys.stderr)
    else:
        print(
            f"[오류] 브리핑이 완전하지 않거나 Slack({where})에 올리지 못했습니다. 위의 오류를 확인하세요.",
            file=sys.stderr,
        )
    return code
