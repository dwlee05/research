from __future__ import annotations

import asyncio
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from dotenv import dotenv_values
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

from mungchi import config
from mungchi import main as main_module
from mungchi.agents import (
    AGENT_LABELS,
    MUNGCHI_SYSTEM_PROMPT,
    NOW_GUIDANCE,
    PERSONA_TOOLS,
    TOOL_GATES,
    build_schedule_prompt,
    build_update_prompt,
    gate_decision,
    now_line,
    tool_gate,
)
from mungchi.personas import SCHEDULE, UPDATE
from mungchi.main import (
    BLOCKED_BUILTINS,
    Renderer,
    TurnResult,
    build_options,
    build_parser,
    main,
    run_chat,
    run_turn,
    stamp_prompt,
)
from mungchi.slack_format import EXAMPLE_FOLDER_LINK, SLACK_FOLDER_LINE_EXAMPLE, SLACK_FORMAT_PROMPT, to_mrkdwn
from mungchi.tools import ALL_TOOLS, CALENDAR_TOOL, DATA_TOOLS, DROPBOX_TOOL, SERVER_NAME, UPDATE_TOOLS, dropbox_tool
from mungchi.tools.dropbox_tool import check_dropbox_updates

NOW = datetime(2026, 10, 5, 8, 0, tzinfo=ZoneInfo("Asia/Seoul"))
# The Overleaf check was removed; its old tool name must stay unusable.
REMOVED_TOOL_NAMES = ("check_overleaf_updates", "mcp__mungchi__check_overleaf_updates")
# The line run_turn / the chat loop put in front of every prompt (default TIMEZONE: Asia/Seoul).
NOW_LINE_RE = re.compile(r"\[지금: \d{4}-\d{2}-\d{2}\([월화수목금토일]\) \d{2}:\d{2} KST\]\n")


def without_now_line(prompt: str) -> str:
    """``prompt`` as typed: asserts the per-turn time line is in front and strips it."""
    match = NOW_LINE_RE.match(prompt)
    assert match, prompt
    return prompt[match.end() :]


def options() -> ClaudeAgentOptions:
    return build_options(env={})


def test_agents_have_ascii_keys_and_own_tools_only():
    opts = options()
    assert set(opts.agents) == {"update", "schedule"}
    assert all(key.isascii() for key in opts.agents)
    update, schedule = opts.agents["update"], opts.agents["schedule"]
    assert isinstance(update, AgentDefinition) and isinstance(schedule, AgentDefinition)
    assert update.tools == [DROPBOX_TOOL]
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
    assert build_options(env={"MUNGCHI_MODEL": "custom-model"}).model == "custom-model"


def test_system_prompt_has_the_briefing_sections_and_points_to_the_per_turn_time_line():
    prompt = options().system_prompt
    # Schedule first, then Dropbox; the credits are appended by code, not written by the model.
    assert prompt.index("### ① 오늘의 일정") < prompt.index("### ② Dropbox 업데이트")
    assert "하루치만(days=1)" in prompt
    assert "Chat KHU 크레딧은 쓰지 않고" in prompt
    # The old "③ 오늘 챙길 것" section was dropped to save tokens.
    assert "③" not in prompt and "오늘 챙길 것" not in prompt
    assert NOW_GUIDANCE in prompt and "[지금: YYYY-MM-DD(요일) HH:MM 시간대]" in prompt
    assert "{" not in MUNGCHI_SYSTEM_PROMPT  # no unfilled placeholders, nothing filled per call


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
    assert [without_now_line(p) for p in client.prompts] == ["질문"]
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
    assert build_options(env={}, resume="abc-123").resume == "abc-123"


def test_cli_one_shot_output_is_unchanged(fake_sdk, capsys):
    assert main(["어제 공저자들이 뭐 고쳤어?"]) == 0
    out, err = capsys.readouterr()
    assert out == "업뎃에게 맡길게요.\n*① 공저자 업데이트*\n• 변경 없음\n"
    assert err == "→ 업뎃에게 맡기는 중...\n"
    [client] = fake_sdk.instances
    assert [without_now_line(p) for p in client.prompts] == ["어제 공저자들이 뭐 고쳤어?"]
    assert client.options.resume is None
    assert "Slack 출력" not in client.options.system_prompt


def test_cli_brief_and_chat_modes_still_work(fake_sdk, capsys, monkeypatch):
    assert main(["--brief"]) == 0
    assert "브리핑" in without_now_line(fake_sdk.instances[-1].prompts[0])

    lines = iter(["안녕", "", "내일 일정은?", "종료"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))
    fake_sdk.instances = []
    assert main([]) == 0
    [client] = fake_sdk.instances  # one client for the whole conversation
    assert [without_now_line(p) for p in client.prompts] == ["안녕", "내일 일정은?"]
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
    # The terminal still shows it, once, inside the [오류] lines (stderr) instead of the answer (stdout).
    assert "API Error: 429" not in out.getvalue()
    assert status.getvalue().count("API Error: 429") == 1


def test_prompts_never_promise_content_summaries():
    from mungchi.agents import UPDATE_DESCRIPTION, UPDATE_PROMPT

    for text in (MUNGCHI_SYSTEM_PROMPT, UPDATE_PROMPT, UPDATE_DESCRIPTION):
        assert "diff" not in text.replace("diff는 없다", "")
        assert "요약한다" not in text
    assert "내용은 직접 확인해 주세요." in MUNGCHI_SYSTEM_PROMPT
    assert "내용은 직접 확인해 주세요." in UPDATE_PROMPT
    for key in ("groups", "by", "path", "modified", "omitted", "total_files", "link"):
        assert key in UPDATE_PROMPT


def test_update_prompts_turn_periods_into_since_hours_and_explain_empty_results():
    from mungchi.tools.common import SINCE_HOURS_SCHEMA

    for prompt in (build_update_prompt(), build_update_prompt(direct=True)):
        # Period -> since_hours.
        assert '"최근 3일"' in prompt and "→ 72" in prompt
        assert '"오늘" → 오늘 0시' in prompt and '"이번 주" → 이번 주 월요일 0시' in prompt
        # Why nothing was found, from stats and since_basis.
        for key in (
            "stats",
            "since_basis",
            "changed_in_window",
            "excluded_mine",
            "excluded_unknown_modifier",
            "default_24h",
            "briefing_checkpoint",
            "lookback_default",
        ):
            assert key in prompt
        assert "last_check" not in prompt and "마지막 확인" not in prompt
        assert "최근 24시간 동안 공저자가 바꾼 파일이 없어요" in prompt
        assert "지난 브리핑(10/06 07:50) 이후 바뀐 파일이 없어요" in prompt
        assert "기간 안에 바뀐 파일 5개는 모두 내가 수정했어요" in prompt
        assert "수정한 사람을 알 수 없어 뺐어요 (공유 폴더가 아닌 곳에 있을 수 있어요)" in prompt

    # 고뭉치 converts periods for 업뎃 from the per-turn time line, and passes
    # that line on, since its subagents never see the user's message.
    mungchi = build_options(env={}).system_prompt
    assert "## 기간 전하기" in mungchi and "since_hours" in mungchi
    assert "사용자 메시지 맨 앞의 [지금: ...] 줄(현지 시간)을 기준으로 시간 수로 바꿔" in mungchi
    assert "Agent 도구의 prompt 맨 앞에 그 [지금: ...] 줄을 그대로 옮겨 적는다" in mungchi
    assert "팀원은 이 대화를 볼 수 없고 지금 날짜·시각도 모른다" in mungchi
    # The subagent falls back to the line 고뭉치 copied; direct 업뎃 reads the user's.
    assert "고뭉치가 맡긴 글 맨 앞의 [지금: ...] 줄을 기준으로 바꾼다" in build_update_prompt()
    direct = build_options(env={}, persona="update").system_prompt
    assert "사용자 메시지 맨 앞의 [지금: ...] 줄에 있다" in direct and NOW_GUIDANCE in direct
    assert "지금 시각:" not in mungchi and "지금 시각:" not in direct

    description = SINCE_HOURS_SCHEMA["properties"]["since_hours"]["description"]
    assert "'최근 3일' → 72" in description and "'오늘'" in description and "'이번 주'" in description
    assert "최근 24시간" in description and "마지막 확인" not in description
    # 고뭉치 does not pick the mode either: without a period it leaves since_hours out.
    assert "since_hours 없이 맡긴다" in mungchi and "마지막 확인" not in mungchi


def test_folder_link_example_line_is_in_every_prompt_that_writes_one():
    """One link per subfolder: never "(폴더 열기: 폴더 열기)" again."""
    terminal_example = f"- 01_Youn\n  {EXAMPLE_FOLDER_LINK}\n  - 김공저: draft.tex (<modified>)"
    assert terminal_example in build_update_prompt()  # 업뎃 reporting to 고뭉치
    assert terminal_example in build_options(env={}, persona="update").system_prompt  # 업뎃 answering directly
    assert terminal_example.replace("\n", "\n  ") in MUNGCHI_SYSTEM_PROMPT  # 고뭉치 relaying it, indented
    assert SLACK_FOLDER_LINE_EXAMPLE == (
        "• *01_Youn* <https://www.dropbox.com/home/20_%EC%97%B0%EA%B5%AC-%EC%A7%84%ED%96%89/01_Youn|📂 열기>"
    )
    for persona in ("mungchi", "update"):  # in Slack, the one-line form overrides the layout above
        slack_prompt = build_options(env={}, persona=persona, extra_system_prompt=SLACK_FORMAT_PROMPT).system_prompt
        assert "\n" + SLACK_FOLDER_LINE_EXAMPLE + "\n" in slack_prompt
    for prompt in (MUNGCHI_SYSTEM_PROMPT, build_update_prompt(), build_update_prompt(direct=True), SLACK_FORMAT_PROMPT):
        assert "(폴더 열기: <" not in prompt and "<주소|폴더 열기>" not in prompt and "<link>)" not in prompt
        assert "'폴더 열기:' 같은 말을" in prompt
    # The Slack safety net leaves the intended line alone.
    assert to_mrkdwn(SLACK_FOLDER_LINE_EXAMPLE) == SLACK_FOLDER_LINE_EXAMPLE


# ---------------------------------------------------------------- personas (direct 업뎃 / 일정)


@pytest.fixture
def server_spy(monkeypatch):
    """Records which tools each built MCP server gets (None = every data tool)."""
    built: list[list[str] | None] = []
    real = main_module.build_server

    def spy(tools=None):
        tools = None if tools is None else list(tools)
        built.append(None if tools is None else [t.name for t in tools])
        return real(tools)

    monkeypatch.setattr(main_module, "build_server", spy)
    return built


def test_direct_update_gets_only_the_dropbox_tool(server_spy):
    opts = build_options(env={}, persona="update")
    assert opts.tools == []  # no built-in tools at all, not even Agent
    assert opts.allowed_tools == [DROPBOX_TOOL]
    assert "Agent" not in opts.allowed_tools and "Agent" in opts.disallowed_tools
    assert not opts.agents
    assert set(BLOCKED_BUILTINS) <= set(opts.disallowed_tools)
    assert opts.permission_mode == "dontAsk"
    assert opts.setting_sources == []
    assert opts.env == options().env
    assert server_spy[0] == ["check_dropbox_updates"]
    assert opts.hooks["PreToolUse"][0].hooks == [TOOL_GATES["update"]]
    assert set(opts.mcp_servers) == {SERVER_NAME}


def test_direct_schedule_gets_only_get_schedule(server_spy):
    opts = build_options(env={}, persona="schedule")
    assert opts.tools == []
    assert opts.allowed_tools == [CALENDAR_TOOL]
    assert "Agent" in opts.disallowed_tools
    assert not opts.agents
    assert set(BLOCKED_BUILTINS) <= set(opts.disallowed_tools)
    assert server_spy[-1] == ["get_schedule"]
    assert opts.hooks["PreToolUse"][0].hooks == [TOOL_GATES["schedule"]]


def test_mungchi_options_are_unchanged_by_personas(server_spy):
    default, explicit = options(), build_options(env={}, persona="mungchi")
    for field in (*SAFETY_FIELDS, "system_prompt"):
        assert getattr(default, field) == getattr(explicit, field), field
    assert explicit.tools == ["Agent"] and explicit.allowed_tools == ["Agent"]
    assert explicit.disallowed_tools == BLOCKED_BUILTINS
    assert set(explicit.agents) == {"update", "schedule"}
    assert explicit.hooks["PreToolUse"][0].hooks == [tool_gate]
    assert TOOL_GATES["mungchi"] is tool_gate
    # 고뭉치's server keeps every data tool for its subagents (built for this run).
    assert server_spy == [["check_dropbox_updates", "get_schedule"]] * 2


def test_unknown_persona_is_rejected():
    with pytest.raises(ValueError):
        build_options(env={}, persona="nobody")


def test_direct_gates_allow_only_the_personas_own_tools():
    def decision(persona, tool, agent_id=None, tool_input=None):
        out = gate_decision(tool, tool_input or {}, None, agent_id, persona=persona)
        return out["hookSpecificOutput"]["permissionDecision"]

    assert decision("update", DROPBOX_TOOL) == "allow"
    assert decision("update", CALENDAR_TOOL) == "deny"
    assert decision("schedule", CALENDAR_TOOL) == "allow"
    assert decision("schedule", DROPBOX_TOOL) == "deny"
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
    rules = sub[sub.index("## 규칙") : sub.index("## 보고 형식")]
    assert rules.replace("고뭉치에게", "사용자에게") in direct
    assert sub.split("## 보고 형식")[1] == direct.split("## 답 형식")[1]

    sub, direct = build_schedule_prompt(), build_schedule_prompt(direct=True)
    assert "고뭉치에게 한국어로 짧게 보고" in sub and "사용자에게 직접 한국어로 짧게 답한다" in direct
    assert sub.split("## 보고 형식 (짧게)")[1] == direct.split("## 답 형식 (짧게)")[1]
    assert "'일정' 에이전트" in sub and "'일정' 에이전트" in direct


def test_direct_prompts_explain_the_time_line_and_point_elsewhere_for_other_requests():
    update = build_options(env={}, persona="update").system_prompt
    schedule = build_options(env={}, persona="schedule").system_prompt
    for prompt in (update, schedule):
        assert NOW_GUIDANCE in prompt and "## 출력" in prompt
        assert "Agent" not in prompt
    assert "사용자 메시지 맨 앞의 [지금: ...] 줄의 날짜를 기준으로 YYYY-MM-DD로" in schedule
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
    assert opts.allowed_tools == [DROPBOX_TOOL]
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
    assert main(["--agent", "update", "누가 무슨 파일 고쳤어?"]) == 0
    [client] = fake_sdk.instances
    assert [without_now_line(p) for p in client.prompts] == ["누가 무슨 파일 고쳤어?"]
    assert client.options.allowed_tools == [DROPBOX_TOOL] and not client.options.agents

    fake_sdk.instances = []
    lines = iter(["내일 일정은?", "종료"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))
    assert main(["--agent", "schedule"]) == 0
    [client] = fake_sdk.instances
    assert [without_now_line(p) for p in client.prompts] == ["내일 일정은?"]
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


# ---------------------------------------------------------------- removed Overleaf check


def test_update_has_exactly_one_data_tool():
    assert UPDATE_TOOLS == [DROPBOX_TOOL]
    assert PERSONA_TOOLS[UPDATE] == [DROPBOX_TOOL]
    assert DATA_TOOLS == [DROPBOX_TOOL, CALENDAR_TOOL]
    assert [t.name for t in ALL_TOOLS] == ["check_dropbox_updates", "get_schedule"]


def test_removed_tool_names_are_unknown_or_denied():
    def decision(tool, persona="mungchi", agent_type=None, agent_id=None):
        out = gate_decision(tool, {}, agent_type, agent_id, persona=persona)
        return out.get("hookSpecificOutput", {}).get("permissionDecision")

    mungchi = options()
    for name in REMOVED_TOOL_NAMES:
        # Unknown: not on the server, not a data tool, not given to anyone.
        assert name not in DATA_TOOLS
        assert name not in {t.name for t in ALL_TOOLS}
        assert name not in mungchi.allowed_tools
        assert all(name not in agent.tools for agent in mungchi.agents.values())
        # Never allowed by 고뭉치's gate, from the main agent or inside a subagent.
        assert decision(name) != "allow"
        for agent_type in (UPDATE, SCHEDULE):
            assert decision(name, agent_type=agent_type, agent_id="a1") != "allow"
        # 업뎃 / 일정 answering directly: explicitly denied.
        for persona in (UPDATE, SCHEDULE):
            assert name not in build_options(env={}, persona=persona).allowed_tools
            assert decision(name, persona=persona) == "deny"


def test_user_facing_texts_never_mention_overleaf():
    from mungchi.agents import UPDATE_DESCRIPTION
    from mungchi.slack_format import SLACK_FORMAT_PROMPT

    texts = [
        MUNGCHI_SYSTEM_PROMPT,
        build_update_prompt(),
        build_update_prompt(direct=True),
        UPDATE_DESCRIPTION,
        build_options(env={}, persona="update").system_prompt,
        SLACK_FORMAT_PROMPT,
        build_parser().format_help(),
        *main_module.CHAT_GREETINGS.values(),
        *config.slack_bot_problems(config.SlackConfig()),
    ]
    for text in texts:
        assert "overleaf" not in text.lower()


OLD_ENV = """\
MUNGCHI_MODEL=claude-opus-5-5
MY_NAMES=홍길동,Gildong Hong
MY_EMAILS=gildong@example.com
OVERLEAF_GIT_TOKEN=olp_not-a-real-token
OVERLEAF_PROJECTS=논문A=0123456789abcdef01234567
OVERLEAF_CACHE_DIR=
"""


def test_old_env_with_overleaf_and_my_lines_still_works(tmp_path, monkeypatch, fake_sdk, capsys):
    """An .env written before the Overleaf removal: the leftover lines are loaded but never read."""
    env_file = tmp_path / ".env"  # conftest made tmp_path the working directory
    env_file.write_text(OLD_ENV, encoding="utf-8")
    for key in dotenv_values(env_file):
        # Recorded by monkeypatch, so whatever main() loads from .env is removed after the test.
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)

    assert main(["--agent", "update", "공저자 업데이트 확인해줘"]) == 0
    assert os.environ["OVERLEAF_GIT_TOKEN"] == "olp_not-a-real-token"  # loaded from .env, then ignored
    [client] = fake_sdk.instances
    assert client.options.allowed_tools == [DROPBOX_TOOL]
    assert "overleaf" not in client.options.system_prompt.lower()
    assert main(["--brief"]) == 0  # 고뭉치 as well

    # The Dropbox check and the Slack checks only talk about their own settings.
    result = asyncio.run(check_dropbox_updates.handler({}))
    data = json.loads(result["content"][0]["text"])
    assert data["configured"] is False and data["missing"] == ["DROPBOX_ACCESS_TOKEN"]
    text = json.dumps(data, ensure_ascii=False) + "\n".join(config.slack_bot_problems(config.load_slack_config()))
    for name in ("OVERLEAF", "MY_NAMES", "MY_EMAILS"):
        assert name not in text
    err = capsys.readouterr().err
    assert "[오류]" not in err and "OVERLEAF" not in err


# ---------------------------------------------------------------- cache-stable system prompts, time per turn


class _PinnedDatetime(datetime):
    """``datetime`` whose ``now()`` is pinned; patched over a module's ``datetime``."""

    pinned: datetime = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.pinned.astimezone(tz) if tz is not None else cls.pinned


def _every_system_prompt() -> dict[str, str]:
    """System prompts of every persona (CLI and Slack) and every subagent definition."""
    texts: dict[str, str] = {}
    for persona in ("mungchi", "update", "schedule"):
        for extra in ("", SLACK_FORMAT_PROMPT):
            opts = build_options(persona=persona, extra_system_prompt=extra)
            key = persona + ("+slack" if extra else "")
            texts[key] = opts.system_prompt
            for name, agent in (opts.agents or {}).items():
                texts[f"{key}/{name}.prompt"] = agent.prompt
                texts[f"{key}/{name}.description"] = agent.description
    return texts


def test_system_prompts_carry_no_time_and_are_byte_identical_at_different_times(monkeypatch):
    morning = datetime(2026, 10, 5, 8, 7, tzinfo=ZoneInfo("Asia/Seoul"))
    months_later = datetime(2027, 3, 14, 21, 37, tzinfo=ZoneInfo("Asia/Seoul"))
    snapshots = []
    for pinned in (morning, months_later):
        monkeypatch.setattr(main_module, "datetime", type("Pinned", (_PinnedDatetime,), {"pinned": pinned}))
        snapshots.append(_every_system_prompt())
    first, second = snapshots
    assert first.keys() == second.keys() and len(first) == 3 * 2 + 2 * 2 * 2
    for key, text in first.items():
        assert text.encode("utf-8") == second[key].encode("utf-8"), key
        assert not re.search(r"\d{4}-\d{2}-\d{2}", text), key
        for stamp in ("10-05", "03-14", "08:07", "21:37", "Asia/Seoul", "KST", "지금 시각:", "오늘 날짜:"):
            assert stamp not in text, (key, stamp)


def test_now_line_is_short_and_uses_the_zone_abbreviation_when_there_is_one():
    utc = datetime(2026, 10, 6, 5, 20, tzinfo=timezone.utc)
    assert now_line(utc.astimezone(ZoneInfo("Asia/Seoul"))) == "[지금: 2026-10-06(화) 14:20 KST]"
    assert now_line(utc) == "[지금: 2026-10-06(화) 05:20 UTC]"
    assert now_line(utc.astimezone(ZoneInfo("America/New_York"))) == "[지금: 2026-10-06(화) 01:20 EDT]"
    # No real abbreviation (tzdata says "+04"): the IANA name instead.
    assert now_line(utc.astimezone(ZoneInfo("Asia/Dubai"))) == "[지금: 2026-10-06(화) 09:20 Asia/Dubai]"
    # A bare offset has neither: the zone is left out.
    assert now_line(utc.astimezone(timezone(timedelta(hours=9)))) == "[지금: 2026-10-06(화) 14:20]"
    # stamp_prompt converts the clock's time to TIMEZONE.
    clock = lambda: utc  # noqa: E731
    assert stamp_prompt("질문", clock) == "[지금: 2026-10-06(화) 14:20 KST]\n질문"
    assert stamp_prompt("q", clock, env={"TIMEZONE": "Europe/Berlin"}) == "[지금: 2026-10-06(화) 07:20 CEST]\nq"


def test_run_turn_puts_the_current_time_in_the_user_message(fake_sdk, monkeypatch):
    first_turn = lambda: datetime(2026, 10, 6, 5, 20, tzinfo=timezone.utc)  # noqa: E731 - 14:20 in Seoul
    next_day = lambda: datetime(2026, 10, 7, 0, 5, tzinfo=timezone.utc)  # noqa: E731 - 09:05 in Seoul
    asyncio.run(run_turn("최근 3일 업데이트 알려줘", clock=first_turn, extra_system_prompt=SLACK_FORMAT_PROMPT))
    # A resumed Slack thread, the next day.
    asyncio.run(run_turn("이어서", resume="sess-final", clock=next_day, extra_system_prompt=SLACK_FORMAT_PROMPT))
    first, second = fake_sdk.instances
    assert first.prompts == ["[지금: 2026-10-06(화) 14:20 KST]\n최근 3일 업데이트 알려줘"]
    assert second.prompts == ["[지금: 2026-10-07(수) 09:05 KST]\n이어서"]
    # Same system prompt on both turns, so the cached prefix is reused.
    assert first.options.system_prompt == second.options.system_prompt
    assert "2026-10-0" not in first.options.system_prompt

    # Direct personas too, in the configured TIMEZONE.
    monkeypatch.setenv("TIMEZONE", "Asia/Tokyo")
    fake_sdk.instances = []
    for persona in ("update", "schedule"):
        asyncio.run(run_turn("내일 일정은?", persona=persona, clock=first_turn))
    assert [c.prompts for c in fake_sdk.instances] == [["[지금: 2026-10-06(화) 14:20 JST]\n내일 일정은?"]] * 2


def test_chat_loop_puts_the_current_time_in_every_message(fake_sdk, monkeypatch, capsys):
    seoul = ZoneInfo("Asia/Seoul")
    times = iter([datetime(2026, 10, 6, 23, 59, tzinfo=seoul), datetime(2026, 10, 7, 0, 1, tzinfo=seoul)])
    lines = iter(["안녕", "", "내일 일정은?", "종료"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))
    options = build_options(persona="schedule")
    assert asyncio.run(run_chat(options, "schedule", clock=lambda: next(times))) == 0
    [client] = fake_sdk.instances  # one session; the time is read again for every message
    assert client.prompts == [
        "[지금: 2026-10-06(화) 23:59 KST]\n안녕",
        "[지금: 2026-10-07(수) 00:01 KST]\n내일 일정은?",
    ]
    assert client.options.system_prompt == build_options(persona="schedule").system_prompt


# ---------------------------------------------------------------- briefing mode per run


@pytest.fixture
def server_tools(monkeypatch):
    """The tool objects each built MCP server gets, in build order."""
    built: list[list] = []
    real = main_module.build_server

    def spy(tools=None):
        tools = None if tools is None else list(tools)
        built.append(tools)
        return real(tools)

    monkeypatch.setattr(main_module, "build_server", spy)
    return built


def _dropbox_mode(tools, monkeypatch) -> bool:
    """Call the run's Dropbox tool and report the briefing flag it passes to run_check."""
    seen: list[bool] = []

    def fake_run_check(since_hours=0, *, briefing=False):
        seen.append(briefing)
        return {"configured": False, "missing": [], "hint": ""}

    monkeypatch.setattr(dropbox_tool, "run_check", fake_run_check)
    [tool] = [t for t in tools if t.name == "check_dropbox_updates"]
    asyncio.run(tool.handler({}))
    return seen[-1]


def test_only_the_brief_run_gets_a_briefing_dropbox_tool(fake_sdk, server_tools, monkeypatch, capsys):
    assert main(["--brief"]) == 0
    assert main(["어제 공저자들이 뭐 고쳤어?"]) == 0
    assert main(["--agent", "update", "누가 무슨 파일 고쳤어?"]) == 0
    asyncio.run(run_turn("업데이트 알려줘", extra_system_prompt=SLACK_FORMAT_PROMPT))  # a Slack turn
    assert [_dropbox_mode(tools, monkeypatch) for tools in server_tools] == [True, False, False, False]
    # Every run builds its own tool object, so a flag can never leak from one run to another.
    dropbox_tools = [t for tools in server_tools for t in tools if t.name == "check_dropbox_updates"]
    assert len({id(t) for t in dropbox_tools}) == 4
    assert all(t is not check_dropbox_updates for t in dropbox_tools)


def test_build_options_binds_briefing_into_its_own_server(server_tools, monkeypatch):
    briefing = build_options(env={}, briefing=True)
    ad_hoc = build_options(env={})
    direct = build_options(env={}, persona="update", briefing=True)
    assert [_dropbox_mode(tools, monkeypatch) for tools in server_tools] == [True, False, True]
    # Apart from the server, the options are the same: nothing else depends on the mode.
    for field in (*SAFETY_FIELDS, "system_prompt"):
        assert getattr(briefing, field) == getattr(ad_hoc, field), field
    assert direct.allowed_tools == [DROPBOX_TOOL]
