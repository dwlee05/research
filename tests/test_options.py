from __future__ import annotations

import asyncio
import io
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

from mungchi import main as main_module
from mungchi.agents import (
    AGENT_LABELS,
    MUNGCHI_SYSTEM_PROMPT,
    PPALIT,
    UPDEOT,
    gate_decision,
    tool_gate,
)
from mungchi.main import BLOCKED_BUILTINS, Renderer, TurnResult, build_options, build_parser, main, run_turn
from mungchi.tools import CALENDAR_TOOL, DATA_TOOLS, DROPBOX_TOOL, OVERLEAF_TOOL, SERVER_NAME

NOW = datetime(2026, 10, 5, 8, 0, tzinfo=ZoneInfo("Asia/Seoul"))


def options() -> ClaudeAgentOptions:
    return build_options(env={}, now=NOW)


def test_agents_have_ascii_keys_and_own_tools_only():
    opts = options()
    assert set(opts.agents) == {"updeot", "ppalit"}
    assert all(key.isascii() for key in opts.agents)
    updeot, ppalit = opts.agents["updeot"], opts.agents["ppalit"]
    assert isinstance(updeot, AgentDefinition) and isinstance(ppalit, AgentDefinition)
    assert sorted(updeot.tools) == sorted([DROPBOX_TOOL, OVERLEAF_TOOL])
    assert ppalit.tools == [CALENDAR_TOOL]
    for agent in (updeot, ppalit):
        assert agent.model == "inherit"
        assert all(t.startswith(f"mcp__{SERVER_NAME}__") for t in agent.tools)
    assert DROPBOX_TOOL == "mcp__mungchi__check_dropbox_updates"


def test_main_agent_can_only_use_the_agent_tool():
    opts = options()
    assert opts.tools == ["Agent"]
    assert opts.allowed_tools == ["Agent"]
    for builtin in ("Bash", "Write", "Edit"):
        assert builtin in opts.disallowed_tools
        assert builtin not in opts.tools
    assert not set(DATA_TOOLS) & set(opts.allowed_tools)
    assert opts.permission_mode == "dontAsk"
    assert opts.setting_sources == []
    assert opts.env["CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS"] == "1"
    assert set(BLOCKED_BUILTINS) <= set(opts.disallowed_tools)


def test_data_tools_are_gated_per_subagent():
    def decision(tool, agent_type=None, agent_id=None, tool_input=None):
        out = gate_decision(tool, tool_input or {}, agent_type, agent_id)
        return out.get("hookSpecificOutput", {}).get("permissionDecision")

    # Main thread (no agent_id) can never call data tools.
    for tool in DATA_TOOLS:
        assert decision(tool) == "deny"
    # Each subagent may call only its own tools.
    assert decision(DROPBOX_TOOL, UPDEOT, "a1") == "allow"
    assert decision(OVERLEAF_TOOL, UPDEOT, "a1") == "allow"
    assert decision(CALENDAR_TOOL, UPDEOT, "a1") == "deny"
    assert decision(CALENDAR_TOOL, PPALIT, "a2") == "allow"
    assert decision(DROPBOX_TOOL, PPALIT, "a2") == "deny"
    assert decision(DROPBOX_TOOL, "general-purpose", "a3") == "deny"
    # Only 업뎃 and 빠릿 can be spawned.
    assert decision("Agent", tool_input={"subagent_type": "updeot"}) is None
    assert decision("Agent", tool_input={"subagent_type": "general-purpose"}) == "deny"


def test_tool_gate_hook_is_registered_and_async():
    opts = options()
    [matcher] = opts.hooks["PreToolUse"]
    assert matcher.matcher is None
    assert tool_gate in matcher.hooks
    out = asyncio.run(tool_gate({"tool_name": CALENDAR_TOOL, "tool_input": {}}, "t1", None))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_model_defaults_and_env_override():
    assert options().model == "claude-opus-5-5"
    assert build_options(env={"MUNGCHI_MODEL": "custom-model"}, now=NOW).model == "custom-model"


def test_system_prompt_mentions_date_and_three_sections():
    prompt = options().system_prompt
    assert "2026-10-05 (월요일)" in prompt
    for heading in ("① 공저자 업데이트", "② 일정", "③ 오늘 챙길 것"):
        assert heading in prompt
    assert "{today}" in MUNGCHI_SYSTEM_PROMPT


def test_mcp_server_is_in_process_sdk_server():
    server = options().mcp_servers[SERVER_NAME]
    assert server["type"] == "sdk"
    assert server["name"] == SERVER_NAME


def test_renderer_streams_text_once_and_announces_subagents():
    out, status = io.StringIO(), io.StringIO()
    renderer = Renderer(out=out, status=status)
    delta = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "확인할게요."}}
    renderer.handle(StreamEvent(uuid="u1", session_id="s", event=delta))
    renderer.handle(
        AssistantMessage(
            content=[
                TextBlock(text="확인할게요."),
                ToolUseBlock(id="t1", name="Agent", input={"subagent_type": "updeot", "prompt": "..."}),
                ToolUseBlock(id="t2", name="Task", input={"subagent_type": "ppalit", "prompt": "..."}),
            ],
            model="m",
        )
    )
    # Subagent-internal messages are not shown.
    renderer.handle(AssistantMessage(content=[TextBlock(text="내부 보고")], model="m", parent_tool_use_id="t1"))
    renderer.handle(AssistantMessage(content=[TextBlock(text="\n브리핑 끝")], model="m"))
    renderer.handle(
        ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2, session_id="s")
    )
    assert out.getvalue() == "확인할게요.\n\n브리핑 끝\n"
    assert status.getvalue().splitlines() == ["→ 업뎃에게 맡기는 중...", "→ 빠릿에게 맡기는 중..."]
    assert not renderer.failed


def test_renderer_reports_errors_in_korean():
    out, status = io.StringIO(), io.StringIO()
    renderer = Renderer(out=out, status=status)
    renderer.handle(AssistantMessage(content=[], model="m", error="authentication_failed"))
    renderer.handle(
        ResultMessage(subtype="error_during_execution", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=1, session_id="s")
    )
    assert renderer.failed
    assert "인증에 실패" in status.getvalue()
    assert "응답을 마치지 못했습니다" in status.getvalue()


def test_cli_parser():
    parser = build_parser()
    assert parser.parse_args(["--brief"]).brief is True
    assert parser.parse_args(["오늘 일정?"]).question == "오늘 일정?"
    assert "사용법" in parser.format_help()
    assert set(AGENT_LABELS.values()) == {"업뎃", "빠릿"}


# ---------------------------------------------------------------- shared runner


SCRIPT = [
    SystemMessage(subtype="init", data={"type": "system", "subtype": "init", "session_id": "sess-init"}),
    AssistantMessage(
        content=[
            TextBlock(text="업뎃에게 맡길게요."),
            ToolUseBlock(id="t1", name="Agent", input={"subagent_type": "updeot", "prompt": "..."}),
        ],
        model="m",
    ),
    AssistantMessage(content=[TextBlock(text="업뎃 내부 보고")], model="m", parent_tool_use_id="t1"),
    AssistantMessage(content=[TextBlock(text="*① 공저자 업데이트*\n• 변경 없음")], model="m"),
    ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2, session_id="sess-final"),
]


class FakeSDKClient:
    """Replaces ClaudeSDKClient: records options/prompts and replays SCRIPT per query."""

    instances: list["FakeSDKClient"] = []

    def __init__(self, options=None, transport=None):
        self.options = options
        self.prompts: list[str] = []
        FakeSDKClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt, session_id="default"):
        self.prompts.append(prompt)

    async def receive_response(self):
        for message in SCRIPT:
            yield message


@pytest.fixture
def fake_sdk(monkeypatch):
    FakeSDKClient.instances = []
    monkeypatch.setattr(main_module, "ClaudeSDKClient", FakeSDKClient)
    return FakeSDKClient


SAFETY_FIELDS = ("tools", "allowed_tools", "disallowed_tools", "permission_mode", "setting_sources", "env", "model")


def test_run_turn_resumes_reports_status_and_returns_answer(fake_sdk, capsys):
    seen: list[str] = []

    async def on_status(line):
        seen.append(line)

    result = asyncio.run(run_turn("질문", resume="sess-0", on_status=on_status, extra_system_prompt="## Slack 규칙"))
    assert isinstance(result, TurnResult)
    # Preamble before the delegation and subagent-internal text are not part of the answer.
    assert result.text == "*① 공저자 업데이트*\n• 변경 없음"
    assert result.session_id == "sess-final"
    assert not result.failed and result.error is None
    assert seen == ["→ 업뎃에게 맡기는 중..."]
    [client] = fake_sdk.instances
    assert client.prompts == ["질문"]
    opts = client.options
    assert opts.resume == "sess-0"
    assert opts.system_prompt.rstrip().endswith("## Slack 규칙")
    # Every safety setting is exactly what the CLI uses.
    baseline = build_options()
    for field in SAFETY_FIELDS:
        assert getattr(opts, field) == getattr(baseline, field), field
    assert opts.hooks["PreToolUse"][0].hooks == [tool_gate]
    assert set(opts.agents) == {"updeot", "ppalit"}
    assert set(opts.mcp_servers) == {SERVER_NAME}
    # Quiet by default: nothing printed.
    assert capsys.readouterr() == ("", "")


def test_run_turn_accepts_sync_and_failing_status_callbacks(fake_sdk, capsys):
    seen: list[str] = []
    asyncio.run(run_turn("q", on_status=seen.append))
    assert seen == ["→ 업뎃에게 맡기는 중..."]

    def broken(line):
        raise RuntimeError("display down")

    result = asyncio.run(run_turn("q", on_status=broken))
    assert result.text.endswith("변경 없음")  # the turn still completes
    assert "진행 상황을 전하지 못했습니다" in capsys.readouterr().err


def test_build_options_defaults_have_no_resume_and_no_extra_prompt():
    opts = options()
    assert opts.resume is None
    assert "Slack" not in opts.system_prompt
    assert build_options(env={}, now=NOW, resume="abc-123").resume == "abc-123"


def test_cli_one_shot_output_is_unchanged(fake_sdk, capsys):
    assert main(["어제 공저자들이 뭐 고쳤어?"]) == 0
    out, err = capsys.readouterr()
    assert out == "업뎃에게 맡길게요.\n*① 공저자 업데이트*\n• 변경 없음\n"
    assert err == "→ 업뎃에게 맡기는 중...\n"
    [client] = fake_sdk.instances
    assert client.prompts == ["어제 공저자들이 뭐 고쳤어?"]
    assert client.options.resume is None
    assert "Slack 출력" not in client.options.system_prompt


def test_cli_brief_and_chat_modes_still_work(fake_sdk, capsys, monkeypatch):
    assert main(["--brief"]) == 0
    assert "브리핑" in fake_sdk.instances[-1].prompts[0]

    lines = iter(["안녕", "", "내일 일정은?", "종료"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))
    fake_sdk.instances = []
    assert main([]) == 0
    [client] = fake_sdk.instances  # one client for the whole conversation
    assert client.prompts == ["안녕", "내일 일정은?"]
    assert "수고하셨습니다" in capsys.readouterr().out


def test_renderer_result_carries_korean_error():
    renderer = Renderer(echo=False)
    renderer.handle(AssistantMessage(content=[], model="m", error="rate_limit"))
    result = renderer.result()
    assert result.failed and result.error.startswith("요청 한도에 걸렸습니다")
    assert renderer.status_lines == ["[오류] 요청 한도에 걸렸습니다. 잠시 후 다시 시도하세요."]


def test_cli_parser_slack_options():
    parser = build_parser()
    assert parser.parse_args(["--brief", "--slack"]).slack is True
    assert parser.parse_args(["slack"]).question == "slack"
    help_text = parser.format_help()
    assert "python -m mungchi slack" in help_text
    assert "--brief --slack" in help_text
    assert "slack 한 단어" in help_text


def test_api_error_text_is_not_part_of_the_answer():
    out, status = io.StringIO(), io.StringIO()
    renderer = Renderer(out=out, status=status)
    renderer.handle(AssistantMessage(content=[TextBlock(text='API Error: 429 {"type":"error"}')], model="m", error="rate_limit"))
    result = renderer.result()
    assert result.text == ""
    assert result.failed and result.error.startswith("요청 한도")
    assert "API Error: 429" in out.getvalue()  # the terminal still shows it, as before
