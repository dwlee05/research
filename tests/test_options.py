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
    TOOL_GATES,
    build_schedule_prompt,
    build_update_prompt,
    gate_decision,
    tool_gate,
)
from mungchi.personas import SCHEDULE, UPDATE
from mungchi.main import BLOCKED_BUILTINS, Renderer, TurnResult, build_options, build_parser, main, run_turn
from mungchi.tools import CALENDAR_TOOL, DATA_TOOLS, DROPBOX_TOOL, OVERLEAF_TOOL, SERVER_NAME

NOW = datetime(2026, 10, 5, 8, 0, tzinfo=ZoneInfo("Asia/Seoul"))


def options() -> ClaudeAgentOptions:
    return build_options(env={}, now=NOW)


def test_agents_have_ascii_keys_and_own_tools_only():
    opts = options()
    assert set(opts.agents) == {"update", "schedule"}
    assert all(key.isascii() for key in opts.agents)
    update, schedule = opts.agents["update"], opts.agents["schedule"]
    assert isinstance(update, AgentDefinition) and isinstance(schedule, AgentDefinition)
    assert sorted(update.tools) == sorted([DROPBOX_TOOL, OVERLEAF_TOOL])
    assert schedule.tools == [CALENDAR_TOOL]
    for agent in (update, schedule):
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
    assert decision(DROPBOX_TOOL, UPDATE, "a1") == "allow"
    assert decision(OVERLEAF_TOOL, UPDATE, "a1") == "allow"
    assert decision(CALENDAR_TOOL, UPDATE, "a1") == "deny"
    assert decision(CALENDAR_TOOL, SCHEDULE, "a2") == "allow"
    assert decision(DROPBOX_TOOL, SCHEDULE, "a2") == "deny"
    assert decision(DROPBOX_TOOL, "general-purpose", "a3") == "deny"
    # Only 업뎃 and 일정 can be spawned.
    assert decision("Agent", tool_input={"subagent_type": "update"}) is None
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
                ToolUseBlock(id="t1", name="Agent", input={"subagent_type": "update", "prompt": "..."}),
                ToolUseBlock(id="t2", name="Task", input={"subagent_type": "schedule", "prompt": "..."}),
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
    assert status.getvalue().splitlines() == ["→ 업뎃에게 맡기는 중...", "→ 일정에게 맡기는 중..."]
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
    assert set(AGENT_LABELS.values()) == {"업뎃", "일정"}


# ---------------------------------------------------------------- shared runner


SCRIPT = [
    SystemMessage(subtype="init", data={"type": "system", "subtype": "init", "session_id": "sess-init"}),
    AssistantMessage(
        content=[
            TextBlock(text="업뎃에게 맡길게요."),
            ToolUseBlock(id="t1", name="Agent", input={"subagent_type": "update", "prompt": "..."}),
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
    assert set(opts.agents) == {"update", "schedule"}
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


def test_prompts_never_promise_overleaf_content_summaries():
    from mungchi.agents import UPDATE_DESCRIPTION, UPDATE_PROMPT

    for text in (MUNGCHI_SYSTEM_PROMPT, UPDATE_PROMPT, UPDATE_DESCRIPTION):
        assert "diff" not in text.replace("diff는 없다", "")
        assert "요약한다" not in text
    assert "프로젝트 열기" in MUNGCHI_SYSTEM_PROMPT and "프로젝트 열기" in UPDATE_PROMPT
    assert "내용은 직접 확인해 주세요." in MUNGCHI_SYSTEM_PROMPT
    assert "내용은 직접 확인해 주세요." in UPDATE_PROMPT
    for key in ("edited_by", "last_edit", "edits", "unchanged", "errors"):
        assert key in UPDATE_PROMPT


# ---------------------------------------------------------------- personas (direct 업뎃 / 일정)


@pytest.fixture
def server_spy(monkeypatch):
    """Records which tools each built MCP server gets (None = all three)."""
    built: list[list[str] | None] = []
    real = main_module.build_server

    def spy(tools=None):
        tools = None if tools is None else list(tools)
        built.append(None if tools is None else [t.name for t in tools])
        return real(tools)

    monkeypatch.setattr(main_module, "build_server", spy)
    return built


def test_direct_update_gets_only_its_two_data_tools(server_spy):
    opts = build_options(env={}, now=NOW, persona="update")
    assert opts.tools == []  # no built-in tools at all, not even Agent
    assert opts.allowed_tools == [DROPBOX_TOOL, OVERLEAF_TOOL]
    assert "Agent" not in opts.allowed_tools and "Agent" in opts.disallowed_tools
    assert not opts.agents
    assert set(BLOCKED_BUILTINS) <= set(opts.disallowed_tools)
    assert opts.permission_mode == "dontAsk"
    assert opts.setting_sources == []
    assert opts.env == options().env
    assert server_spy[0] == ["check_dropbox_updates", "check_overleaf_updates"]
    assert opts.hooks["PreToolUse"][0].hooks == [TOOL_GATES["update"]]
    assert set(opts.mcp_servers) == {SERVER_NAME}


def test_direct_schedule_gets_only_get_schedule(server_spy):
    opts = build_options(env={}, now=NOW, persona="schedule")
    assert opts.tools == []
    assert opts.allowed_tools == [CALENDAR_TOOL]
    assert "Agent" in opts.disallowed_tools
    assert not opts.agents
    assert set(BLOCKED_BUILTINS) <= set(opts.disallowed_tools)
    assert server_spy[-1] == ["get_schedule"]
    assert opts.hooks["PreToolUse"][0].hooks == [TOOL_GATES["schedule"]]


def test_mungchi_options_are_unchanged_by_personas(server_spy):
    default, explicit = options(), build_options(env={}, now=NOW, persona="mungchi")
    for field in (*SAFETY_FIELDS, "system_prompt"):
        assert getattr(default, field) == getattr(explicit, field), field
    assert explicit.tools == ["Agent"] and explicit.allowed_tools == ["Agent"]
    assert explicit.disallowed_tools == BLOCKED_BUILTINS
    assert set(explicit.agents) == {"update", "schedule"}
    assert explicit.hooks["PreToolUse"][0].hooks == [tool_gate]
    assert TOOL_GATES["mungchi"] is tool_gate
    assert server_spy == [None, None]  # 고뭉치's server keeps all three tools for its subagents


def test_unknown_persona_is_rejected():
    with pytest.raises(ValueError):
        build_options(env={}, now=NOW, persona="nobody")


def test_direct_gates_allow_only_the_personas_own_tools():
    def decision(persona, tool, agent_id=None, tool_input=None):
        out = gate_decision(tool, tool_input or {}, None, agent_id, persona=persona)
        return out["hookSpecificOutput"]["permissionDecision"]

    assert decision("update", DROPBOX_TOOL) == "allow"
    assert decision("update", OVERLEAF_TOOL) == "allow"
    assert decision("update", CALENDAR_TOOL) == "deny"
    assert decision("schedule", CALENDAR_TOOL) == "allow"
    assert decision("schedule", DROPBOX_TOOL) == "deny"
    assert decision("schedule", OVERLEAF_TOOL) == "deny"
    for persona in ("update", "schedule"):
        for tool in ("Agent", "Task"):
            for subagent in ("update", "schedule", "general-purpose"):
                assert decision(persona, tool, tool_input={"subagent_type": subagent}) == "deny"
        for tool in ("Bash", "Read", "Write", "WebFetch", "mcp__other__tool", ""):
            assert decision(persona, tool) == "deny"
        # Never from inside a subagent, even for the persona's own tools.
        for tool in DATA_TOOLS:
            assert decision(persona, tool, agent_id="a1") == "deny"
    # Unknown personas own nothing.
    for tool in DATA_TOOLS:
        assert decision("nobody", tool) == "deny"


def test_direct_gate_hooks_are_async_and_persona_bound():
    async def ask(persona, tool):
        out = await TOOL_GATES[persona]({"tool_name": tool, "tool_input": {}}, "t1", None)
        return out["hookSpecificOutput"]["permissionDecision"]

    assert asyncio.run(ask("update", DROPBOX_TOOL)) == "allow"
    assert asyncio.run(ask("update", CALENDAR_TOOL)) == "deny"
    assert asyncio.run(ask("schedule", CALENDAR_TOOL)) == "allow"
    assert asyncio.run(ask("schedule", "Agent")) == "deny"


def test_direct_and_subagent_prompts_share_the_same_rules():
    sub, direct = build_update_prompt(), build_update_prompt(direct=True)
    assert "고뭉치에게 한국어로 보고" in sub and "고뭉치" not in direct.split("## 대화")[0]
    assert "사용자에게 직접" in direct and "사용자에게 직접" not in sub
    rules = sub[sub.index("## 공통 규칙") : sub.index("## 보고 형식")]
    assert rules.replace("고뭉치에게", "사용자에게") in direct
    assert sub.split("## 보고 형식")[1] == direct.split("## 답 형식")[1]

    sub, direct = build_schedule_prompt(), build_schedule_prompt(direct=True)
    assert "고뭉치에게 한국어로 짧게 보고" in sub and "사용자에게 직접 한국어로 짧게 답한다" in direct
    assert sub.split("## 보고 형식 (짧게)")[1] == direct.split("## 답 형식 (짧게)")[1]
    assert "'일정' 에이전트" in sub and "'일정' 에이전트" in direct


def test_direct_prompts_carry_date_and_point_elsewhere_for_other_requests():
    update = build_options(env={}, now=NOW, persona="update").system_prompt
    schedule = build_options(env={}, now=NOW, persona="schedule").system_prompt
    for prompt in (update, schedule):
        assert "2026-10-05 (월요일)" in prompt and "Asia/Seoul" in prompt
        assert "Agent" not in prompt
    assert "'일정' 에이전트" in update and "고뭉치 담당" in update
    assert "업뎃" in schedule and "고뭉치 담당" in schedule
    # 고뭉치 names its schedule subagent unambiguously.
    assert "'일정' 에이전트 (subagent_type: \"schedule\")" in MUNGCHI_SYSTEM_PROMPT


def test_run_turn_runs_the_requested_persona(fake_sdk):
    result = asyncio.run(run_turn("질문", persona="update", resume="sess-0", extra_system_prompt="## Slack 규칙"))
    assert result.session_id == "sess-final"
    [client] = fake_sdk.instances
    opts = client.options
    assert opts.resume == "sess-0"
    assert opts.tools == [] and not opts.agents
    assert opts.allowed_tools == [DROPBOX_TOOL, OVERLEAF_TOOL]
    assert opts.hooks["PreToolUse"][0].hooks == [TOOL_GATES["update"]]
    assert opts.system_prompt.rstrip().endswith("## Slack 규칙")


def test_renderer_answer_starts_after_last_data_tool_call():
    renderer = Renderer(echo=False)
    renderer.handle(
        AssistantMessage(
            content=[TextBlock(text="확인해 볼게요."), ToolUseBlock(id="t1", name=DROPBOX_TOOL, input={})], model="m"
        )
    )
    renderer.handle(AssistantMessage(content=[TextBlock(text="Dropbox 변경 없음")], model="m"))
    assert renderer.result().text == "Dropbox 변경 없음"
    assert renderer.status_lines == []


def test_cli_agent_one_shot_and_chat(fake_sdk, capsys, monkeypatch):
    assert main(["--agent", "update", "누가 Overleaf 고쳤어?"]) == 0
    [client] = fake_sdk.instances
    assert client.prompts == ["누가 Overleaf 고쳤어?"]
    assert client.options.allowed_tools == [DROPBOX_TOOL, OVERLEAF_TOOL] and not client.options.agents

    fake_sdk.instances = []
    lines = iter(["내일 일정은?", "종료"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))
    assert main(["--agent", "schedule"]) == 0
    [client] = fake_sdk.instances
    assert client.prompts == ["내일 일정은?"]
    assert client.options.allowed_tools == [CALENDAR_TOOL]
    out = capsys.readouterr().out
    assert "\n'일정'입니다. 캘린더 일정을 확인해 드릴게요." in out and "일정: 수고하셨습니다!" in out


def test_cli_agent_argument_rules(capsys):
    for argv in (["--agent", "update", "--brief"], ["slack", "--agent", "update"], ["--agent", "mungchi"], ["--agent", "nobody"]):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--brief는 고뭉치 전용" in err
    assert "slack 명령은 --agent와 함께 쓸 수 없습니다" in err
    help_text = build_parser().format_help()
    assert "--agent" in help_text and "update" in help_text and "schedule" in help_text
