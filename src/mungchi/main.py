"""CLI entry point: options for 고뭉치, the shared turn runner and the terminal front end."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, Sequence, TextIO

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
    SUBAGENT_TOOL,
    SUBAGENT_TOOL_NAMES,
    build_agents,
    build_system_prompt,
    korean_date,
    tool_gate,
)
from .tools import SERVER_NAME, build_server
from .tools.common import scrub

# Built-in tools that must never be reachable (belt and braces: ``tools``
# already limits the built-in set to the Agent tool).
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
    # No general-purpose/Explore/Plan agents: only 업뎃 and 빠릿 can be spawned.
    "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",
    # Run subagents in the foreground so a turn ends only after both reports
    # are in (parallel Agent calls in one message still run concurrently).
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    # Subagents must not spawn further subagents.
    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1",
    # Only three small tools: load their schemas upfront instead of deferring.
    "ENABLE_TOOL_SEARCH": "false",
}

BRIEFING_PROMPT = "업뎃과 빠릿에게 일을 맡겨서 오늘({today}) 브리핑을 해줘."
EXIT_WORDS = {"exit", "quit", "종료"}
# A positional prompt that is exactly this word starts the Slack bot.
SLACK_COMMAND = "slack"

ERROR_MESSAGES = {
    "authentication_failed": "인증에 실패했습니다. ANTHROPIC_API_KEY 또는 Claude 로그인을 확인하세요.",
    "billing_error": "결제/사용 한도 문제로 요청이 거절되었습니다.",
    "rate_limit": "요청 한도에 걸렸습니다. 잠시 후 다시 시도하세요.",
    "invalid_request": "잘못된 요청입니다. MUNGCHI_MODEL 값을 확인하세요.",
    "server_error": "API 서버 오류입니다. 잠시 후 다시 시도하세요.",
    "unknown": "알 수 없는 오류가 발생했습니다.",
}


StatusCallback = Callable[[str], "Awaitable[None] | None"]


@dataclass
class TurnResult:
    """Outcome of one turn. ``error`` is a short Korean message, already scrubbed."""

    text: str
    session_id: str | None = None
    failed: bool = False
    error: str | None = None


def briefing_prompt(now: datetime | None = None, env: Mapping[str, str] | None = None) -> str:
    tz = config.get_timezone(env)
    now = (now or datetime.now(tz)).astimezone(tz)
    return BRIEFING_PROMPT.format(today=korean_date(now))


def build_options(
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
    *,
    resume: str | None = None,
    extra_system_prompt: str = "",
) -> ClaudeAgentOptions:
    """The single place where 고뭉치's options are built (CLI and Slack).

    ``resume`` continues an earlier session by id; ``extra_system_prompt`` is
    appended to 고뭉치's system prompt (e.g. Slack formatting rules).
    """
    tz = config.get_timezone(env)
    now = (now or datetime.now(tz)).astimezone(tz)
    system_prompt = build_system_prompt(now, config.get_timezone_name(env))
    if extra_system_prompt.strip():
        system_prompt += "\n" + extra_system_prompt.strip() + "\n"
    return ClaudeAgentOptions(
        model=config.get_model(env),
        system_prompt=system_prompt,
        # Built-in tool availability: only the subagent-invocation tool.
        tools=[SUBAGENT_TOOL],
        # 고뭉치's only pre-approved tool. Data tools are approved per subagent
        # by the PreToolUse hook (``tool_gate``) and denied for 고뭉치 itself.
        allowed_tools=[SUBAGENT_TOOL],
        disallowed_tools=list(BLOCKED_BUILTINS),
        permission_mode="dontAsk",
        mcp_servers={SERVER_NAME: build_server()},
        agents=build_agents(),
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[tool_gate])]},
        # Ignore user/project settings files so the tool surface stays fixed.
        setting_sources=[],
        include_partial_messages=True,
        env=dict(CLI_ENV),
        resume=resume or None,
    )


class Renderer:
    """Streams 고뭉치's text to ``out`` and short status lines to ``status``.

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
        self.session_id: str | None = None
        self.status_lines: list[str] = []
        self._texts: list[str] = []
        # Index into ``_texts`` where the final answer starts: text written
        # before the last delegation ("업뎃에게 맡길게요") is not part of it.
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

    def _fail(self, error: str) -> None:
        self.failed = True
        if self.error is None:
            self.error = error
        self._status_line("[오류] " + error)

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
            self._fail(ERROR_MESSAGES.get(message.error, ERROR_MESSAGES["unknown"]))
        for block in message.content:
            if isinstance(block, TextBlock):
                if not message.error:  # raw API error text is not an answer
                    self._texts.append(block.text)
                if not self._streamed_text:  # not already shown via partial messages
                    self._write(block.text)
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
            details = "; ".join(message.errors or []) or message.subtype
            self._fail(f"응답을 마치지 못했습니다: {scrub(details)}")


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
) -> TurnResult:
    """Run one turn of 고뭉치 in a fresh session (or ``resume`` an earlier one).

    Shared by the CLI (one-shot, ``--brief``) and the Slack front end.
    ``on_status`` receives the same status lines the CLI prints, e.g.
    "→ 업뎃에게 맡기는 중...". Without ``renderer`` nothing is printed.
    """
    options = build_options(resume=resume, extra_system_prompt=extra_system_prompt)
    renderer = renderer or Renderer(echo=False)
    async with ClaudeSDKClient(options=options) as client:
        return await stream_turn(client, prompt, renderer, on_status)


async def run_once(prompt: str) -> int:
    result = await run_turn(prompt, renderer=Renderer())
    return 1 if result.failed else 0


async def run_chat(options: ClaudeAgentOptions) -> int:
    # One long-lived client keeps the whole conversation in a single session.
    print("고뭉치 비서실입니다. 무엇을 도와드릴까요? (끝내려면 exit 또는 종료)")
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
            await stream_turn(client, prompt, Renderer())
    print("고뭉치: 수고하셨습니다!")
    return 0


class KoreanHelpFormatter(argparse.RawDescriptionHelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):  # type: ignore[override]
        return super().add_usage(usage, actions, groups, prefix="사용법: ")


class KoreanArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(2, f"오류: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = KoreanArgumentParser(
        prog="mungchi",
        description="고뭉치 비서실: 업뎃(공저자 업데이트)과 빠릿(일정)에게 일을 맡기는 연구 비서.",
        epilog=(
            "예시:\n"
            "  python -m mungchi                       # 대화 모드\n"
            "  python -m mungchi --brief               # 오늘 브리핑\n"
            '  python -m mungchi "어제 공저자들이 뭐 고쳤어?"   # 질문 한 번\n'
            "  python -m mungchi slack                 # Slack 봇 실행 (Socket Mode)\n"
            "  python -m mungchi --brief --slack       # 오늘 브리핑을 Slack 채널에 올리기 (cron용)\n"
            "\n"
            "질문 자리에 slack 한 단어만 쓰면 질문이 아니라 Slack 봇 실행 명령으로 처리합니다.\n"
            "Slack 설정(SLACK_BOT_TOKEN 등)은 README의 'Slack에서 고뭉치 부르기'를 보세요."
        ),
        formatter_class=KoreanHelpFormatter,
        add_help=False,
    )
    args_group = parser.add_argument_group("인자")
    args_group.add_argument(
        "question",
        nargs="?",
        metavar="질문",
        help="고뭉치에게 한 번만 물어볼 질문. slack 이라고만 쓰면 Slack 봇을 실행합니다",
    )
    opts = parser.add_argument_group("옵션")
    opts.add_argument("--brief", action="store_true", help="오늘 브리핑을 한 번 받고 끝냅니다 (cron용)")
    opts.add_argument(
        "--slack",
        action="store_true",
        help="--brief와 함께 쓰면 브리핑을 터미널 대신 SLACK_BRIEF_CHANNEL 채널에 올립니다",
    )
    opts.add_argument("-h", "--help", action="help", help="이 도움말을 보여 주고 끝냅니다")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    start_slack_bot = args.question == SLACK_COMMAND
    if start_slack_bot and (args.brief or args.slack):
        parser.error("slack 명령은 --brief, --slack과 함께 쓸 수 없습니다.")
    if args.brief and args.question:
        parser.error("--brief와 질문은 함께 쓸 수 없습니다.")
    if args.slack and not args.brief:
        parser.error("--slack은 --brief와 함께 써야 합니다.")

    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(usecwd=True))
    # An empty ANTHROPIC_API_KEY= line copied from .env.example must not
    # shadow a `claude` CLI login.
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        os.environ.pop("ANTHROPIC_API_KEY", None)

    try:
        if start_slack_bot:
            from .slack_bot import run_bot_cli

            return run_bot_cli()
        if args.brief and args.slack:
            from .slack_bot import post_briefing_cli

            return post_briefing_cli()
        if args.brief:
            return asyncio.run(run_once(briefing_prompt()))
        if args.question:
            return asyncio.run(run_once(args.question))
        return asyncio.run(run_chat(build_options()))
    except KeyboardInterrupt:
        print("\n고뭉치: 중단했습니다.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - show a clean Korean message, never a token
        print(f"[오류] 고뭉치를 실행하지 못했습니다: {scrub(f'{type(exc).__name__}: {exc}')}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
