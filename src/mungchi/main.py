"""CLI entry point: agent options per persona, the shared turn runner and the terminal front end."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, MutableMapping, Sequence, TextIO

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

from . import config
from .agents import (
    AGENT_LABELS,
    PERSONA_TOOLS,
    SUBAGENT_TOOL,
    SUBAGENT_TOOL_NAMES,
    TOOL_GATES,
    build_agents,
    build_direct_prompt,
    build_system_prompt,
    korean_date,
    with_now_line,
)
from .personas import DIRECT_PERSONAS, MUNGCHI, PERSONA_LABELS, PERSONAS, SCHEDULE, UPDATE, josa
from .tools import DATA_TOOLS, SERVER_NAME, build_server, data_tools, tools_named
from .tools.common import scrub

# Built-in tools that must never be reachable (belt and braces: ``tools``
# already limits the built-in set to the Agent tool, or to nothing at all for
# 업뎃 / 일정 answering directly).
BLOCKED_BUILTINS = [
    "Bash",
    "Write",
    "Edit",
    "NotebookEdit",
    "Read",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
]

# Runtime switches for the bundled Claude Code CLI.
CLI_ENV = {
    # No general-purpose/Explore/Plan agents: only 업뎃 and 일정 can be spawned.
    "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",
    # Run subagents in the foreground so a turn ends only after both reports
    # are in (parallel Agent calls in one message still run concurrently).
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    # Subagents must not spawn further subagents.
    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1",
    # Only a few small tools: load their schemas upfront instead of deferring.
    "ENABLE_TOOL_SEARCH": "false",
}

BRIEFING_PROMPT = "업뎃과 '일정' 에이전트에게 일을 맡겨서 오늘({today}) 브리핑을 해줘."
EXIT_WORDS = {"exit", "quit", "종료"}
# A positional prompt that is exactly this word starts the Slack bot.
SLACK_COMMAND = "slack"
# A first argument that is exactly this word is a background-service command
# (``python -m mungchi service install`` etc., see ``service.py``), not a question.
SERVICE_COMMAND = "service"

CHAT_GREETINGS = {
    MUNGCHI: "고뭉치 비서실입니다. 무엇을 도와드릴까요? (끝내려면 exit 또는 종료)",
    UPDATE: "업뎃입니다. 공저자 업데이트(Dropbox)를 확인해 드릴게요. (끝내려면 exit 또는 종료)",
    SCHEDULE: "'일정'입니다. 캘린더 일정을 확인해 드릴게요. (끝내려면 exit 또는 종료)",
}

ERROR_MESSAGES = {
    "authentication_failed": "인증에 실패했습니다.",
    "billing_error": "결제/사용 한도 문제로 요청이 거절되었습니다.",
    "rate_limit": "요청 한도에 걸렸습니다. 잠시 후 다시 시도하세요.",
    "invalid_request": "잘못된 요청입니다.",
    "server_error": "API 서버 오류입니다. 잠시 후 다시 시도하세요.",
    "unknown": "알 수 없는 오류가 발생했습니다.",
}

# ResultMessage subtypes that explain a failed turn (never shown raw; "success"
# with is_error=True means an API error and has no note of its own).
RESULT_SUBTYPE_NOTES = {
    "error_max_turns": "최대 턴 수 도달",
    "error_during_execution": "실행 중 오류",
    "error_max_budget_usd": "비용 한도 도달",
    "error_max_structured_output_retries": "구조화된 출력 재시도 한도 도달",
}

# Problems we can point at a setting for: wrong model / base URL, or wrong key.
_AUTH_ERROR_RE = re.compile(
    r"(?i)\b401\b|authenticat|unauthori[sz]ed|invalid[\s_-]*(?:x-)?api[\s_-]*key"
    r"|invalid[\s_-]*(?:bearer|auth(?:entication)?)[\s_-]*token"
)
_MODEL_ERROR_RE = re.compile(
    r"(?i)issue with the selected model|\b404\b|not_found_error"
    r"|model\b[^.\n]{0,60}?(?:not found|does not exist|may not exist|not available|unavailable|not supported)"
    r"|\b(?:unknown|invalid|unsupported)[\s_-]*model\b"
)
ERROR_HINTS = {"auth": config.AUTH_HINT, "model": config.MODEL_HINT}
# What a recognised problem is called when the SDK only says "unknown".
CATEGORY_MESSAGES = {"auth": ERROR_MESSAGES["authentication_failed"], "model": "모델 설정에 문제가 있습니다."}
# SDK error kinds that already name the problem.
KIND_CATEGORIES = {"authentication_failed": "auth", "invalid_request": "model"}
MAX_ERROR_DETAIL_CHARS = 200


StatusCallback = Callable[[str], "Awaitable[None] | None"]
# Returns the current time; injectable so tests can pin the per-turn time line.
Clock = Callable[[], datetime]


@dataclass
class TurnResult:
    """Outcome of one turn.

    ``error`` is a short Korean message, already scrubbed: a reason, then
    possibly an excerpt of the API's own error text and a "→ ..." hint, one
    per line.
    """

    text: str
    session_id: str | None = None
    failed: bool = False
    error: str | None = None


# ---------------------------------------------------------------- error messages


def _flatten(text: str | None) -> str:
    return " ".join((text or "").split())


def error_excerpt(text: str | None, limit: int = MAX_ERROR_DETAIL_CHARS) -> str:
    """One scrubbed line of the API's error text, at most ``limit`` characters."""
    flat = scrub(_flatten(text))  # scrub before cutting so no partial secret survives
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def error_category(text: str | None, status: int | None = None) -> str | None:
    """``"auth"`` or ``"model"`` when the error points at a setting, else None."""
    if status == 401:
        return "auth"
    if status == 404:
        return "model"
    text = text or ""
    if _AUTH_ERROR_RE.search(text):
        return "auth"
    if _MODEL_ERROR_RE.search(text):
        return "model"
    return None


def describe_error(
    reason: str, detail: str | None = None, *, kind: str | None = None, status: int | None = None
) -> str:
    """Korean error text: ``reason``, an excerpt of ``detail`` and a hint, one per line."""
    lines = [reason]
    excerpt = error_excerpt(detail)
    if excerpt:
        lines.append(excerpt)
    category = error_category(detail, status) or KIND_CATEGORIES.get(kind or "")
    if category:
        lines.append(ERROR_HINTS[category])
    return scrub("\n".join(lines))


def describe_assistant_error(kind: str, text: str | None = None) -> str:
    """Message for an assistant message flagged with an SDK error ``kind``.

    ``text`` is the API error text the CLI put in that message, e.g.
    "There's an issue with the selected model (...)".
    """
    reason = ERROR_MESSAGES.get(kind, ERROR_MESSAGES["unknown"])
    if kind not in ERROR_MESSAGES or kind in ("unknown", "invalid_request"):
        category = error_category(text)
        if category:
            reason = CATEGORY_MESSAGES[category]
    return describe_error(reason, text, kind=kind)


def describe_result_error(subtype: str | None, detail: str | None = None, status: int | None = None) -> str:
    """Message for a failed ``ResultMessage``; the bare subtype is never shown."""
    notes = [note for note in (RESULT_SUBTYPE_NOTES.get(subtype or ""), f"HTTP {status}" if status else "") if note]
    reason = "응답을 마치지 못했습니다" + (f"({', '.join(notes)})" if notes else "") + "."
    return describe_error(reason, detail, status=status)


def briefing_prompt(now: datetime | None = None, env: Mapping[str, str] | None = None) -> str:
    tz = config.get_timezone(env)
    now = (now or datetime.now(tz)).astimezone(tz)
    return BRIEFING_PROMPT.format(today=korean_date(now))


def stamp_prompt(prompt: str, clock: Clock | None = None, env: Mapping[str, str] | None = None) -> str:
    """``prompt`` with the current local time in front, e.g. ``[지금: 2026-10-06(화) 14:20 KST]``.

    The time goes in the user message, never the system prompt, so the system
    prompt stays byte-identical and cached across the turns of a conversation.
    ``clock`` defaults to the wall clock; the result is shown in ``TIMEZONE``.
    """
    tz = config.get_timezone(env)
    now = clock() if clock is not None else datetime.now(tz)
    return with_now_line(prompt, now.astimezone(tz))


def build_options(
    env: Mapping[str, str] | None = None,
    *,
    resume: str | None = None,
    extra_system_prompt: str = "",
    persona: str = MUNGCHI,
    briefing: bool = False,
) -> ClaudeAgentOptions:
    """The single place where agent options are built (CLI and Slack).

    ``persona="mungchi"`` is 고뭉치 with the 업뎃 / 일정 subagents.
    ``persona="update"`` or ``"schedule"`` makes 업뎃 or 일정 the top-level
    agent answering the user directly: it gets only its own data tools, no
    Agent tool, no subagents and no built-in tools.

    ``resume`` continues an earlier session by id; ``extra_system_prompt`` is
    appended to the system prompt (e.g. Slack formatting rules).

    ``briefing=True`` (only the ``--brief`` paths) builds this run's Dropbox
    tool in briefing mode: it looks at the time since the last briefing and
    moves that checkpoint. The mode is bound to the tool objects of these
    options, so it is fixed per run, never chosen by the model, and never
    leaks into other runs of the same process.

    Nothing here depends on the clock: the system prompts carry no date or
    time, so they can be cached. The time is added per turn (``stamp_prompt``).
    """
    if persona not in PERSONAS:
        raise ValueError(f"unknown persona: {persona!r}")
    if persona == MUNGCHI:
        system_prompt = build_system_prompt()
        # Built-in tool availability: only the subagent-invocation tool.
        builtin_tools = [SUBAGENT_TOOL]
        # 고뭉치's only pre-approved tool. Data tools are approved per subagent
        # by the PreToolUse hook (``tool_gate``) and denied for 고뭉치 itself.
        allowed_tools = [SUBAGENT_TOOL]
        disallowed_tools = list(BLOCKED_BUILTINS)
        server = build_server(data_tools(briefing=briefing))
        agents = build_agents()
    else:
        system_prompt = build_direct_prompt(persona)
        # No built-in tools at all, not even the Agent tool.
        builtin_tools = []
        # Only this persona's data tools exist (server) and are pre-approved;
        # the persona's PreToolUse gate denies everything else.
        allowed_tools = list(PERSONA_TOOLS[persona])
        disallowed_tools = [*BLOCKED_BUILTINS, SUBAGENT_TOOL]
        server = build_server(tools_named(allowed_tools, briefing=briefing))
        agents = None
    if extra_system_prompt.strip():
        system_prompt += "\n" + extra_system_prompt.strip() + "\n"
    return ClaudeAgentOptions(
        model=config.get_model(env),
        system_prompt=system_prompt,
        tools=builtin_tools,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        permission_mode="dontAsk",
        mcp_servers={SERVER_NAME: server},
        agents=agents,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[TOOL_GATES[persona]])]},
        # Ignore user/project settings files so the tool surface stays fixed.
        setting_sources=[],
        include_partial_messages=True,
        env=dict(CLI_ENV),
        resume=resume or None,
    )


class Renderer:
    """Streams the top-level agent's text to ``out`` and short status lines to ``status``.

    It also records what a non-terminal front end needs: the status lines,
    the answer text, the session id and a short Korean error. With
    ``echo=False`` nothing is written.
    """

    def __init__(self, out: TextIO | None = None, status: TextIO | None = None, *, echo: bool = True):
        self.out = out or sys.stdout
        self.status = status or sys.stderr
        self.echo = echo
        self._streamed_text = False
        self._at_line_start = True
        self._announced: set[str] = set()
        self.failed = False
        self.error: str | None = None
        # API error texts already shown, so the closing ResultMessage does not repeat them.
        self._reported_details: list[str] = []
        self.session_id: str | None = None
        self.status_lines: list[str] = []
        self._texts: list[str] = []
        # Index into ``_texts`` where the final answer starts: text written
        # before the last delegation ("업뎃에게 맡길게요") or, for 업뎃 / 일정
        # answering directly, before the last data tool call is not part of it.
        self._answer_start = 0

    def _write(self, text: str) -> None:
        if not text or not self.echo:
            return
        self.out.write(text)
        self.out.flush()
        self._at_line_start = text.endswith("\n")

    def _status_line(self, text: str) -> None:
        self.status_lines.append(text)
        if not self.echo:
            return
        if not self._at_line_start:
            self._write("\n")
        self.status.write(text + "\n")
        self.status.flush()

    def _fail(self, error: str, detail: str | None = None) -> None:
        self.failed = True
        if self.error is None:
            self.error = error
        flat = _flatten(detail)
        if flat:
            self._reported_details.append(flat)
        self._status_line("[오류] " + error)

    def _already_reported(self, detail: str | None) -> bool:
        flat = _flatten(detail)
        if not flat:
            return True
        return any(flat in seen or seen in flat for seen in self._reported_details)

    @property
    def answer(self) -> str:
        parts = self._texts[self._answer_start:] or self._texts
        return "\n\n".join(part.strip() for part in parts if part.strip())

    def result(self) -> TurnResult:
        return TurnResult(text=self.answer, session_id=self.session_id, failed=self.failed, error=self.error)

    def handle(self, message: Any) -> None:
        if isinstance(message, StreamEvent):
            self._on_stream_event(message)
        elif isinstance(message, AssistantMessage):
            self._on_assistant(message)
        elif isinstance(message, ResultMessage):
            self._on_result(message)
        elif isinstance(message, SystemMessage) and message.subtype == "init":
            session_id = (message.data or {}).get("session_id")
            if isinstance(session_id, str) and session_id:
                self.session_id = session_id

    def _on_stream_event(self, message: StreamEvent) -> None:
        if message.parent_tool_use_id:  # inside a subagent
            return
        event = message.event or {}
        if event.get("type") != "content_block_delta":
            return
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            self._streamed_text = True
            self._write(delta.get("text", ""))

    def _on_assistant(self, message: AssistantMessage) -> None:
        if message.parent_tool_use_id:  # subagent output reaches the user via 고뭉치
            return
        if message.session_id:
            self.session_id = message.session_id
        if message.error:
            # The text of an error message is the API's error, not an answer:
            # it is shown once, inside the [오류] lines, with a hint.
            raw = "\n".join(block.text for block in message.content if isinstance(block, TextBlock))
            self._fail(describe_assistant_error(message.error, raw), detail=raw)
        for block in message.content:
            if isinstance(block, TextBlock):
                if message.error:
                    continue
                self._texts.append(block.text)
                if not self._streamed_text:  # not already shown via partial messages
                    self._write(block.text)
            elif isinstance(block, ToolUseBlock) and block.name in DATA_TOOLS:
                self._answer_start = len(self._texts)
            elif isinstance(block, ToolUseBlock) and block.name in SUBAGENT_TOOL_NAMES:
                self._answer_start = len(self._texts)
                if block.id in self._announced:
                    continue
                self._announced.add(block.id)
                subagent = str(block.input.get("subagent_type", ""))
                label = AGENT_LABELS.get(subagent, subagent or "담당자")
                self._status_line(f"→ {label}에게 맡기는 중...")
        self._streamed_text = False

    def _on_result(self, message: ResultMessage) -> None:
        if message.session_id:
            self.session_id = message.session_id
        if not self._at_line_start:
            self._write("\n")
        if message.is_error:
            # On an API error the CLI reports subtype "success" with the error
            # text in ``result``; ``errors`` is filled for execution errors.
            detail = "; ".join(e for e in (message.errors or []) if e) or (message.result or "")
            status = getattr(message, "api_error_status", None)
            if self.failed and message.subtype not in RESULT_SUBTYPE_NOTES and self._already_reported(detail):
                return  # the assistant error above already said all of this
            self._fail(describe_result_error(message.subtype, detail, status), detail=detail)


async def _notify(on_status: StatusCallback, line: str) -> None:
    try:
        outcome = on_status(line)
        if inspect.isawaitable(outcome):
            await outcome
    except Exception as exc:  # noqa: BLE001 - a status display must never abort the turn
        print(f"[경고] 진행 상황을 전하지 못했습니다: {scrub(f'{type(exc).__name__}: {exc}')}", file=sys.stderr)


async def stream_turn(
    client: ClaudeSDKClient,
    prompt: str,
    renderer: Renderer,
    on_status: StatusCallback | None = None,
) -> TurnResult:
    """Send one prompt on an open client and feed the reply through ``renderer``."""
    await client.query(prompt)
    seen = len(renderer.status_lines)
    async for message in client.receive_response():
        renderer.handle(message)
        while seen < len(renderer.status_lines):
            if on_status is not None:
                await _notify(on_status, renderer.status_lines[seen])
            seen += 1
    return renderer.result()


async def run_turn(
    prompt: str,
    *,
    resume: str | None = None,
    on_status: StatusCallback | None = None,
    extra_system_prompt: str = "",
    renderer: Renderer | None = None,
    persona: str = MUNGCHI,
    clock: Clock | None = None,
    briefing: bool = False,
) -> TurnResult:
    """Run one turn of ``persona`` in a fresh session (or ``resume`` an earlier one).

    Shared by the CLI (one-shot, ``--brief``, ``--agent``) and the Slack bots.
    ``on_status`` receives the same status lines the CLI prints, e.g.
    "→ 업뎃에게 맡기는 중...". Without ``renderer`` nothing is printed.
    The prompt is sent with the current time in front (``stamp_prompt``).
    ``briefing=True`` only for briefing runs (see ``build_options``).
    """
    options = build_options(
        resume=resume, extra_system_prompt=extra_system_prompt, persona=persona, briefing=briefing
    )
    renderer = renderer or Renderer(echo=False)
    async with ClaudeSDKClient(options=options) as client:
        return await stream_turn(client, stamp_prompt(prompt, clock), renderer, on_status)


async def run_once(prompt: str, persona: str = MUNGCHI, *, briefing: bool = False) -> int:
    result = await run_turn(prompt, renderer=Renderer(), persona=persona, briefing=briefing)
    return 1 if result.failed else 0


async def run_chat(options: ClaudeAgentOptions, persona: str = MUNGCHI, *, clock: Clock | None = None) -> int:
    # One long-lived client keeps the whole conversation in a single session;
    # every line the user types is sent with the current time in front.
    print(CHAT_GREETINGS[persona])
    async with ClaudeSDKClient(options=options) as client:
        while True:
            try:
                line = await asyncio.to_thread(input, "\n나> ")
            except EOFError:
                print()
                break
            prompt = line.strip()
            if not prompt:
                continue
            if prompt.lower() in EXIT_WORDS:
                break
            print()
            await stream_turn(client, stamp_prompt(prompt, clock), Renderer())
    print(f"{PERSONA_LABELS[persona]}: 수고하셨습니다!")
    return 0


class KoreanHelpFormatter(argparse.RawDescriptionHelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):  # type: ignore[override]
        # argparse passes prefix="" when it builds a subcommand's prog; keep that.
        return super().add_usage(usage, actions, groups, prefix="사용법: " if prefix is None else prefix)


class KoreanArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(2, f"오류: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = KoreanArgumentParser(
        prog="mungchi",
        description="고뭉치 비서실: 업뎃(공저자 업데이트)과 '일정'(캘린더)에게 일을 맡기는 연구 비서.",
        epilog=(
            "예시:\n"
            "  python -m mungchi                       # 대화 모드\n"
            "  python -m mungchi --brief               # 오늘 브리핑\n"
            '  python -m mungchi "어제 공저자들이 뭐 고쳤어?"   # 질문 한 번\n'
            "  python -m mungchi slack                 # Slack 봇 실행 (Socket Mode)\n"
            "  python -m mungchi --brief --slack       # 오늘 브리핑을 Slack 채널에 올리기 (cron용)\n"
            '  python -m mungchi --agent update "누가 무슨 파일 고쳤어?"   # 업뎃에게 바로 묻기\n'
            "  python -m mungchi --agent schedule      # '일정'과 바로 대화\n"
            "  python -m mungchi --list-models         # 쓸 수 있는 모델 ID 확인 (MUNGCHI_MODEL 고르기)\n"
            "  python -m mungchi --credits             # Chat KHU 남은 크레딧과 이번 달 사용량 (LLM 호출 없음)\n"
            "  python -m mungchi --calendar-setup      # Mac 캘린더 앱 연결 (처음 한 번, 터미널에서)\n"
            "  python -m mungchi --dropbox-check --hours 72   # 업뎃이 Dropbox 변경을 못 찾을 때 원인 확인 (최근 72시간)\n"
            "  python -m mungchi service install       # (macOS) Slack 봇을 백그라운드 서비스로 설치 (로그인하면 자동 시작)\n"
            "  python -m mungchi service status        # (macOS) 서비스 상태와 최근 로그\n"
            "\n"
            "질문 자리에 slack 한 단어만 쓰면 질문이 아니라 Slack 봇 실행 명령으로 처리합니다.\n"
            "Slack 봇은 고뭉치·업뎃·일정 가운데 토큰을 넣은 봇이 한 프로세스에서 함께 켜집니다.\n"
            "Slack 설정(SLACK_BOT_TOKEN 등)은 README의 'Slack에서 부르기'를 보세요.\n"
            "\n"
            "마찬가지로 맨 앞에 service 한 단어를 쓰면 질문이 아니라 백그라운드 서비스 명령(macOS 전용)입니다:\n"
            "  service install | uninstall | start | stop | restart | status | logs [-f] [-n N]\n"
            "  service run은 서비스가 내부에서 쓰는 명령입니다(직접 실행하지 마세요).\n"
            "  자세히: python -m mungchi service --help, README의 '백그라운드로 실행하기'"
        ),
        formatter_class=KoreanHelpFormatter,
        add_help=False,
    )
    args_group = parser.add_argument_group("인자")
    args_group.add_argument(
        "question",
        nargs="?",
        metavar="질문",
        help=(
            "한 번만 물어볼 질문 (기본은 고뭉치에게). slack 이라고만 쓰면 Slack 봇을 실행하고, "
            "맨 앞의 service 는 백그라운드 서비스 명령입니다"
        ),
    )
    opts = parser.add_argument_group("옵션")
    opts.add_argument("--brief", action="store_true", help="오늘 브리핑을 한 번 받고 끝냅니다 (cron용)")
    opts.add_argument(
        "--slack",
        action="store_true",
        help="--brief와 함께 쓰면 브리핑을 터미널 대신 SLACK_BRIEF_CHANNEL 채널에 올립니다",
    )
    opts.add_argument(
        "--agent",
        choices=list(DIRECT_PERSONAS),
        metavar="{update,schedule}",
        help="고뭉치 대신 업뎃(update) 또는 '일정'(schedule)과 바로 이야기합니다 (질문 한 번 또는 대화 모드)",
    )
    opts.add_argument(
        "--list-models",
        action="store_true",
        help=(
            "Claude API(또는 ANTHROPIC_BASE_URL의 게이트웨이)에서 쓸 수 있는 모델 ID를 보여 주고 끝냅니다 "
            "(에이전트는 실행하지 않음)"
        ),
    )
    opts.add_argument(
        "--credits",
        action="store_true",
        help=(
            "Chat KHU(Mindlogic 게이트웨이)의 남은 크레딧, 이번 달 사용량과 모델별 사용량을 보여 주고 끝냅니다 "
            "(에이전트는 실행하지 않음, LLM 호출 없음)"
        ),
    )
    opts.add_argument(
        "--calendar-setup",
        action="store_true",
        help=(
            "Mac 캘린더 앱 접근을 허용하고, 읽을 캘린더와 오늘·내일 일정을 확인합니다 "
            "(macOS 터미널에서 한 번 실행, Claude API는 쓰지 않음)"
        ),
    )
    opts.add_argument(
        "--dropbox-check",
        action="store_true",
        help=(
            "업뎃이 Dropbox 변경을 못 찾을 때 원인을 확인합니다: 폴더·계정, 기간 안에 바뀐 파일마다 "
            "포함/제외 이유, 기간과 상관없이 최근에 바뀐 파일 (읽기 전용, Claude API는 쓰지 않고 "
            "브리핑 기준 시각도 바꾸지 않음)"
        ),
    )
    opts.add_argument(
        "--hours",
        type=int,
        metavar="N",
        help="--dropbox-check와 함께: 최근 N시간을 봅니다 (없으면 최근 24시간을 보고, 브리핑 기준 시각도 함께 보여 줌)",
    )
    opts.add_argument("-h", "--help", action="help", help="이 도움말을 보여 주고 끝냅니다")
    return parser


def drop_empty_claude_env(environ: MutableMapping[str, str] | None = None) -> None:
    """Remove empty Claude settings such as ``ANTHROPIC_API_KEY=`` from the environment.

    The bundled Claude Code CLI inherits this process's environment when the
    SDK starts it, so this must run before any agent turn. With a gateway
    (``ANTHROPIC_BASE_URL`` + ``ANTHROPIC_AUTH_TOKEN``) an empty-but-set
    ``ANTHROPIC_API_KEY`` copied from .env.example could interfere with the
    gateway's auth, and an empty ``ANTHROPIC_BASE_URL`` is not an address.
    """
    environ = os.environ if environ is None else environ
    for name in config.CLAUDE_ENV_VARS:
        if name in environ and not environ[name].strip():
            del environ[name]


def load_env() -> None:
    """Load ``.env`` (searched from the working directory upwards), then drop empty Claude settings.

    Runs before any SDK subprocess starts (CLI turns, Slack bots, --brief --slack,
    the background service): an empty ANTHROPIC_API_KEY= line must not get in
    the way of gateway auth.
    """
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(usecwd=True))
    drop_empty_claude_env()


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == SERVICE_COMMAND:
        # ``service <action> [...]`` has its own parser; like ``slack``, the bare
        # word in the question position is a command, never a question.
        from .service import service_main

        return service_main(argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.question == SERVICE_COMMAND:
        parser.error("service 명령은 맨 앞에 쓰고 다른 옵션과 함께 쓸 수 없습니다. 예: python -m mungchi service status")
    if args.list_models and (args.question or args.brief or args.slack or args.agent):
        parser.error("--list-models는 질문이나 다른 옵션(--brief, --slack, --agent, slack)과 함께 쓸 수 없습니다.")
    if args.credits and (
        args.question
        or args.brief
        or args.slack
        or args.agent
        or args.list_models
        or args.calendar_setup
        or args.dropbox_check
    ):
        parser.error(
            "--credits는 질문이나 다른 옵션(--brief, --slack, --agent, --list-models, --calendar-setup, "
            "--dropbox-check, slack)과 함께 쓸 수 없습니다."
        )
    if args.calendar_setup and (args.question or args.brief or args.slack or args.agent or args.list_models):
        parser.error(
            "--calendar-setup은 질문이나 다른 옵션(--brief, --slack, --agent, --list-models, slack)과 함께 쓸 수 없습니다."
        )
    if args.dropbox_check and (
        args.question or args.brief or args.slack or args.agent or args.list_models or args.calendar_setup
    ):
        parser.error(
            "--dropbox-check는 질문이나 다른 옵션(--brief, --slack, --agent, --list-models, --calendar-setup, slack)과 "
            "함께 쓸 수 없습니다."
        )
    if args.hours is not None and not args.dropbox_check:
        parser.error("--hours는 --dropbox-check와 함께 써야 합니다. 예: python -m mungchi --dropbox-check --hours 72")
    if args.hours is not None and args.hours <= 0:
        parser.error("--hours에는 1 이상의 정수(시간 수)를 적으세요. 예: --hours 72")
    start_slack_bot = args.question == SLACK_COMMAND
    if start_slack_bot and (args.brief or args.slack):
        parser.error("slack 명령은 --brief, --slack과 함께 쓸 수 없습니다.")
    if start_slack_bot and args.agent:
        parser.error("slack 명령은 --agent와 함께 쓸 수 없습니다. 업뎃·일정 봇은 토큰을 넣으면 slack 명령 하나로 함께 켜집니다.")
    if args.brief and args.question:
        parser.error("--brief와 질문은 함께 쓸 수 없습니다.")
    if args.slack and not args.brief:
        parser.error("--slack은 --brief와 함께 써야 합니다.")
    if args.brief and args.agent:
        parser.error("--brief는 고뭉치 전용이라 --agent와 함께 쓸 수 없습니다.")
    persona = args.agent or MUNGCHI
    label = PERSONA_LABELS[persona]

    load_env()

    try:
        if args.list_models:
            from .model_list import list_models

            return list_models()
        if args.credits:
            from .credits import run_credits_cli

            return run_credits_cli()
        if args.calendar_setup:
            from .calendar_setup import run_calendar_setup

            return run_calendar_setup()
        if args.dropbox_check:
            from .dropbox_check import run_dropbox_check

            return run_dropbox_check(hours=args.hours)
        if start_slack_bot:
            from .slack_bot import run_bot_cli

            return run_bot_cli()
        if args.brief and args.slack:
            from .slack_bot import post_briefing_cli

            return post_briefing_cli()
        if args.brief:
            # The only briefing run in the terminal: it alone moves the Dropbox checkpoint.
            return asyncio.run(run_once(briefing_prompt(), briefing=True))
        if args.question:
            return asyncio.run(run_once(args.question, persona))
        return asyncio.run(run_chat(build_options(persona=persona), persona))
    except KeyboardInterrupt:
        print(f"\n{label}: 중단했습니다.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - show a clean Korean message, never a token
        print(f"[오류] {josa(label, '을', '를')} 실행하지 못했습니다: {scrub(f'{type(exc).__name__}: {exc}')}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
