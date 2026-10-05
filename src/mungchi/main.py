"""CLI entry point: options for 뭉치 and the terminal front end."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime
from typing import Any, Mapping, Sequence, TextIO

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    StreamEvent,
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

ERROR_MESSAGES = {
    "authentication_failed": "인증에 실패했습니다. ANTHROPIC_API_KEY 또는 Claude 로그인을 확인하세요.",
    "billing_error": "결제/사용 한도 문제로 요청이 거절되었습니다.",
    "rate_limit": "요청 한도에 걸렸습니다. 잠시 후 다시 시도하세요.",
    "invalid_request": "잘못된 요청입니다. MUNGCHI_MODEL 값을 확인하세요.",
    "server_error": "API 서버 오류입니다. 잠시 후 다시 시도하세요.",
    "unknown": "알 수 없는 오류가 발생했습니다.",
}


def build_options(
    env: Mapping[str, str] | None = None, now: datetime | None = None
) -> ClaudeAgentOptions:
    tz = config.get_timezone(env)
    now = (now or datetime.now(tz)).astimezone(tz)
    return ClaudeAgentOptions(
        model=config.get_model(env),
        system_prompt=build_system_prompt(now, config.get_timezone_name(env)),
        # Built-in tool availability: only the subagent-invocation tool.
        tools=[SUBAGENT_TOOL],
        # 뭉치's only pre-approved tool. Data tools are approved per subagent
        # by the PreToolUse hook (``tool_gate``) and denied for 뭉치 itself.
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
    )


class Renderer:
    """Streams 뭉치's text to ``out`` and short status lines to ``status``."""

    def __init__(self, out: TextIO | None = None, status: TextIO | None = None):
        self.out = out or sys.stdout
        self.status = status or sys.stderr
        self._streamed_text = False
        self._at_line_start = True
        self._announced: set[str] = set()
        self.failed = False

    def _write(self, text: str) -> None:
        if not text:
            return
        self.out.write(text)
        self.out.flush()
        self._at_line_start = text.endswith("\n")

    def _status_line(self, text: str) -> None:
        if not self._at_line_start:
            self._write("\n")
        self.status.write(text + "\n")
        self.status.flush()

    def handle(self, message: Any) -> None:
        if isinstance(message, StreamEvent):
            self._on_stream_event(message)
        elif isinstance(message, AssistantMessage):
            self._on_assistant(message)
        elif isinstance(message, ResultMessage):
            self._on_result(message)

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
        if message.parent_tool_use_id:  # subagent output reaches the user via 뭉치
            return
        if message.error:
            self.failed = True
            self._status_line("[오류] " + ERROR_MESSAGES.get(message.error, ERROR_MESSAGES["unknown"]))
        for block in message.content:
            if isinstance(block, TextBlock):
                if not self._streamed_text:  # not already shown via partial messages
                    self._write(block.text)
            elif isinstance(block, ToolUseBlock) and block.name in SUBAGENT_TOOL_NAMES:
                if block.id in self._announced:
                    continue
                self._announced.add(block.id)
                subagent = str(block.input.get("subagent_type", ""))
                label = AGENT_LABELS.get(subagent, subagent or "담당자")
                self._status_line(f"→ {label}에게 맡기는 중...")
        self._streamed_text = False

    def _on_result(self, message: ResultMessage) -> None:
        if not self._at_line_start:
            self._write("\n")
        if message.is_error:
            self.failed = True
            details = "; ".join(message.errors or []) or message.subtype
            self._status_line(f"[오류] 응답을 마치지 못했습니다: {scrub(details)}")


async def run_turn(client: ClaudeSDKClient, prompt: str, renderer: Renderer) -> None:
    await client.query(prompt)
    async for message in client.receive_response():
        renderer.handle(message)


async def run_once(prompt: str, options: ClaudeAgentOptions) -> int:
    renderer = Renderer()
    async with ClaudeSDKClient(options=options) as client:
        await run_turn(client, prompt, renderer)
    return 1 if renderer.failed else 0


async def run_chat(options: ClaudeAgentOptions) -> int:
    print("뭉치 비서실입니다. 무엇을 도와드릴까요? (끝내려면 exit 또는 종료)")
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
            await run_turn(client, prompt, Renderer())
    print("뭉치: 수고하셨습니다!")
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
        description="뭉치 비서실: 업뎃(공저자 업데이트)과 빠릿(일정)에게 일을 맡기는 연구 비서.",
        epilog=(
            "예시:\n"
            "  python -m mungchi                       # 대화 모드\n"
            "  python -m mungchi --brief               # 오늘 브리핑\n"
            '  python -m mungchi "어제 공저자들이 뭐 고쳤어?"   # 질문 한 번'
        ),
        formatter_class=KoreanHelpFormatter,
        add_help=False,
    )
    args_group = parser.add_argument_group("인자")
    args_group.add_argument("question", nargs="?", metavar="질문", help="뭉치에게 한 번만 물어볼 질문")
    opts = parser.add_argument_group("옵션")
    opts.add_argument("--brief", action="store_true", help="오늘 브리핑을 한 번 받고 끝냅니다 (cron용)")
    opts.add_argument("-h", "--help", action="help", help="이 도움말을 보여 주고 끝냅니다")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.brief and args.question:
        parser.error("--brief와 질문은 함께 쓸 수 없습니다.")

    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(usecwd=True))
    # An empty ANTHROPIC_API_KEY= line copied from .env.example must not
    # shadow a `claude` CLI login.
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        os.environ.pop("ANTHROPIC_API_KEY", None)
    options = build_options()

    try:
        if args.brief:
            today = korean_date(datetime.now(config.get_timezone()))
            return asyncio.run(run_once(BRIEFING_PROMPT.format(today=today), options))
        if args.question:
            return asyncio.run(run_once(args.question, options))
        return asyncio.run(run_chat(options))
    except KeyboardInterrupt:
        print("\n뭉치: 중단했습니다.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - show a clean Korean message, never a token
        print(f"[오류] 뭉치를 실행하지 못했습니다: {scrub(f'{type(exc).__name__}: {exc}')}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
