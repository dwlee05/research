"""Slack front end: the Socket Mode bots (``python -m mungchi slack``), the
scheduled morning briefing they send (``BRIEF_TIME``) and briefing delivery
(``python -m mungchi --brief --slack``). Short credit ("토큰") and weather
questions, alone or together ("날씨랑 토큰 좀 말해봐"), are answered by code,
without an agent turn. A short briefing request to 고뭉치 ("오늘 건너뛴 브리핑
좀 해봐", or a bare ``@고뭉치``) gets the same relay briefing as the morning
one (``post_briefing``): 고뭉치 greets with the weather and the credits and
hands off, then 업뎃 and 일정 post their own parts as their own bots. The
answer to a calendar proposal waiting in the thread (events from a pasted
note) is handled by code too, without an agent turn: a category picked by
number or name ("2", "Research", "khu"), "네" (the suggested category),
"아니요", or a click on one of the category buttons posted under the
preview. The events are created, or the proposal dropped.

A photo (poster, email screenshot, timetable) sent to 업뎃 or 일정 (a mention
with an image, or a DM) is downloaded with the bot token, shrunk in memory
(``images``) and sent to the agent with the text, which proposes its events
like a pasted note. The allow-list is checked before anything is
downloaded. 고뭉치 points photos to 업뎃 / 일정 without an agent turn.

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
import json
import logging
import random
import re
import sys
import time
import traceback
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

import httpx
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from . import briefing, config, credits, images, phrases, quick_info, version, weather
from .main import TurnResult, run_turn
from .personas import MUNGCHI, PERSONA_LABELS, PERSONAS, SCHEDULE, SLACK_HANDLES, UPDATE, call_name, josa
from .slack_format import (
    PLACEHOLDER_TEXT,
    SLACK_FORMAT_PROMPT,
    chunk_text,
    strip_mention,
    to_mrkdwn,
)
from .state import StateStore, ThreadSessions, utcnow
from .tools import event_proposals
from .tools.common import safe_error, scrub

log = logging.getLogger("mungchi.slack")

RunTurn = Callable[..., Awaitable[TurnResult]]
# Returns the Slack text for the credit shortcut (blocking: run in a worker thread).
CreditText = Callable[[], str]
# Returns the Slack text for the weather shortcut (blocking: run in a worker thread).
WeatherText = Callable[[], str]
# ``post_briefing``-like: runs the relay briefing to the given targets, returns 0 when all went well.
BriefingRelay = Callable[..., Awaitable[int]]
# ``event_proposals.create_proposal_events``-like (blocking: run in a worker thread).
EventCreator = Callable[[Mapping[str, Any]], event_proposals.CreationOutcome]
# ``download_slack_file``-like: (url_private_download, bot token) -> the file's bytes.
FileFetcher = Callable[[str, str], Awaitable[bytes]]

REFUSAL_TEXT = "죄송하지만 이 봇은 소유자만 사용할 수 있어요."
FRESH_SESSION_NOTE = "이 스레드의 이전 대화는 이어 갈 수 없어서, 다음 메시지부터는 새 대화로 시작해요."
CREDIT_CRASH_TEXT = "⚠️ 크레딧을 확인하지 못했어요 ({kind}). 잠시 후 다시 시도해 주세요."
WEATHER_CRASH_TEXT = "⚠️ 날씨를 가져오지 못했어요 ({kind}). 잠시 후 다시 시도해 주세요."
CALENDAR_CRASH_TEXT = "❌ 캘린더에 추가하지 못했어요 ({kind}). 다시 부탁해 주세요."
BRIEF_CRASH_TEXT = "⚠️ 오늘 브리핑을 만들지 못했어요 ({kind}). 실행 로그를 확인해 주세요."
# Category buttons under a calendar proposal (Block Kit, needs Interactivity).
CATEGORY_ACTION_PREFIX = "mungchi_cal_"
CATEGORY_ACTION_RE = re.compile(rf"^{CATEGORY_ACTION_PREFIX}")
PICK_ACTION = CATEGORY_ACTION_PREFIX + "pick_{index}"
CONFIRM_ACTION = CATEGORY_ACTION_PREFIX + "confirm"
CANCEL_ACTION = CATEGORY_ACTION_PREFIX + "cancel"
STALE_ACTION_TEXT = "이미 처리됐거나 만료된 요청이에요"
BUTTONS_ANSWERED_TEXT = "버튼 대신 답장으로 처리했어요."
BUTTONS_REPLACED_TEXT = "새 메시지가 와서 이 제안은 닫았어요."
MAX_BUTTON_TEXT_CHARS = 75
# Photos: 고뭉치 does not read them; 업뎃 and 일정 do.
IMAGE_REDIRECT_TEXT = "사진 속 일정 등록은 @업뎃이나 @일정에게 보내주세요"
FILE_SHARE_SUBTYPE = "file_share"
NO_IMAGE_READ_TEXT = "보낸 사진을 하나도 읽지 못했어요."
FILE_DOWNLOAD_TIMEOUT_SECONDS = 30.0

# Low-credit alert inside the running bots: first check shortly after start, then hourly.
CREDIT_CHECK_FIRST_DELAY_SECONDS = 60.0
CREDIT_CHECK_INTERVAL_SECONDS = 3_600.0

# Scheduled morning briefing: the wall clock is read again every 30 seconds
# (never one long sleep until BRIEF_TIME: on macOS the monotonic clock stops
# while the Mac sleeps). 업뎃's and 일정's report runs may take at most 15 minutes each.
BRIEF_CHECK_INTERVAL_SECONDS = 30.0
BRIEF_RUN_TIMEOUT_SECONDS = 15 * 60.0
NO_MUNGCHI_BRIEF_WARNING = (
    "고뭉치 봇(SLACK_BOT_TOKEN, SLACK_APP_TOKEN)이 켜져 있지 않아 아침 브리핑을 보내지 않습니다. "
    "아침 브리핑은 고뭉치 봇이 보냅니다."
)

# What a bare mention (no text) asks 업뎃 and 일정 for. 고뭉치's is today's
# briefing, built by code like the morning one (``SlackHandler.wants_briefing``).
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
    placeholders: tuple[str, ...]  # the first reply while the agent works: one is picked at random
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
        placeholders=phrases.PLACEHOLDER_POOLS[persona],
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


class FileDownloadError(RuntimeError):
    """A Slack file could not be downloaded; ``str()`` is a short Korean reason (never the token or the URL)."""

    def __init__(self, reason: str, *, missing_scope: bool = False):
        super().__init__(reason)
        self.missing_scope = missing_scope


def is_slack_file_url(url: str) -> bool:
    """Only Slack's own https file hosts ever get the bot token."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host == "slack.com" or host.endswith(".slack.com"))


async def download_slack_file(
    url: str,
    token: str,
    *,
    max_bytes: int = images.MAX_FILE_BYTES,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = FILE_DOWNLOAD_TIMEOUT_SECONDS,
) -> bytes:
    """A Slack file's bytes (``url_private_download`` with ``Authorization: Bearer <bot token>``), in memory.

    Refuses anything that is not a Slack https address, stops reading past
    ``max_bytes``, and turns Slack's HTML sign-in page (what a token without
    ``files:read`` gets) into ``FileDownloadError(missing_scope=True)``. Errors
    never carry the token or the URL. httpx drops the token on a redirect to
    another host.
    """
    if not is_slack_file_url(url):
        raise FileDownloadError("Slack 파일 주소가 아니에요")
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, transport=transport) as client:
            async with client.stream("GET", url, headers={"Authorization": f"Bearer {token}"}) as response:
                if response.status_code != 200:
                    raise FileDownloadError(f"HTTP {response.status_code}")
                if response.headers.get("content-type", "").lower().startswith("text/html"):
                    raise FileDownloadError("files:read 권한이 없어요", missing_scope=True)
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise FileDownloadError(images.TOO_BIG_TEXT)
                    chunks.append(chunk)
    except FileDownloadError:
        raise
    except httpx.HTTPError as exc:
        raise FileDownloadError(type(exc).__name__) from None
    return b"".join(chunks)


def _button(action_id: str, text: str, value: Mapping[str, Any], *, primary: bool = False) -> dict[str, Any]:
    label = text if len(text) <= MAX_BUTTON_TEXT_CHARS else text[: MAX_BUTTON_TEXT_CHARS - 1] + "…"
    button: dict[str, Any] = {
        "type": "button",
        "action_id": action_id,
        "text": {"type": "plain_text", "text": label, "emoji": True},
        "value": json.dumps(dict(value), ensure_ascii=False, separators=(",", ":")),
    }
    if primary:
        button["style"] = "primary"
    return button


def category_blocks(proposal: Mapping[str, Any] | None) -> tuple[str, list[dict[str, Any]]] | None:
    """``(fallback text, blocks)`` with one button per category of a pending proposal, or None without categories.

    The suggested category is the primary button; 취소 comes last. Each
    button's value carries the proposal id (and the category), so a click only
    ever answers this very proposal. When every event already has its own
    category there is one 추가 button instead. Pure.
    """
    labels = event_proposals.category_labels(proposal)
    proposal_id = str((proposal or {}).get("id") or "")
    if not labels or not proposal_id:
        return None
    if event_proposals.all_events_assigned(proposal):
        text = "일정마다 정한 카테고리로 추가할까요?"
        buttons = [_button(CONFIRM_ACTION, "추가", {"proposal_id": proposal_id, "confirm": True}, primary=True)]
    else:
        suggested = (proposal or {}).get("suggested_category")
        text = "카테고리를 골라주세요" + (f" (추천: {suggested})" if suggested in labels else "")
        buttons = [
            _button(
                PICK_ACTION.format(index=index),
                label,
                {"proposal_id": proposal_id, "category": label},
                primary=label == suggested,
            )
            for index, label in enumerate(labels, start=1)
        ]
    buttons.append(_button(CANCEL_ACTION, "취소", {"proposal_id": proposal_id, "cancel": True}))
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "block_id": f"{CATEGORY_ACTION_PREFIX}{proposal_id}", "elements": buttons},
    ]
    return text, blocks


def button_answer(value: Mapping[str, Any], proposal: Mapping[str, Any]) -> event_proposals.Answer | None:
    """What a button click means for ``proposal`` (already matched by id); None for a button it does not offer."""
    if value.get("cancel"):
        return event_proposals.Answer(event_proposals.NO)
    if value.get("confirm"):
        return event_proposals.Answer(event_proposals.YES) if event_proposals.all_events_assigned(proposal) else None
    category = value.get("category")
    if isinstance(category, str) and category in event_proposals.category_labels(proposal):
        return event_proposals.Answer(event_proposals.YES, category)
    return None


def quick_info_request(text: str, label: str | None = None) -> set[str]:
    """What a message asks the code-only shortcuts for: a subset of ``{"weather", "credits"}``.

    ``quick_info.parse_quick_info`` first (one or both, e.g. "날씨랑 토큰 좀
    말해봐"), then the single-purpose matchers (``credits.is_credit_query``,
    ``weather.is_weather_query``) so every phrasing they know still works.
    Empty: the message goes to the agent. Pure.
    """
    wanted = quick_info.parse_quick_info(text, label)
    if wanted:
        return wanted
    if credits.is_credit_query(text):
        return {quick_info.CREDITS}
    if weather.is_weather_query(text, label):
        return {quick_info.WEATHER}
    return set()


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
        briefing_relay: BriefingRelay | None = None,
        brief_bots: Mapping[str, "BriefBot"] | None = None,
        proposals: StateStore | None = None,
        create_events: EventCreator | None = None,
        bot_token: str | None = None,
        file_fetcher: FileFetcher | None = None,
        rng: random.Random | None = None,
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
        # 고뭉치's briefing on request: the relay (None means ``post_briefing``, looked up when used),
        # with the bots taking part: ``brief_bots``, else the bots of this process (``peers``,
        # filled by ``build_apps``), else 고뭉치 alone.
        self.briefing_relay = briefing_relay
        self.brief_bots = brief_bots
        self.peers: dict[str, SlackHandler] = {}
        # Calendar proposals waiting for "네" (the state file the propose tool writes),
        # and what creates their events once confirmed (the Calendar app adapter).
        self.proposals = proposals or StateStore(config.get_state_path())
        self.create_events = create_events or event_proposals.create_proposal_events
        # Photos are downloaded with this bot's own token (never logged).
        self.bot_token = bot_token if bot_token is not None else getattr(client, "token", None)
        self.file_fetcher = file_fetcher or download_slack_file
        # Picks the varied lines (placeholders, shortcut lead-ins); seed it for repeatable tests.
        self.rng = rng or random.Random()
        # Threads with an agent turn running or queued: a "네" sent meanwhile came
        # before its preview was shown, so it never confirms anything.
        self._turns: dict[str, int] = {}
        # Threads whose answer to a proposal is being applied right now: the proposal.
        self._confirming: dict[str, Mapping[str, Any]] = {}
        # Category buttons still clickable, per thread: (channel, message ts, proposal id).
        self._button_messages: dict[str, tuple[str, str, str]] = {}
        self._semaphore = semaphore or asyncio.Semaphore(max(1, max_concurrent))
        self._locks: dict[str, list[Any]] = {}
        self._seen = RecentKeys()
        self._refused = RecentKeys()

    # -- filtering

    def ignore_reason(self, event: Mapping[str, Any], source: str) -> str | None:
        """Why an event is skipped, or None if it should be handled."""
        if source == "dm" and event.get("channel_type") != "im":
            return "not_dm"
        subtype = event.get("subtype")
        # A photo sent in a DM arrives as a message with the file_share subtype;
        # edits, joins and every other subtype are still skipped.
        if subtype and subtype != FILE_SHARE_SUBTYPE:
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

    def wants_briefing(self, request: str) -> bool:
        """고뭉치 only: a bare mention / empty DM, or a short briefing request ("오늘 건너뛴 브리핑 좀 해봐").

        업뎃 and 일정 treat "브리핑" like any other message. Pure.
        """
        return self.persona == MUNGCHI and (not request.strip() or quick_info.is_briefing_request(request))

    def default_prompt(self) -> str:
        """What a bare mention asks 업뎃 / 일정 for (고뭉치's is the briefing, see ``wants_briefing``)."""
        return EMPTY_MENTION_PROMPTS.get(self.persona, "")

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
        # The user's text without this bot's mention; empty means 고뭉치's briefing
        # or ``default_prompt()``.
        request = strip_mention(str(event.get("text") or ""), self.bot_user_id)
        # Photos (and other files) attached to the message. Checked after the allow-list.
        files = [file for file in (event.get("files") or []) if isinstance(file, Mapping)]
        if files and await self._answer_files(channel, thread_ts, request, files):
            return
        # The answer to a calendar proposal waiting in this thread (a category,
        # "네", "아니요"): code only.
        if await self._answer_proposal(channel, thread_ts, request):
            return
        wanted = quick_info_request(request, weather.configured_label())
        if wanted >= {quick_info.WEATHER, quick_info.CREDITS}:
            await self._answer_weather_and_credits(channel, thread_ts)
            return
        if quick_info.CREDITS in wanted:
            await self._answer_credits(channel, thread_ts)
            return
        if quick_info.WEATHER in wanted:
            await self._answer_weather(channel, thread_ts)
            return
        # Anything else goes to an agent run, and replaces a calendar proposal
        # waiting in this thread: the agent re-proposes if needed, so a later
        # "네" can never confirm a preview the conversation has moved past.
        await self._replace_proposal(channel, thread_ts)
        if self.wants_briefing(request):
            await self._answer_briefing(
                channel, thread_ts, user=user, in_thread=bool(event.get("thread_ts")), dm=source == "dm"
            )
            return
        # Never the text itself: only its length and whether a shortcut word was in it.
        log.debug(
            "%s: 바로 답변에 해당하지 않아 에이전트에게 넘깁니다 (글자 수 %d, 날씨 포함: %s, 크레딧 포함: %s)",
            self.texts.label,
            len(request),
            "예" if "날씨" in request else "아니오",
            "예" if "크레딧" in request or "토큰" in request else "아니오",
        )
        await self._answer(channel, thread_ts, request or self.default_prompt())

    async def _refuse(self, channel: str, thread_ts: str, user: str) -> None:
        log.warning("%s 봇: 허용되지 않은 사용자(%s)의 요청을 거절했습니다.", self.texts.label, user)
        if self._refused.seen(f"{channel}:{thread_ts}"):
            return  # refuse once per thread
        await self._post(channel, thread_ts, REFUSAL_TEXT)

    # -- calendar proposals: a category, "네", "아니요" or a button (no agent turn, no LLM call)

    def _conversation_key(self, channel: str, thread_ts: str) -> str:
        return event_proposals.slack_conversation_key(self.persona, channel, thread_ts)

    def _drop_proposal(self, channel: str, thread_ts: str) -> None:
        try:
            if self.proposals.clear_pending_proposal(self._conversation_key(channel, thread_ts)):
                log.info("%s: 스레드 %s:%s 기다리던 캘린더 제안을 새 메시지로 대신합니다", self.texts.label, channel, thread_ts)
        except OSError as exc:
            log.warning("기다리던 캘린더 제안을 지우지 못했습니다: %s", safe_error(exc))

    async def _replace_proposal(self, channel: str, thread_ts: str) -> None:
        """Drop this thread's pending proposal (a new message replaces it) and close its buttons."""
        self._drop_proposal(channel, thread_ts)
        await self._retire_buttons(self._conversation_key(channel, thread_ts), BUTTONS_REPLACED_TEXT)

    async def _retire_buttons(self, key: str, text: str) -> None:
        """Replace this thread's category buttons (if any are still up) with ``text``."""
        entry = self._button_messages.pop(key, None)
        if entry is not None:
            channel, ts, _proposal_id = entry
            await self._update(channel, ts, text, blocks=[])

    async def _offer_category_buttons(self, channel: str, thread_ts: str) -> None:
        """After an agent turn: post the category buttons for the proposal it left in this thread, if any."""
        key = self._conversation_key(channel, thread_ts)
        try:
            pending = self.proposals.pending_proposal(key, utcnow())
        except OSError as exc:
            log.warning("캘린더 제안을 읽지 못했습니다: %s", safe_error(exc))
            return
        offer = category_blocks(pending)
        if pending is None or offer is None:
            return
        text, blocks = offer
        ts = await self._post(channel, thread_ts, text, blocks=blocks)
        if ts:
            self._button_messages[key] = (channel, ts, str(pending.get("id") or ""))

    async def _answer_proposal(self, channel: str, thread_ts: str, request: str) -> bool:
        """Apply an answer to this thread's pending calendar proposal. False: not such an answer.

        Only an explicit answer (``event_proposals.parse_answer``: a category
        by number or name, "네" for the suggested one, "아니요"; without
        categories a short "네" / "아니요") creates or cancels anything, and
        only in the thread (and bot) where the proposal was shown, before it
        expires. Nothing is created for a reply sent while this thread's agent
        turn was still running (it came before the preview). A reply that fits
        several categories is asked about once more and the proposal stays. A
        second answer while the first is being applied is dropped: answered once is enough.
        """
        key = self._conversation_key(channel, thread_ts)
        if self._turns.get(key):
            return False
        applying = self._confirming.get(key)
        if applying is not None:
            return event_proposals.parse_answer(request, applying).kind is not None
        try:
            pending = self.proposals.pending_proposal(key, utcnow())
        except OSError as exc:
            log.warning("캘린더 제안을 읽지 못했습니다: %s", safe_error(exc))
            return False
        if pending is None:
            return False
        answer = event_proposals.parse_answer(request, pending)
        if answer.kind is None:
            return False
        if answer.kind == event_proposals.CLARIFY:
            log.info("%s: 스레드 %s:%s 캘린더 제안의 카테고리를 다시 묻습니다 (에이전트 실행 없음)", self.texts.label, channel, thread_ts)
            await self._post_shortcut(channel, thread_ts, answer.message)
            return True

        async def deliver(reply: str) -> None:
            await self._post_shortcut(channel, thread_ts, reply)
            await self._retire_buttons(key, BUTTONS_ANSWERED_TEXT)

        await self._apply_answer(channel, thread_ts, pending, answer, deliver)
        return True

    async def _apply_answer(
        self,
        channel: str,
        thread_ts: str,
        pending: Mapping[str, Any],
        answer: event_proposals.Answer,
        deliver: Callable[[str], Awaitable[None]],
    ) -> bool:
        """Take ``pending`` out of the store and create its events (or cancel it), then ``deliver`` the reply.

        Shared by text answers and button clicks. The proposal is taken out on
        the event loop, and only if it is still that very proposal (same id),
        so it is never created twice. False: it was already gone.
        """
        key = self._conversation_key(channel, thread_ts)
        what = "취소" if answer.kind == event_proposals.NO else f"추가 ({answer.category or '일정마다 정한 카테고리'})"
        log.info("%s: 스레드 %s:%s 캘린더 제안 %s (에이전트 실행 없음)", self.texts.label, channel, thread_ts, what)
        self._confirming[key] = pending
        try:
            async with self._thread_lock(f"{self.persona}:{channel}:{thread_ts}"):
                try:
                    proposal = self.proposals.take_pending_proposal(key, utcnow(), proposal_id=pending.get("id"))
                    if proposal is None:
                        return False
                    reply = await asyncio.to_thread(
                        event_proposals.answer_text,
                        proposal,
                        answer.kind or "",
                        create=self.create_events,
                        category=answer.category,
                    )
                except Exception as exc:  # noqa: BLE001 - reported in Slack without details
                    _log_exception("캘린더 제안을 처리하지 못했습니다.", exc)
                    reply = CALENDAR_CRASH_TEXT.format(kind=type(exc).__name__)
            await deliver(reply)
        finally:
            self._confirming.pop(key, None)
        return True

    async def handle_action(self, body: Mapping[str, Any]) -> None:
        """A click on a category button (Bolt has already acked it).

        The allow-list comes first: anyone else gets an ephemeral refusal and
        nothing happens. The button's proposal id must be the one pending in
        this thread for this bot (not expired, not answered yet); otherwise the
        clicker sees "이미 처리됐거나 만료된 요청이에요". Then the same code as a
        text answer creates the events, and the button message is replaced by
        the result, so the buttons cannot be clicked twice.
        """
        user = str((body.get("user") or {}).get("id") or "")
        container = body.get("container") or {}
        message = body.get("message") or {}
        channel = str((body.get("channel") or {}).get("id") or container.get("channel_id") or "")
        message_ts = str(message.get("ts") or container.get("message_ts") or "")
        thread_ts = str(message.get("thread_ts") or container.get("thread_ts") or message_ts)
        if not channel or not message_ts:
            log.warning("%s 봇: 채널이나 메시지가 없는 버튼 클릭을 무시합니다.", self.texts.label)
            return
        if not self.is_allowed(user):
            log.warning("%s 봇: 허용되지 않은 사용자(%s)의 버튼 클릭을 거절했습니다.", self.texts.label, user)
            await self._ephemeral(channel, user, thread_ts, REFUSAL_TEXT)
            return
        actions = body.get("actions") or [{}]
        try:
            value = json.loads(str(actions[0].get("value") or ""))
        except (ValueError, AttributeError):
            value = {}
        value = value if isinstance(value, dict) else {}
        key = self._conversation_key(channel, thread_ts)
        if key in self._confirming:
            return  # a second click while the first is being applied: answered once is enough
        try:
            pending = self.proposals.pending_proposal(key, utcnow())
        except OSError as exc:
            log.warning("캘린더 제안을 읽지 못했습니다: %s", safe_error(exc))
            pending = None
        proposal_id = str(value.get("proposal_id") or "")
        answer = button_answer(value, pending) if pending is not None and proposal_id == pending.get("id") else None
        if answer is None:
            log.info("%s: 스레드 %s:%s 이미 처리됐거나 만료된 캘린더 버튼 클릭", self.texts.label, channel, thread_ts)
            await self._ephemeral(channel, user, thread_ts, STALE_ACTION_TEXT)
            return

        async def deliver(reply: str) -> None:
            self._button_messages.pop(key, None)  # this very message becomes the result
            chunks = to_slack_chunks(reply) or [CALENDAR_CRASH_TEXT.format(kind="빈 응답")]
            if not await self._update(channel, message_ts, chunks[0], blocks=[]):
                await self._post(channel, thread_ts, chunks[0])
            for chunk in chunks[1:]:
                await self._post(channel, thread_ts, chunk)

        if not await self._apply_answer(channel, thread_ts, pending, answer, deliver):
            await self._ephemeral(channel, user, thread_ts, STALE_ACTION_TEXT)

    # -- photos -> calendar (업뎃 and 일정)

    async def _answer_files(self, channel: str, thread_ts: str, request: str, files: list[Mapping[str, Any]]) -> bool:
        """A message with files. True: handled here; False: go on with its text as before.

        업뎃 / 일정: up to 5 images go to the agent with the text (``_answer``
        with ``files``); notes about skipped files are posted first. 고뭉치:
        an image gets ``IMAGE_REDIRECT_TEXT`` (no agent turn). Files without
        any image: a short note, then the text (if any) as an ordinary message.
        """
        selection = images.select_images(files)
        log.info(
            "%s: 스레드 %s:%s 파일 %d개 (읽을 사진 %d장)", self.texts.label, channel, thread_ts, len(files), len(selection.images)
        )
        if self.persona == MUNGCHI and selection.any_image:
            await self._post_shortcut(channel, thread_ts, IMAGE_REDIRECT_TEXT)
            return True
        if selection.notes:
            await self._post_shortcut(channel, thread_ts, "\n".join(selection.notes))
        if not selection.images:
            return not request
        await self._replace_proposal(channel, thread_ts)
        await self._answer(channel, thread_ts, request, files=selection.images)
        return True

    async def _full_file(self, file: Mapping[str, Any]) -> Mapping[str, Any]:
        """The file object with its download address (``files.info`` when the event left it out)."""
        if (file.get("url_private_download") or file.get("url_private")) and file.get("file_access") != "check_file_info":
            return file
        file_id = file.get("id")
        if not file_id:
            return file
        response = await self.client.files_info(file=file_id)
        full = response.get("file") if response is not None else None
        return full if isinstance(full, Mapping) else file

    async def _fetch_images(self, files: list[Mapping[str, Any]]) -> tuple[list[images.ImageInput], list[str]]:
        """Download and prepare the photos, in memory: ``(images for run_turn, Korean notes about failures)``."""
        prepared: list[images.ImageInput] = []
        notes: list[str] = []
        for file in files:
            name = images.short_name(file.get("name") or file.get("title"))
            try:
                full = await self._full_file(file)
                url = str(full.get("url_private_download") or full.get("url_private") or "")
                if not url or not self.bot_token:
                    raise FileDownloadError("파일 주소나 봇 토큰이 없어요")
                data = await self.file_fetcher(url, self.bot_token)
                prepared.append(await asyncio.to_thread(images.prepare_image, data, images.file_mimetype(full)))
                del data
            except images.ImageError as exc:
                notes.append(f"{name}: {exc}")
            except FileDownloadError as exc:
                log.warning("%s 봇: 사진을 받지 못했습니다: %s", self.texts.label, scrub(str(exc)))
                if exc.missing_scope:
                    notes.append(
                        f"{name}: 봇에 files:read 권한이 없어 사진을 받지 못했어요. "
                        f"slack_manifests/{self.texts.handle}.yaml대로 권한을 주고 앱을 다시 설치하세요."
                    )
                else:
                    notes.append(f"{name}: 사진을 받지 못했어요 ({scrub(str(exc))}).")
            except Exception as exc:  # noqa: BLE001 - one photo failing never stops the others
                log.warning("%s 봇: 사진을 받지 못했습니다: %s", self.texts.label, safe_error(exc, redact_urls=True))
                notes.append(f"{name}: 사진을 받지 못했어요 ({type(exc).__name__}).")
        log.info("%s: 사진 %d장 준비, %d장 실패", self.texts.label, len(prepared), len(notes))
        return prepared, notes

    # -- weather and credit shortcuts (no agent turn, no LLM call, no session)
    #
    # The reply goes into the thread; the thread -> session map is not touched.

    async def _shortcut_text(self, fetch: Callable[[], str], crash: str, failure: str) -> tuple[str, bool]:
        """``(fetch(), fetched)`` from a worker thread, or ``(crash, False)`` (with the exception's class name) when it fails or is empty.

        ``fetched`` is False for a failure line, so ``_with_lead`` leaves it bare.
        """
        try:
            text = await asyncio.to_thread(fetch)
        except Exception as exc:  # noqa: BLE001 - reported in Slack without details
            _log_exception(failure, exc)
            return crash.format(kind=type(exc).__name__), False
        if not (text or "").strip():
            return crash.format(kind="빈 응답"), False
        # The shortcut's own failure lines ("⚠️ ...", "🌤️ 서울 날씨: 가져오지 못했어요") get no cheerful lead-in.
        return text, not (text.lstrip().startswith("⚠️") or weather.FAILED_NOTE in text)

    async def _credit_text(self) -> tuple[str, bool]:
        return await self._shortcut_text(self.credit_text, CREDIT_CRASH_TEXT, "크레딧을 확인하지 못했습니다.")

    async def _weather_text(self) -> tuple[str, bool]:
        return await self._shortcut_text(self.weather_text, WEATHER_CRASH_TEXT, "날씨를 가져오지 못했습니다.")

    def _with_lead(self, leads: Mapping[str, Sequence[str]], body: str, fetched: bool) -> str:
        """``body`` (code's data lines, unchanged) under one short line in this bot's voice; a failure stays bare."""
        return f"{phrases.pick(leads[self.persona], self.rng)}\n{body}" if fetched else body

    async def _post_shortcut(self, channel: str, thread_ts: str, text: str) -> None:
        for chunk in to_slack_chunks(text):
            await self._post(channel, thread_ts, chunk)

    async def _answer_credits(self, channel: str, thread_ts: str) -> None:
        """Reply with the credit summary."""
        log.info("%s: 스레드 %s:%s 크레딧 바로 답변 (에이전트 실행 없음)", self.texts.label, channel, thread_ts)
        text, fetched = await self._credit_text()
        await self._post_shortcut(channel, thread_ts, self._with_lead(phrases.CREDIT_LEADS, text, fetched))

    async def _answer_weather(self, channel: str, thread_ts: str) -> None:
        """Reply with today's weather line."""
        log.info("%s: 스레드 %s:%s 날씨 바로 답변 (에이전트 실행 없음)", self.texts.label, channel, thread_ts)
        text, fetched = await self._weather_text()
        await self._post_shortcut(channel, thread_ts, self._with_lead(phrases.WEATHER_LEADS, text, fetched))

    async def _answer_weather_and_credits(self, channel: str, thread_ts: str) -> None:
        """Reply with the weather line, a blank line, then the credit summary (both fetched at once)."""
        log.info("%s: 스레드 %s:%s 날씨·크레딧 바로 답변 (에이전트 실행 없음)", self.texts.label, channel, thread_ts)
        (weather_text, weather_ok), (credit_text, credit_ok) = await asyncio.gather(self._weather_text(), self._credit_text())
        body = f"{weather_text}\n\n{credit_text}"
        await self._post_shortcut(channel, thread_ts, self._with_lead(phrases.BOTH_LEADS, body, weather_ok or credit_ok))

    # -- 고뭉치's briefing on request (the morning relay, here and now)

    def relay_bots(self) -> dict[str, "BriefBot"]:
        """The bots taking part in a relay started here: ``brief_bots``, else this process's bots, else 고뭉치 alone."""
        if self.brief_bots is not None:
            return dict(self.brief_bots)
        peers = self.peers or {self.persona: self}
        return {persona: BriefBot(persona, handler.client, handler.bot_user_id) for persona, handler in peers.items()}

    async def _answer_briefing(self, channel: str, thread_ts: str, *, user: str, in_thread: bool, dm: bool) -> None:
        """Today's briefing as the morning relay (``post_briefing``): 고뭉치, then 업뎃 and 일정 as their own bots.

        In a channel the three parts go into that channel: into the request's
        thread when it was asked in a thread, else at the top level (like the
        morning briefing). In 고뭉치's DM it is the DM relay for the person
        who asked: 고뭉치's part in this DM (in the thread when asked in one),
        업뎃's and 일정's in their own DMs with that person. 업뎃's run is a
        briefing run, so the Dropbox checkpoint moves. ``last_brief_date`` is
        never written: a briefing on request never stops the next scheduled one.
        """
        log.info(
            "%s: 스레드 %s:%s 브리핑 요청: 아침 브리핑과 같은 릴레이 브리핑을 보냅니다 (%s, 아침 브리핑 기록은 그대로 둡니다)",
            self.texts.label,
            channel,
            thread_ts,
            "DM" if dm else "채널",
        )
        thread = thread_ts if in_thread else None
        target = (
            BriefTarget(user=user, thread_ts=thread, mungchi_channel=channel)
            if dm
            else BriefTarget(channel=channel, thread_ts=thread)
        )
        async with self._thread_lock(f"{self.persona}:{channel}:{thread_ts}"):
            async with self._semaphore:
                try:
                    code = await (self.briefing_relay or post_briefing)(
                        self.relay_bots(),
                        [target],
                        run=self.run or run_turn,
                        sessions=self.sessions,
                        run_timeout=BRIEF_RUN_TIMEOUT_SECONDS,
                        rng=self.rng,
                    )
                except Exception as exc:  # noqa: BLE001 - reported in Slack without details
                    _log_exception("브리핑을 만들지 못했습니다.", exc)
                    await self._post(channel, thread_ts, BRIEF_CRASH_TEXT.format(kind=briefing.crash_kind(exc)))
                    return
        log.info("%s: 스레드 %s:%s 브리핑 %s", self.texts.label, channel, thread_ts, "완료" if code == 0 else "끝 (일부 실패, 위 로그 참고)")

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

    async def _answer(
        self, channel: str, thread_ts: str, prompt: str, *, files: list[Mapping[str, Any]] | None = None
    ) -> None:
        key = self._conversation_key(channel, thread_ts)
        # Counted before the first await, so a "네" arriving meanwhile sees it.
        self._turns[key] = self._turns.get(key, 0) + 1
        try:
            placeholder_text = phrases.pick_placeholder(self.persona, self.rng)
            placeholder = await self._post(channel, thread_ts, placeholder_text)
            # Messages in one thread run in order per bot; all bots share the cost cap.
            async with self._thread_lock(f"{self.persona}:{channel}:{thread_ts}"):
                async with self._semaphore:
                    await self._run_and_reply(
                        channel, thread_ts, placeholder, prompt, files=files, placeholder_text=placeholder_text
                    )
        finally:
            self._turns[key] -= 1
            if not self._turns[key]:
                del self._turns[key]

    async def _run_and_reply(
        self,
        channel: str,
        thread_ts: str,
        placeholder: str | None,
        prompt: str,
        *,
        files: list[Mapping[str, Any]] | None = None,
        placeholder_text: str = PLACEHOLDER_TEXT,
    ) -> None:
        persona, label = self.persona, self.texts.label
        # Photos: downloaded and shrunk here (in memory), sent with the text as image blocks.
        run_extra: dict[str, Any] = {}
        image_notes: list[str] = []
        if files:
            prepared, image_notes = await self._fetch_images(files)
            if not prepared:
                await self._finish(channel, thread_ts, placeholder, to_slack_chunks("\n".join([NO_IMAGE_READ_TEXT, *image_notes])))
                return
            run_extra["images"] = prepared
        resume = self.sessions.get(channel, thread_ts, persona=persona)
        # Only 고뭉치 shows progress ("→ 업뎃이에게 물어보는 중..."); 업뎃 and 일정
        # keep their placeholder until the answer replaces it.
        updater = (
            StatusUpdater(
                self.client, channel, placeholder, interval=self.status_interval, placeholder=placeholder_text
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
                prompt,
                resume=resume,
                on_status=updater,
                extra_system_prompt=SLACK_FORMAT_PROMPT,
                persona=persona,
                # A calendar proposal made in this turn waits for the answer in this thread, for this bot.
                conversation_key=self._conversation_key(channel, thread_ts),
                **run_extra,
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
        if image_notes:
            reply += "\n\n" + "\n".join(f"⚠️ {note}" for note in image_notes)
        if result.session_id:
            remember_session(self.sessions, channel, thread_ts, result.session_id, persona=persona)
        elif resume and result.failed:
            remember_session(self.sessions, channel, thread_ts, None, persona=persona)
            reply += "\n" + FRESH_SESSION_NOTE
        if result.failed:
            log.warning("%s 답을 끝내지 못했습니다: %s", josa(label, "이", "가"), scrub(result.error or ""))
        await self._finish(channel, thread_ts, placeholder, to_slack_chunks(reply))
        # A calendar proposal made in this turn: its category buttons go right under the answer.
        await self._offer_category_buttons(channel, thread_ts)
        log.info("%s: 스레드 %s:%s 답변 완료", label, channel, thread_ts)

    # -- Slack I/O (failures are logged, never raised)

    async def _finish(self, channel: str, thread_ts: str, placeholder: str | None, chunks: list[str]) -> None:
        chunks = chunks or [self.texts.empty_answer]
        first, rest = chunks[0], chunks[1:]
        if not (placeholder and await self._update(channel, placeholder, first)):
            await self._post(channel, thread_ts, first)
        for chunk in rest:
            await self._post(channel, thread_ts, chunk)

    async def _post(
        self, channel: str, thread_ts: str, text: str, *, blocks: list[dict[str, Any]] | None = None
    ) -> str | None:
        extra = {"blocks": blocks} if blocks is not None else {}
        try:
            response = await self.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=text, unfurl_links=False, unfurl_media=False, **extra
            )
        except Exception as exc:  # noqa: BLE001
            log.error("%s 봇: Slack 메시지를 보내지 못했습니다: %s", self.texts.label, describe_slack_error(exc, self.persona))
            return None
        return response.get("ts")

    async def _update(self, channel: str, ts: str, text: str, *, blocks: list[dict[str, Any]] | None = None) -> bool:
        """``chat_update``; ``blocks=[]`` removes the message's blocks (e.g. the buttons)."""
        extra = {"blocks": blocks} if blocks is not None else {}
        try:
            await self.client.chat_update(channel=channel, ts=ts, text=text, **extra)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 봇: Slack 메시지를 고치지 못했습니다: %s", self.texts.label, describe_slack_error(exc, self.persona))
            return False
        return True

    async def _ephemeral(self, channel: str, user: str, thread_ts: str, text: str) -> None:
        """A message only ``user`` sees (e.g. a refused or stale button click)."""
        try:
            await self.client.chat_postEphemeral(channel=channel, user=user, thread_ts=thread_ts, text=text)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 봇: 나만 보이는 메시지를 보내지 못했습니다: %s", self.texts.label, describe_slack_error(exc, self.persona))


# ---------------------------------------------------------------- Bolt app


def register_listeners(app: Any, handler: SlackHandler) -> None:
    """Wire Bolt events and the category buttons to ``handler``. Bolt acks each event before the listener runs."""

    @app.event("app_mention")
    async def on_app_mention(event: dict[str, Any], body: dict[str, Any], context: Any) -> None:
        handler.bot_user_id = handler.bot_user_id or getattr(context, "bot_user_id", None)
        await handler.handle_event(event, event_id=body.get("event_id"), source="mention")

    @app.event("message")
    async def on_message(event: dict[str, Any], body: dict[str, Any], context: Any) -> None:
        handler.bot_user_id = handler.bot_user_id or getattr(context, "bot_user_id", None)
        await handler.handle_event(event, event_id=body.get("event_id"), source="dm")

    # Category buttons under a calendar proposal. Actions are not acked by
    # Bolt itself: ack first, then create the events (EventKit may take a while).
    @app.action(CATEGORY_ACTION_RE)
    async def on_calendar_action(ack: Any, body: dict[str, Any]) -> None:
        await ack()
        await handler.handle_action(body)

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
        bot_token=bot.bot_token,
    )
    register_listeners(app, handler)
    return app, handler


def build_apps(
    cfg: config.SlackConfig,
    *,
    sessions: ThreadSessions | None = None,
    run: RunTurn | None = None,
) -> list[tuple[config.SlackBotConfig, Any, SlackHandler]]:
    """One Bolt app per configured bot, sharing the cost cap, the thread map and the bot-id set.

    Every handler also knows the others (``peers``), so a briefing asked of
    고뭉치 can hand over to 업뎃 and 일정 as their own bots.
    """
    semaphore = asyncio.Semaphore(max(1, cfg.max_concurrent))
    sessions = sessions or ThreadSessions(config.get_slack_threads_path())
    our_bot_user_ids: set[str] = set()
    peers: dict[str, SlackHandler] = {}
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
        handler.peers = peers
        peers[bot.persona] = handler
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
    bots: Any,
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
    greeting_generate: briefing.GreetingGenerate | None = None,
    rng: random.Random | None = None,
) -> str:
    """One look at the clock: send today's morning briefing if it is due (``briefing.brief_due``).

    ``bots``: the bots taking part (``BriefBot`` per persona; 고뭉치 first),
    or just 고뭉치's client. The briefing is the relay (``post_briefing``).
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
                bots,
                list(destinations),
                run=run,
                sessions=sessions,
                now=now,
                env=env,
                credit_fetch=credit_fetch,
                weather_fetch=weather_fetch,
                run_timeout=run_timeout,
                greeting_generate=greeting_generate,
                store=store,
                rng=rng,
            )
    except Exception as exc:  # noqa: BLE001 - one scrubbed line, the bots keep running
        log.error("아침 브리핑 실패 (%s): %s", today, safe_error(exc))
        return "failed"
    if code != 0:
        log.warning("아침 브리핑 실패 (%s): 일부를 만들지 못했거나 Slack에 올리지 못했습니다. 위 로그를 보세요.", today)
        return "failed"
    log.info("아침 브리핑을 보냈습니다 (%s).", today)
    return "sent"


async def morning_brief_loop(
    bots: Any,
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
            await tick(bots, targets, schedule=schedule, state=state, env=env, **options)
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
    """Print the schedule line and start ``scheduler`` with the running bots (``BriefBot``s); None when off. Never raises."""
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
        _bot, _app, handler = target
        print(
            f"아침 브리핑: {schedule.describe()} → {config.describe_brief_destination(cfg)} "
            f"(그 시각에 Mac이 잠자고 있었으면 깨어난 뒤 {schedule.catchup_text()} 보냅니다)",
            file=sys.stderr,
        )
        relay = {bot.persona: BriefBot(bot.persona, app.client, h.bot_user_id) for bot, app, h in bots}
        return asyncio.create_task(
            scheduler(
                relay,
                config.brief_destinations(cfg),
                schedule=schedule,
                sessions=handler.sessions,
                semaphore=handler._semaphore,
            )
        )
    except Exception as exc:  # noqa: BLE001 - the bots run without the briefing
        log.warning("아침 브리핑을 시작하지 못했습니다 (봇은 그대로 돕니다): %s", safe_error(exc))
        return None


async def record_code_version(
    code_version: Callable[[], str] | None = None,
    store: StateStore | None = None,
    now: datetime | None = None,
) -> str:
    """Print ``코드 버전: abc1234`` and write it with the start time to the state file. Never raises.

    ``service status`` compares it with the repository's commit, so a bot
    still running old code after ``git pull`` shows up there.
    """
    try:
        current = await asyncio.to_thread(code_version or version.code_version)
    except Exception as exc:  # noqa: BLE001 - a version label never stops the bots
        log.warning("코드 버전을 확인하지 못했습니다: %s", safe_error(exc))
        current = version.package_version()
    print(f"코드 버전: {current}", file=sys.stderr)
    try:
        (store or StateStore(config.get_state_path())).mark_running(current, now or utcnow())
    except OSError as exc:
        log.warning("실행 중인 코드 버전을 상태 파일에 기록하지 못했습니다 (봇은 그대로 돕니다): %s", safe_error(exc))
    return current


async def run_bots(
    cfg: config.SlackConfig,
    *,
    socket_factory: Callable[[Any, str], Any] | None = None,
    wait: Callable[[], Awaitable[None]] = _wait_forever,
    credit_alert: Callable[..., Awaitable[None]] | None = None,
    brief_scheduler: Callable[..., Awaitable[None]] | None = None,
    code_version: Callable[[], str] | None = None,
    store: StateStore | None = None,
) -> int:
    """Connect every configured bot over Socket Mode and serve them in one event loop.

    First the running code's version (``code_version``, default
    ``version.code_version``: the short git commit) is printed and written to
    the state file (``store``). While the bots run, ``credit_alert`` (default
    ``credit_alert_loop``) watches the Chat KHU credits and DMs the allowed
    users through 고뭉치's bot (or the first configured bot) when they run
    low, and ``brief_scheduler`` (default ``morning_brief_loop``, only with
    ``BRIEF_TIME`` and 고뭉치's bot) sends the morning relay briefing: 고뭉치
    first, then 업뎃 and 일정 (those that run) as their own bots.
    """
    if socket_factory is None:
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

        socket_factory = AsyncSocketModeHandler
    await record_code_version(code_version, store)
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


# ---------------------------------------------------------------- briefing (the relay)


@dataclass(frozen=True)
class BriefBot:
    """One bot taking part in the relay briefing: its persona, Slack client and user id (for ``<@mentions>``)."""

    persona: str
    client: Any
    user_id: str | None = None


@dataclass(frozen=True)
class BriefTarget:
    """Where one relay briefing goes.

    Channel mode (``channel``, a C…/G… id): all three bots post there, in
    ``thread_ts`` when set, else at the top level. DM mode (``user``): every
    bot posts in its own DM with that person; 고뭉치's part goes to
    ``mungchi_channel`` (default: the person, i.e. 고뭉치's DM), in ``thread_ts`` when set.
    """

    channel: str = ""
    user: str = ""
    thread_ts: str | None = None
    mungchi_channel: str | None = None

    @property
    def dm(self) -> bool:
        return bool(self.user)

    def place(self, persona: str) -> tuple[str, str | None]:
        """``(channel, thread_ts)`` where ``persona``'s bot posts its part."""
        if not self.dm:
            return self.channel, self.thread_ts
        if persona == MUNGCHI:
            return self.mungchi_channel or self.user, self.thread_ts
        return self.user, None


def brief_target(destination: "str | BriefTarget") -> BriefTarget:
    """A ``brief_destinations`` entry as a target: a member id (U…/W…) is DM mode, anything else a channel."""
    if isinstance(destination, BriefTarget):
        return destination
    return BriefTarget(user=destination) if destination[:1] in ("U", "W") else BriefTarget(channel=destination)


def _as_brief_bots(bots: Any) -> dict[str, BriefBot]:
    """``{persona: BriefBot}`` from a mapping (as given) or a bare client (고뭉치 alone)."""
    if isinstance(bots, Mapping):
        found = {persona: bot for persona, bot in bots.items() if bot is not None}
    else:
        found = {MUNGCHI: BriefBot(MUNGCHI, bots)}
    if MUNGCHI not in found:
        raise ValueError("the briefing needs 고뭉치's bot")
    return found


def _slack_error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    with contextlib.suppress(Exception):
        return str(response.get("error") or "") if response is not None else ""
    return ""


INVITE_HINT = "채널에서 `/invite @update @schedule` 하면 다음부터는 직접 보고해요."


def _missing_note(personas: Sequence[str]) -> str:
    """고뭉치's line about bots that are not set up (their part is left out)."""
    labels = "·".join(PERSONA_LABELS[persona] for persona in personas)
    envs = ", ".join(name for persona in personas for name in config.SLACK_BOT_ENV[persona])
    return f"{labels} 봇은 아직 설정되지 않아서 오늘 보고는 빠졌어요 (필요한 값: {envs})."


def _on_behalf_prefix(persona: str, code: str, dm: bool) -> str:
    """``(업뎃이가 채널에 없어서 대신 전해드려요)`` and the like, for a part 고뭉치 posts instead."""
    name = josa(call_name(persona), "이", "가")
    if code == "not_in_channel":
        return f"({name} 채널에 없어서 대신 전해드려요)"
    if dm:
        return f"({name} DM을 보내지 못해서 대신 전해드려요)"
    return f"({name} 메시지를 올리지 못해서 대신 전해드려요)"


def mungchi_closing(
    target: BriefTarget,
    bots: Mapping[str, BriefBot],
    reporters: Sequence[str],
    missing: Sequence[str],
    rng: random.Random | None = None,
) -> list[str]:
    """고뭉치's last lines: a note about bots that are not set up, then the hand-off.

    Channel mode mentions 업뎃 and 일정 (``<@U…>``, their names when the id is
    unknown); DM mode says they will report in their own DMs.
    """
    lines = [_missing_note(missing)] if missing else []
    if reporters:
        if target.dm:
            lines.append(phrases.pick(phrases.DM_HANDOFF_TEMPLATES, rng).format(names=phrases.teammate_names(reporters, topic=True)))
        else:
            ids = [bots[persona].user_id for persona in reporters]
            who = " ".join(f"<@{uid}>" for uid in ids) if all(ids) else phrases.teammate_names(reporters)
            lines.append(phrases.pick(phrases.HANDOFF_TEMPLATES, rng).format(bots=who))
    return lines


async def _post_chunks(client: Any, channel: str, chunks: list[str], thread_ts: str | None = None) -> tuple[str, str | None]:
    """Post one part: the first chunk (in ``thread_ts`` when set), the rest in the same thread.

    Returns ``(channel, ts)`` of the first message (for a DM, the DM channel
    Slack answers with). The first post's error is raised; a later chunk that
    fails is logged.
    """
    where = {"thread_ts": thread_ts} if thread_ts else {}
    first = await client.chat_postMessage(channel=channel, text=chunks[0], unfurl_links=False, unfurl_media=False, **where)
    root_channel = str(first.get("channel") or channel)
    root_ts = first.get("ts")
    for chunk in chunks[1:]:
        try:
            await client.chat_postMessage(
                channel=root_channel, thread_ts=thread_ts or root_ts, text=chunk, unfurl_links=False, unfurl_media=False
            )
        except Exception as exc:  # noqa: BLE001
            log.error("브리핑의 나머지를 스레드(%s)에 올리지 못했습니다: %s", channel, describe_slack_error(exc))
            break
    return root_channel, root_ts


@dataclass
class _RelayState:
    hinted: bool = False  # the /invite hint was posted once


async def _deliver_report(
    report: briefing.Report,
    target: BriefTarget,
    bots: Mapping[str, BriefBot],
    sessions: ThreadSessions | None,
    state: _RelayState,
) -> bool:
    """Post 업뎃's / 일정's part as its own bot; if that fails, 고뭉치 posts it on its behalf. False: not posted at all.

    The bot's own message (or, in a thread, that thread) is mapped to the
    report run's session, so a follow-up mention there continues with that bot.
    """
    persona = report.persona
    bot = bots[persona]
    channel, thread_ts = target.place(persona)
    chunks = to_slack_chunks(report.text) or [BOT_TEXTS[persona].empty_answer]
    try:
        root_channel, root_ts = await _post_chunks(bot.client, channel, chunks, thread_ts)
    except Exception as exc:  # noqa: BLE001 - 고뭉치 posts it instead
        code = _slack_error_code(exc)
        log.error(
            "%s 봇이 브리핑을 Slack(%s)에 올리지 못해 고뭉치가 대신 올립니다: %s",
            PERSONA_LABELS[persona],
            channel,
            describe_slack_error(exc, persona),
        )
        text = f"{_on_behalf_prefix(persona, code, target.dm)}\n{report.text}"
        if code == "not_in_channel" and not state.hinted:
            text += f"\n\n{INVITE_HINT}"
            state.hinted = True
        m_channel, m_thread = target.place(MUNGCHI)
        try:
            await _post_chunks(bots[MUNGCHI].client, m_channel, to_slack_chunks(text), m_thread)
        except Exception as again:  # noqa: BLE001
            log.error("고뭉치도 %s의 브리핑을 Slack(%s)에 올리지 못했습니다: %s", PERSONA_LABELS[persona], m_channel, describe_slack_error(again))
            return False
        return True
    if sessions is not None and report.session_id and root_ts:
        remember_session(sessions, root_channel, thread_ts or root_ts, report.session_id, persona=persona)
    return True


async def post_briefing(
    bots: Any,
    destinations: "str | BriefTarget | Sequence[str | BriefTarget]",
    *,
    run: RunTurn | None = None,
    sessions: ThreadSessions | None = None,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    on_status: Callable[[str], Any] | None = None,
    credit_fetch: Callable[[], credits.CreditReport] | None = None,
    weather_fetch: Callable[[], weather.WeatherReport] | None = None,
    run_timeout: float | None = None,
    greeting_generate: briefing.GreetingGenerate | None = None,
    store: StateStore | None = None,
    rng: random.Random | None = None,
) -> int:
    """Run today's relay briefing once and post it to every destination, in order: 고뭉치 → 업뎃 → 일정.

    ``bots``: ``{persona: BriefBot}`` (고뭉치 required; 업뎃 / 일정 when set
    up) or just 고뭉치's client. ``destinations``: a channel id (all three
    post there), member ids (each bot posts in its own DM with each person),
    or ``BriefTarget``s (e.g. a request's thread).

    Everything starts at once (``briefing.start_relay``); 고뭉치's part (its
    greeting, the weather and credit lines, the hand-off) goes out as soon as
    it is ready, then 업뎃's and 일정's, each as its own bot. A bot that is not
    set up is left out and 고뭉치 says so; a bot that cannot post (e.g.
    ``not_in_channel``) has 고뭉치 post its part instead, with the
    ``/invite`` hint once. 고뭉치's own message is not mapped to any session
    (a mention there starts fresh); 업뎃's and 일정's are mapped to their runs'
    sessions. ``last_brief_date`` is never touched here.

    Returns 0 when every part was made and reached every destination, else 1
    (details are logged).
    """
    relay_bots = _as_brief_bots(bots)
    if isinstance(destinations, (str, BriefTarget)):
        destinations = [destinations]
    targets = [brief_target(d) for d in destinations if d]
    if not targets:
        log.error("브리핑을 보낼 곳이 없습니다 (SLACK_BRIEF_CHANNEL, SLACK_ALLOWED_USER_IDS).")
        return 1
    reporters = [persona for persona in briefing.REPORTERS if persona in relay_bots]
    missing = [persona for persona in briefing.REPORTERS if persona not in relay_bots]
    rng = rng or random.Random()
    ok = True
    state = _RelayState()
    async with briefing.start_relay(
        personas=reporters,
        run=run or run_turn,
        now=now,
        env=env,
        slack=True,
        credit_fetch=credit_fetch,
        weather_fetch=weather_fetch,
        greeting_generate=greeting_generate,
        store=store,
        rng=rng,
        run_timeout=run_timeout,
        on_status=on_status,
    ) as relay:
        head = await relay.head()
        for target in targets:
            channel, thread_ts = target.place(MUNGCHI)
            text = head.text(mungchi_closing(target, relay_bots, reporters, missing, rng))
            try:
                await _post_chunks(relay_bots[MUNGCHI].client, channel, to_slack_chunks(text), thread_ts)
            except Exception as exc:  # noqa: BLE001 - the others still get theirs
                ok = False
                log.error("브리핑을 Slack(%s)에 올리지 못했습니다: %s", channel, describe_slack_error(exc))
        for persona in reporters:
            report = await relay.report(persona)
            ok = ok and not report.failed
            for target in targets:
                ok = await _deliver_report(report, target, relay_bots, sessions, state) and ok
    return 0 if ok else 1


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


async def _cli_brief_bots(cfg: config.SlackConfig) -> dict[str, BriefBot]:
    """고뭉치's client, plus 업뎃's and 일정's when their bot tokens are set (with their user ids, for the mentions)."""
    bots = {MUNGCHI: BriefBot(MUNGCHI, AsyncWebClient(token=cfg.bot_token))}
    for bot in cfg.bots:
        if bot.persona == MUNGCHI or not bot.bot_token:
            continue
        client = AsyncWebClient(token=bot.bot_token)
        user_id = None
        try:
            user_id = (await client.auth_test()).get("user_id")
        except Exception as exc:  # noqa: BLE001 - the hand-off then names the bot instead of mentioning it
            log.warning("%s 봇의 사용자 ID를 확인하지 못했습니다: %s", bot.label, describe_slack_error(exc, bot.persona))
        bots[bot.persona] = BriefBot(bot.persona, client, user_id)
    return bots


def post_briefing_cli(env: Mapping[str, str] | None = None) -> int:
    """``python -m mungchi --brief --slack``: send today's relay briefing now, where the morning briefing goes.

    ``SLACK_BRIEF_CHANNEL``, else a DM to every user in ``SLACK_ALLOWED_USER_IDS``.
    고뭉치 posts with ``SLACK_BOT_TOKEN``; 업뎃 and 일정 take part with their
    own bot tokens when those are set. Does not touch ``last_brief_date``: a
    test run never stops the scheduled one.
    """
    cfg = config.load_slack_config(env)
    problems = config.slack_brief_problems(cfg)
    if problems:
        _print_problems("브리핑을 Slack에 올릴 수 없습니다.", problems)
        return 1
    setup_logging()
    where = config.describe_brief_destination(cfg)

    async def deliver() -> int:
        sessions = ThreadSessions(config.get_slack_threads_path(env))
        return await post_briefing(
            await _cli_brief_bots(cfg), config.brief_destinations(cfg), sessions=sessions, env=env, on_status=_print_status
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
