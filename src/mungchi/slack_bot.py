"""Slack front end: the Socket Mode bot (``python -m mungchi slack``) and
briefing delivery (``python -m mungchi --brief --slack``).

``SlackHandler`` holds all event logic and talks to Slack only through an
injected ``AsyncWebClient``-like object, so it can be tested without Bolt.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
import traceback
from collections import OrderedDict
from datetime import datetime
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Mapping

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from . import config
from .main import TurnResult, briefing_prompt, run_turn
from .slack_format import (
    PLACEHOLDER_TEXT,
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
EMPTY_ANSWER_TEXT = "고뭉치가 빈 답을 보냈어요. 다시 물어봐 주세요."
FAILED_TEXT = "⚠️ 고뭉치가 답을 끝내지 못했어요."
CRASH_TEXT = "⚠️ 고뭉치를 실행하지 못했어요 ({kind}). 잠시 후 다시 시도해 주세요."
FRESH_SESSION_NOTE = "이 스레드의 이전 대화는 이어 갈 수 없어서, 다음 메시지부터는 새 대화로 시작해요."
BRIEF_CRASH_TEXT = "⚠️ 오늘 브리핑을 만들지 못했어요 ({kind}). 실행 로그를 확인해 주세요."

STATUS_INTERVAL_SECONDS = 1.0
MAX_STATUS_LINES = 6
MAX_ERROR_CHARS = 300
MAX_REMEMBERED_EVENTS = 1_000

SLACK_ERROR_HINTS = {
    "invalid_auth": "토큰이 올바르지 않습니다. SLACK_BOT_TOKEN과 SLACK_APP_TOKEN을 확인하세요.",
    "not_authed": "토큰이 없습니다. SLACK_BOT_TOKEN을 확인하세요.",
    "account_inactive": "토큰이 더 이상 유효하지 않습니다. 앱을 다시 설치하고 새 토큰을 받으세요.",
    "token_revoked": "토큰이 취소되었습니다. 앱을 다시 설치하고 새 토큰을 받으세요.",
    "not_in_channel": "봇이 채널에 없습니다. 채널에서 /invite @moongchi 로 초대하세요.",
    "channel_not_found": "채널을 찾을 수 없습니다. SLACK_BRIEF_CHANNEL의 채널 ID를 확인하고, 비공개 채널이면 봇을 먼저 초대하세요.",
    "missing_scope": "앱 권한이 부족합니다. slack_manifest.yaml대로 권한을 주고 앱을 다시 설치하세요.",
    "is_archived": "보관된 채널에는 올릴 수 없습니다.",
}


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


def compose_reply(result: TurnResult) -> str:
    """Answer text plus a short Korean error note if the turn failed."""
    text = (result.text or "").strip()
    if result.failed:
        reason = scrub(result.error or "")[:MAX_ERROR_CHARS]
        note = f"⚠️ {reason}" if reason else FAILED_TEXT
        return f"{text}\n\n{note}" if text else note
    return text or EMPTY_ANSWER_TEXT


def to_slack_chunks(text: str) -> list[str]:
    """Final outgoing text: secrets scrubbed, mrkdwn safety net, Slack-sized chunks."""
    return chunk_text(to_mrkdwn(scrub(text)))


def remember_session(sessions: ThreadSessions, channel: str, thread_ts: str, session_id: str | None) -> None:
    """Store (or with ``session_id=None`` drop) a thread's session; a disk error never blocks a reply."""
    try:
        if session_id:
            sessions.set(channel, thread_ts, session_id)
        else:
            sessions.forget(channel, thread_ts)
    except OSError as exc:
        log.warning("스레드와 대화의 연결을 저장하지 못했습니다: %s", safe_error(exc))


def describe_slack_error(exc: BaseException) -> str:
    code = ""
    response = getattr(exc, "response", None)
    with contextlib.suppress(Exception):
        code = str(response.get("error") or "") if response is not None else ""
    hint = SLACK_ERROR_HINTS.get(code)
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
    ):
        self.client = client
        self.channel = channel
        self.ts = ts
        self.interval = interval
        self.clock = clock
        self.lines: list[str] = []
        self._last: float | None = None
        self._pending: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._closed_event = asyncio.Event()

    def text(self) -> str:
        return "\n".join([PLACEHOLDER_TEXT, *self.lines[-MAX_STATUS_LINES:]])

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
    """Turns Slack events into 고뭉치 turns and posts the replies in threads."""

    def __init__(
        self,
        client: Any,
        *,
        allowed_user_ids: Iterable[str],
        sessions: ThreadSessions,
        run: RunTurn | None = None,
        bot_user_id: str | None = None,
        max_concurrent: int = config.DEFAULT_SLACK_MAX_CONCURRENT,
        status_interval: float = STATUS_INTERVAL_SECONDS,
    ):
        self.allowed_user_ids = frozenset(allowed_user_ids)
        if not self.allowed_user_ids:
            # Never run without an allow-list: 고뭉치 reads private data.
            raise ValueError("allowed_user_ids must not be empty")
        self.client = client
        self.sessions = sessions
        self.run = run
        self.bot_user_id = bot_user_id
        self.status_interval = status_interval
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))
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
        if not event.get("channel") or not event.get("ts"):
            return "malformed"
        return None

    def is_allowed(self, user: str | None) -> bool:
        return bool(user) and user in self.allowed_user_ids

    def build_prompt(self, text: str) -> str:
        """The user's text without the bot mention; empty means "today's briefing"."""
        return strip_mention(text, self.bot_user_id) or briefing_prompt()

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
        log.warning("허용되지 않은 사용자(%s)의 요청을 거절했습니다.", user)
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
        placeholder = await self._post(channel, thread_ts, PLACEHOLDER_TEXT)
        # Messages in one thread run in order; all threads share the cost cap.
        async with self._thread_lock(f"{channel}:{thread_ts}"):
            async with self._semaphore:
                await self._run_and_reply(channel, thread_ts, placeholder, prompt)

    async def _run_and_reply(self, channel: str, thread_ts: str, placeholder: str | None, prompt: str) -> None:
        resume = self.sessions.get(channel, thread_ts)
        updater = (
            StatusUpdater(self.client, channel, placeholder, interval=self.status_interval) if placeholder else None
        )
        log.info("스레드 %s:%s 처리 시작 (%s)", channel, thread_ts, "이어서" if resume else "새 대화")
        run = self.run or run_turn
        result: TurnResult | None = None
        crash: Exception | None = None
        try:
            result = await run(prompt, resume=resume, on_status=updater, extra_system_prompt=SLACK_FORMAT_PROMPT)
        except Exception as exc:  # noqa: BLE001 - reported in Slack without details
            crash = exc
        finally:
            if updater is not None:
                await updater.close()

        if result is None:
            _log_exception("고뭉치 실행 중 오류가 났습니다.", crash or RuntimeError("no result"))
            reply = CRASH_TEXT.format(kind=type(crash).__name__)
            if resume:
                # The stored session may be unusable (e.g. its transcript is
                # gone); start fresh next time instead of failing forever.
                remember_session(self.sessions, channel, thread_ts, None)
                reply += "\n" + FRESH_SESSION_NOTE
            await self._finish(channel, thread_ts, placeholder, [reply])
            return

        reply = compose_reply(result)
        if result.session_id:
            remember_session(self.sessions, channel, thread_ts, result.session_id)
        elif resume and result.failed:
            remember_session(self.sessions, channel, thread_ts, None)
            reply += "\n" + FRESH_SESSION_NOTE
        if result.failed:
            log.warning("고뭉치가 답을 끝내지 못했습니다: %s", scrub(result.error or ""))
        await self._finish(channel, thread_ts, placeholder, to_slack_chunks(reply))
        log.info("스레드 %s:%s 답변 완료", channel, thread_ts)

    # -- Slack I/O (failures are logged, never raised)

    async def _finish(self, channel: str, thread_ts: str, placeholder: str | None, chunks: list[str]) -> None:
        chunks = chunks or [EMPTY_ANSWER_TEXT]
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
            log.error("Slack 메시지를 보내지 못했습니다: %s", describe_slack_error(exc))
            return None
        return response.get("ts")

    async def _update(self, channel: str, ts: str, text: str) -> bool:
        try:
            await self.client.chat_update(channel=channel, ts=ts, text=text)
        except Exception as exc:  # noqa: BLE001
            log.warning("Slack 메시지를 고치지 못했습니다: %s", describe_slack_error(exc))
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
    sessions: ThreadSessions | None = None,
    run: RunTurn | None = None,
) -> tuple[Any, SlackHandler]:
    """Create the Bolt app and its handler. Does not contact Slack."""
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(token=cfg.bot_token, name="mungchi")
    handler = SlackHandler(
        app.client,
        allowed_user_ids=cfg.allowed_user_ids,
        sessions=sessions or ThreadSessions(config.get_slack_threads_path()),
        run=run,
        max_concurrent=cfg.max_concurrent,
    )
    register_listeners(app, handler)
    return app, handler


async def run_bot(cfg: config.SlackConfig) -> int:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    app, handler = build_app(cfg)
    auth = await app.client.auth_test()
    handler.bot_user_id = auth.get("user_id")
    socket_mode = AsyncSocketModeHandler(app, cfg.app_token)
    print(
        f"고뭉치 Slack 봇을 시작했습니다 (Socket Mode, 허용된 사용자 {len(cfg.allowed_user_ids)}명). "
        "멈추려면 Ctrl+C를 누르세요.",
        file=sys.stderr,
    )
    try:
        await socket_mode.start_async()
    finally:
        await socket_mode.close_async()
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
    replies in that message's thread. The thread is mapped to the briefing
    session, so mentioning the bot in it continues the same conversation.
    """
    tz = config.get_timezone(env)
    now = (now or datetime.now(tz)).astimezone(tz)
    header = brief_header(now)
    run = run or run_turn
    try:
        result = await run(briefing_prompt(now, env), on_status=on_status, extra_system_prompt=SLACK_FORMAT_PROMPT)
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
        remember_session(sessions, root_channel, root_ts, result.session_id)
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
        return asyncio.run(run_bot(cfg))
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
