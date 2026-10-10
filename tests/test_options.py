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
from mungchi.tools import (
    ALL_TOOLS,
    CALENDAR_TOOL,
    CREDITS_TOOL,
    DATA_TOOLS,
    DROPBOX_TOOL,
    PROPOSE_TOOL,
    SERVER_NAME,
    UPDATE_TOOLS,
    WEATHER_TOOL,
    dropbox_tool,
)
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
    assert update.tools == [DROPBOX_TOOL]  # 업뎃 stays Dropbox-only under 고뭉치
    assert schedule.tools == [CALENDAR_TOOL, WEATHER_TOOL, PROPOSE_TOOL]
    for agent in (update, schedule):
        assert agent.model == "inherit"
        assert all(t.startswith(f"mcp__{SERVER_NAME}__") for t in agent.tools)
    assert DROPBOX_TOOL == "mcp__mungchi__check_dropbox_updates"


def test_main_agent_can_use_only_the_agent_tool_and_its_two_read_only_tools():
    opts = options()
    assert opts.tools == ["Agent"]
    # Agent, plus get_credits and get_weather (read-only, cheap); Dropbox and the calendar stay delegated.
    assert opts.allowed_tools == ["Agent", CREDITS_TOOL, WEATHER_TOOL]
    for builtin in ("Bash", "Write", "Edit"):
        assert builtin in opts.disallowed_tools
        assert builtin not in opts.tools
    assert set(DATA_TOOLS) & set(opts.allowed_tools) == {CREDITS_TOOL, WEATHER_TOOL}
    assert DROPBOX_TOOL not in opts.allowed_tools and CALENDAR_TOOL not in opts.allowed_tools
    assert opts.permission_mode == "dontAsk"
    assert opts.setting_sources == []
    assert opts.env["CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS"] == "1"
    assert set(BLOCKED_BUILTINS) <= set(opts.disallowed_tools)


def test_data_tools_are_gated_per_subagent():
    def decision(tool, agent_type=None, agent_id=None, tool_input=None):
        out = gate_decision(tool, tool_input or {}, agent_type, agent_id)
        return out.get("hookSpecificOutput", {}).get("permissionDecision")

    # Main thread (no agent_id): never Dropbox or the calendar; get_credits and get_weather it calls itself.
    assert decision(DROPBOX_TOOL) == "deny"
    assert decision(CALENDAR_TOOL) == "deny"
    assert decision(CREDITS_TOOL) == "allow"
    assert decision(WEATHER_TOOL) == "allow"
    # Each subagent may call only its own tools; get_credits belongs to no subagent.
    assert decision(DROPBOX_TOOL, UPDATE, "a1") == "allow"
    assert decision(CALENDAR_TOOL, UPDATE, "a1") == "deny"
    assert decision(CALENDAR_TOOL, SCHEDULE, "a2") == "allow"
    assert decision(DROPBOX_TOOL, SCHEDULE, "a2") == "deny"
    assert decision(DROPBOX_TOOL, "general-purpose", "a3") == "deny"
    for agent_type in (UPDATE, SCHEDULE, "general-purpose"):
        assert decision(CREDITS_TOOL, agent_type, "a4") == "deny"
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
    # Schedule first, then Dropbox.
    assert prompt.index("### ① 오늘의 일정") < prompt.index("### ② Dropbox 업데이트")
    assert "하루치만(days=1)" in prompt
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
    # Teammates by their friendly names (업뎃이, 일정이).
    assert status.getvalue().splitlines() == ["→ 업뎃이에게 물어보는 중...", "→ 일정이에게 물어보는 중..."]
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
    AssistantMessage(content=[TextBlock(text="*② Dropbox 업데이트*\n• 변경 없음")], model="m"),
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
    assert result.text == "*② Dropbox 업데이트*\n• 변경 없음"
    assert result.session_id == "sess-final"
    assert not result.failed and result.error is None
    assert seen == ["→ 업뎃이에게 물어보는 중..."]
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
    assert seen == ["→ 업뎃이에게 물어보는 중..."]

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
    assert main(["어제 Dropbox에서 누가 뭐 고쳤어?"]) == 0
    out, err = capsys.readouterr()
    assert out == "업뎃에게 맡길게요.\n*② Dropbox 업데이트*\n• 변경 없음\n"
    assert err == "→ 업뎃이에게 물어보는 중...\n"
    [client] = fake_sdk.instances
    assert [without_now_line(p) for p in client.prompts] == ["어제 Dropbox에서 누가 뭐 고쳤어?"]
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
        assert "Dropbox 업데이트 없음: 최근 24시간 동안 바뀐 파일이 없어요" in prompt
        assert "지난 브리핑(10/06 07:50) 이후 바뀐 파일이 없어요" in prompt
        assert "기간 안에 바뀐 파일 5개는 모두 박사님이 수정하신 거예요" in prompt
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


def test_the_dropbox_window_is_explained_from_since_basis_only():
    for prompt in (build_update_prompt(), build_update_prompt(direct=True)):
        assert "## 기간 (since_basis로만 쓴다)" in prompt
        assert '- default_24h → "최근 24시간 기준"' in prompt
        assert '- briefing_checkpoint → "지난 브리핑(10/06 07:50) 이후"' in prompt
        assert '- lookback_default → "지난 브리핑 기록이 없어 최근 24시간 기준"' in prompt
        assert '- since_hours → "10/03 14:20 이후"' in prompt
        assert "왜 그 기간인지(지난 브리핑 기록이 있었는지, 어떤 실행이었는지)는 짐작해서 덧붙이지 않는다." in prompt
        # The report's first line carries that wording, not a bare "since 이후".
        assert "Dropbox <folder> (<기간>, 파일 total_files개)" in prompt and "(since 이후," not in prompt
        # Every no-result line names its window the same way.
        assert "지난 브리핑 기록이 없어 최근 24시간 기준으로 봤는데, 바뀐 파일이 없어요" in prompt
        assert "Dropbox 업데이트 없음 (최근 24시간 기준): 기간 안에 바뀐 파일 5개는 모두 박사님이 수정하신 거예요" in prompt
        assert "since_basis가 default_24h, briefing_checkpoint, lookback_default이면 같은 줄 끝에" in prompt
        assert "since_basis가 since_hours나 lookback_default" not in prompt
    # 고뭉치 relays 업뎃's wording and never guesses why the window is what it is.
    mungchi = options().system_prompt
    assert '어느 기간을 봤는지는 업뎃이 적은 말(예: "최근 24시간 기준", "지난 브리핑(10/06 07:50) 이후")을 그대로 옮긴다.' in mungchi
    assert "왜 그 기간인지(지난 브리핑 기록이 있었는지, 어떤 실행이었는지)는 짐작해서 덧붙이지 않는다." in mungchi
    assert "어느 기간을 봤는지는 업뎃이 보고에 적는다" in mungchi
    for prompt in (mungchi, build_update_prompt(), build_update_prompt(direct=True)):
        assert "체크포인트" not in prompt and "정기 브리핑 실행" not in prompt


def test_a_briefing_in_conversation_has_all_four_parts():
    prompt = options().system_prompt
    section = prompt[prompt.index("## 브리핑") : prompt.index("### ① 오늘의 일정")]
    assert "대화 중에 브리핑이나 오늘 요약을 부탁받으면" in section and "건너뛴 브리핑 해줘" in section
    assert "아래 네 부분을 이 순서로 모두 담는다. 날씨나 크레딧을 빼지 않는다." in section
    # All four sources are asked at once: 고뭉치's own two tools and the two teammates.
    assert "한 번의 응답 안에서 get_weather, get_credits와 Agent 도구 두 번('일정' 에이전트, 업뎃)을 함께 호출한다." in section
    assert "🌤️ 날씨는 get_weather로 직접 확인한다." in section
    assert "① 오늘의 일정은 '일정' 에이전트에게 맡긴다" in section
    assert "② Dropbox 업데이트는 업뎃에게 맡긴다" in section
    assert "💳 크레딧은 get_credits로 직접 확인한다." in section
    # The format, in the order of the code-driven briefing: weather, ①, ②, credits.
    lines = ["  🌤️ 날씨: get_weather의 summary 한 줄", "  ① 오늘의 일정: 아래 규칙대로", "  ② Dropbox 업데이트: 아래 규칙대로", "  💳 크레딧: get_credits의 short_summary 그대로"]
    assert "\n".join(lines) in section
    # No blanket "never in a briefing" rule; and since the morning briefing became a relay (each bot
    # reports its own part), 고뭉치 has no program-made briefing run left to special-case.
    assert "브리핑에서는 get_weather와 get_credits를 부르지 않는다" not in prompt
    assert "Chat KHU 크레딧은 쓰지 않고" not in prompt
    assert "프로그램이 따로 붙인다" not in prompt and "프로그램이 만드는 브리핑" not in prompt
    # The morning reports' prompts are per run (with the date), never in a cached system prompt.
    from mungchi import briefing

    when = briefing.BriefTime.at(NOW)
    for persona in ("update", "schedule"):
        report = briefing.report_prompt(persona, when)
        assert report not in prompt and report not in build_options(env={}, persona=persona).system_prompt
    assert "2026-10-05" in briefing.report_prompt("schedule", when) and "2026-10-05" not in prompt


def test_folder_link_example_line_is_in_every_prompt_that_writes_one():
    """One link per subfolder: never "(폴더 열기: 폴더 열기)" again."""
    terminal_example = f"- 01_ProjectA\n  {EXAMPLE_FOLDER_LINK}\n  - 김공저: draft.tex (<modified>)"
    assert terminal_example in build_update_prompt()  # 업뎃 reporting to 고뭉치
    assert terminal_example in build_options(env={}, persona="update").system_prompt  # 업뎃 answering directly
    assert terminal_example.replace("\n", "\n  ") in MUNGCHI_SYSTEM_PROMPT  # 고뭉치 relaying it, indented
    assert SLACK_FOLDER_LINE_EXAMPLE == (
        "• *01_ProjectA* <https://www.dropbox.com/home/20_%EC%97%B0%EA%B5%AC-%EC%A7%84%ED%96%89/01_ProjectA|📂 열기>"
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


def test_direct_update_gets_only_the_dropbox_and_propose_tools(server_spy):
    opts = build_options(env={}, persona="update")
    assert opts.tools == []  # no built-in tools at all, not even Agent
    assert opts.allowed_tools == [DROPBOX_TOOL, PROPOSE_TOOL]
    assert "Agent" not in opts.allowed_tools and "Agent" in opts.disallowed_tools
    assert not opts.agents
    assert set(BLOCKED_BUILTINS) <= set(opts.disallowed_tools)
    assert opts.permission_mode == "dontAsk"
    assert opts.setting_sources == []
    assert opts.env == options().env
    assert server_spy[0] == ["check_dropbox_updates", "propose_calendar_events"]
    assert opts.hooks["PreToolUse"][0].hooks == [TOOL_GATES["update"]]
    assert set(opts.mcp_servers) == {SERVER_NAME}


def test_direct_schedule_gets_only_its_calendar_weather_and_propose_tools(server_spy):
    opts = build_options(env={}, persona="schedule")
    assert opts.tools == []
    assert opts.allowed_tools == [CALENDAR_TOOL, WEATHER_TOOL, PROPOSE_TOOL]
    assert "Agent" in opts.disallowed_tools
    assert not opts.agents
    assert set(BLOCKED_BUILTINS) <= set(opts.disallowed_tools)
    assert server_spy[-1] == ["get_schedule", "get_weather", "propose_calendar_events"]
    assert opts.hooks["PreToolUse"][0].hooks == [TOOL_GATES["schedule"]]


def test_mungchi_options_are_unchanged_by_personas(server_spy):
    default, explicit = options(), build_options(env={}, persona="mungchi")
    for field in (*SAFETY_FIELDS, "system_prompt"):
        assert getattr(default, field) == getattr(explicit, field), field
    assert explicit.tools == ["Agent"] and explicit.allowed_tools == ["Agent", CREDITS_TOOL, WEATHER_TOOL]
    assert explicit.disallowed_tools == BLOCKED_BUILTINS
    assert set(explicit.agents) == {"update", "schedule"}
    assert explicit.hooks["PreToolUse"][0].hooks == [tool_gate]
    assert TOOL_GATES["mungchi"] is tool_gate
    # 고뭉치's server keeps every data tool for its subagents and itself (built for this run).
    assert server_spy == [
        ["check_dropbox_updates", "get_schedule", "get_weather", "get_credits", "propose_calendar_events"]
    ] * 2


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
    # Same format; only the voice differs (plain report to 고뭉치 vs. 업뎃's own voice to the user).
    assert sub.split("## 보고 형식")[1].split("\n## 말투")[0] == direct.split("## 답 형식")[1].split("\n## 말투")[0]

    sub, direct = build_schedule_prompt(), build_schedule_prompt(direct=True)
    assert "고뭉치에게 한국어로 짧게 보고" in sub and "사용자에게 직접 한국어로 짧게 답한다" in direct
    assert (
        sub.split("## 보고 형식 (짧게)")[1].split("\n## 말투")[0]
        == direct.split("## 답 형식 (짧게)")[1].split("\n## 말투")[0]
    )
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
    assert opts.allowed_tools == [DROPBOX_TOOL, PROPOSE_TOOL]
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
    assert client.options.allowed_tools == [DROPBOX_TOOL, PROPOSE_TOOL] and not client.options.agents

    fake_sdk.instances = []
    lines = iter(["내일 일정은?", "종료"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))
    assert main(["--agent", "schedule"]) == 0
    [client] = fake_sdk.instances
    assert [without_now_line(p) for p in client.prompts] == ["내일 일정은?"]
    assert client.options.allowed_tools == [CALENDAR_TOOL, WEATHER_TOOL, PROPOSE_TOOL]
    out = capsys.readouterr().out
    assert "\n'일정'입니다. 캘린더 일정과 오늘·내일 날씨를 확인해 드릴게요." in out and "일정: 수고하셨습니다!" in out


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
    # One data tool; answering directly it can also turn a pasted note into a calendar proposal.
    assert UPDATE_TOOLS == [DROPBOX_TOOL]
    assert PERSONA_TOOLS[UPDATE] == [DROPBOX_TOOL, PROPOSE_TOOL]
    assert DATA_TOOLS == [DROPBOX_TOOL, CALENDAR_TOOL, WEATHER_TOOL, CREDITS_TOOL, PROPOSE_TOOL]
    assert [t.name for t in ALL_TOOLS] == [
        "check_dropbox_updates",
        "get_schedule",
        "get_weather",
        "get_credits",
        "propose_calendar_events",
    ]


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

    assert main(["--agent", "update", "Dropbox 업데이트 확인해줘"]) == 0
    assert os.environ["OVERLEAF_GIT_TOKEN"] == "olp_not-a-real-token"  # loaded from .env, then ignored
    [client] = fake_sdk.instances
    assert client.options.allowed_tools == [DROPBOX_TOOL, PROPOSE_TOOL]
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
    # The conversational briefing guidance is part of the cached, time-free prompt.
    for key in ("mungchi", "mungchi+slack"):
        assert "아래 네 부분을 이 순서로 모두 담는다" in first[key] and "💳 크레딧은 get_credits로 직접 확인한다" in first[key]
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
    assert main(["--brief"]) == 0  # the relay: 업뎃's report run (briefing mode) and 일정's (no Dropbox tool at all)
    assert len(server_tools) == 2 and not any(t.name == "check_dropbox_updates" for t in server_tools[1])
    assert main(["어제 Dropbox에서 누가 뭐 고쳤어?"]) == 0
    assert main(["--agent", "update", "누가 무슨 파일 고쳤어?"]) == 0
    asyncio.run(run_turn("업데이트 알려줘", extra_system_prompt=SLACK_FORMAT_PROMPT))  # a Slack turn
    with_dropbox = [tools for tools in server_tools if any(t.name == "check_dropbox_updates" for t in tools)]
    assert [_dropbox_mode(tools, monkeypatch) for tools in with_dropbox] == [True, False, False, False]
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
    assert direct.allowed_tools == [DROPBOX_TOOL, PROPOSE_TOOL]


# ---------------------------------------------------------------- 일정's get_weather tool


def _weather_decision(persona="mungchi", agent_type=None, agent_id=None):
    out = gate_decision(WEATHER_TOOL, {}, agent_type, agent_id, persona=persona)
    return out.get("hookSpecificOutput", {}).get("permissionDecision")


def test_get_weather_is_an_mcp_tool_owned_by_schedule_only():
    assert WEATHER_TOOL == "mcp__mungchi__get_weather"
    assert WEATHER_TOOL in DATA_TOOLS
    assert PERSONA_TOOLS[SCHEDULE] == [CALENDAR_TOOL, WEATHER_TOOL, PROPOSE_TOOL]
    assert WEATHER_TOOL not in PERSONA_TOOLS[UPDATE]
    mungchi = options()
    assert mungchi.agents["schedule"].tools == [CALENDAR_TOOL, WEATHER_TOOL, PROPOSE_TOOL]
    assert WEATHER_TOOL not in mungchi.agents["update"].tools
    assert WEATHER_TOOL in mungchi.allowed_tools  # 고뭉치 itself answers simple weather questions
    assert build_options(env={}, persona="update").allowed_tools == [DROPBOX_TOOL, PROPOSE_TOOL]


def test_get_weather_gate_allows_schedule_direct_its_subagent_and_mungchi_itself():
    # 일정 answering directly, 일정 as 고뭉치's subagent and 고뭉치's main agent: allowed.
    assert _weather_decision(persona="schedule") == "allow"
    assert _weather_decision(agent_type=SCHEDULE, agent_id="a2") == "allow"
    assert _weather_decision() == "allow"
    # 업뎃 directly or as a subagent, anyone else: denied.
    assert _weather_decision(persona="update") == "deny"
    assert _weather_decision(agent_type=UPDATE, agent_id="a1") == "deny"
    assert _weather_decision(agent_type="general-purpose", agent_id="a3") == "deny"
    assert _weather_decision(persona="schedule", agent_id="a1") == "deny"  # never from inside a subagent
    assert _weather_decision(persona="nobody") == "deny"

    async def ask(persona, tool, **extra):
        out = await TOOL_GATES[persona]({"tool_name": tool, "tool_input": {}, **extra}, "t1", None)
        return out["hookSpecificOutput"]["permissionDecision"]

    assert asyncio.run(ask("schedule", WEATHER_TOOL)) == "allow"
    assert asyncio.run(ask("update", WEATHER_TOOL)) == "deny"
    assert asyncio.run(ask("mungchi", WEATHER_TOOL)) == "allow"
    assert asyncio.run(ask("mungchi", WEATHER_TOOL, agent_type="schedule", agent_id="a2")) == "allow"
    assert asyncio.run(ask("mungchi", WEATHER_TOOL, agent_type="update", agent_id="a1")) == "deny"


def test_schedule_prompts_call_get_weather_for_weather_questions():
    from mungchi.agents import SCHEDULE_DESCRIPTION

    direct_system = build_options(env={}, persona="schedule").system_prompt
    for prompt in (build_schedule_prompt(), build_schedule_prompt(direct=True), direct_system):
        assert "get_weather" in prompt
        assert "날씨를 묻거나" in prompt and "야외 일정이나 이동(출퇴근 등)" in prompt
        assert "일정만 물으면(브리핑 포함) get_weather는 부르지 않는다" in prompt
        assert "summary 한 줄로 짧게" in prompt
        for key in ("today", "tomorrow", "current", "fine_dust", "precipitation_probability"):
            assert key in prompt
    assert "일정·날씨와 상관없는 요청" in direct_system
    # 업뎃 sends weather questions to 일정, and never sees the tool.
    update = build_options(env={}, persona="update").system_prompt
    assert "일정·약속·날씨는 '일정' 에이전트" in update and "get_weather" not in update
    # 고뭉치 answers simple weather questions itself, sends weather + schedule questions to 일정,
    # and leaves the weather out of the briefing (code adds the line).
    assert "날씨만 묻는 질문은 위처럼 get_weather로 직접 답한다" in MUNGCHI_SYSTEM_PROMPT
    assert "날씨와 일정이 함께 걸린 질문만 '일정' 에이전트에게 맡긴다" in MUNGCHI_SYSTEM_PROMPT
    assert "내일 비 오면 일정 바꿔야 할까?" in MUNGCHI_SYSTEM_PROMPT
    assert "Dropbox와 캘린더는 직접 다루지 않고" in MUNGCHI_SYSTEM_PROMPT
    assert "날씨는 맡기지 않는다" in MUNGCHI_SYSTEM_PROMPT
    # In a briefing 고뭉치 fetches the weather itself (unless the run's prompt says code adds it).
    assert "🌤️ 날씨는 get_weather로 직접 확인한다" in MUNGCHI_SYSTEM_PROMPT
    assert "브리핑에서는 get_weather와 get_credits를 부르지 않는다" not in MUNGCHI_SYSTEM_PROMPT
    assert "날씨 질문" in SCHEDULE_DESCRIPTION and "날씨" in options().agents["schedule"].description


def test_help_says_schedule_also_does_weather():
    help_text = build_parser().format_help()
    assert "'일정'(캘린더·날씨)" in help_text
    assert '--agent schedule "내일 비 오면 일정 바꿔야 할까?"' in help_text


# ---------------------------------------------------------------- 고뭉치's own get_credits / get_weather


def _decision(tool, persona="mungchi", agent_type=None, agent_id=None, tool_input=None):
    out = gate_decision(tool, tool_input or {}, agent_type, agent_id, persona=persona)
    return out.get("hookSpecificOutput", {}).get("permissionDecision")


def test_mungchi_main_agent_may_call_exactly_agent_get_credits_and_get_weather():
    assert CREDITS_TOOL == "mcp__mungchi__get_credits"
    assert _decision(CREDITS_TOOL) == "allow"
    assert _decision(WEATHER_TOOL) == "allow"
    for subagent in ("update", "schedule"):
        assert _decision("Agent", tool_input={"subagent_type": subagent}) is None  # pre-approved, not denied
    # Dropbox and the calendar stay delegated; everything else is denied outright.
    assert _decision(DROPBOX_TOOL) == "deny"
    assert _decision(CALENDAR_TOOL) == "deny"
    assert _decision("Agent", tool_input={"subagent_type": "general-purpose"}) == "deny"
    for tool in ("Bash", "Read", "Write", "WebFetch", "WebSearch", "mcp__other__tool", "mcp__mungchi__unknown", ""):
        assert _decision(tool) == "deny"

    async def ask(tool):
        out = await tool_gate({"tool_name": tool, "tool_input": {}}, "t1", None)
        return out["hookSpecificOutput"]["permissionDecision"]

    assert asyncio.run(ask(CREDITS_TOOL)) == "allow"
    assert asyncio.run(ask(DROPBOX_TOOL)) == "deny"


def test_get_credits_belongs_to_mungchi_only():
    # 업뎃: no get_credits and no get_weather, directly or as a subagent.
    for tool in (CREDITS_TOOL, WEATHER_TOOL):
        assert _decision(tool, persona="update") == "deny"
        assert _decision(tool, agent_type=UPDATE, agent_id="a1") == "deny"
    # 일정: get_weather but no get_credits, directly or as a subagent.
    assert _decision(WEATHER_TOOL, persona="schedule") == "allow"
    assert _decision(WEATHER_TOOL, agent_type=SCHEDULE, agent_id="a2") == "allow"
    assert _decision(CREDITS_TOOL, persona="schedule") == "deny"
    assert _decision(CREDITS_TOOL, agent_type=SCHEDULE, agent_id="a2") == "deny"
    assert _decision(CREDITS_TOOL, persona="nobody") == "deny"
    # Neither the subagents nor the direct personas are given the tool at all.
    mungchi = options()
    assert all(CREDITS_TOOL not in agent.tools for agent in mungchi.agents.values())
    assert CREDITS_TOOL not in PERSONA_TOOLS[UPDATE] and CREDITS_TOOL not in PERSONA_TOOLS[SCHEDULE]
    assert build_options(env={}, persona="update").allowed_tools == [DROPBOX_TOOL, PROPOSE_TOOL]
    assert build_options(env={}, persona="schedule").allowed_tools == [CALENDAR_TOOL, WEATHER_TOOL, PROPOSE_TOOL]


def test_direct_personas_servers_never_get_get_credits(server_spy):
    build_options(env={}, persona="update")
    build_options(env={}, persona="schedule")
    build_options(env={})
    assert server_spy == [
        ["check_dropbox_updates", "propose_calendar_events"],
        ["get_schedule", "get_weather", "propose_calendar_events"],
        ["check_dropbox_updates", "get_schedule", "get_weather", "get_credits", "propose_calendar_events"],
    ]


def test_mungchi_prompt_says_token_means_chat_khu_credits_and_to_use_its_tools():
    prompt = options().system_prompt
    assert "'토큰'·'크레딧'은 Chat KHU(Mindlogic) API 크레딧을 말한다" in prompt
    assert "암호화폐나 코인 가격이 아니다" in prompt
    assert "get_credits를 한 번 불러 답한다" in prompt
    assert "네가 쓰는 도구는 Agent, get_credits, get_weather 세 가지뿐이다" in prompt
    assert "도구가 있는 일을 할 수 없다고 말하지 않는다" in prompt
    assert "오류를 알리면(ok: false, error) 무엇이 실패했는지 짧게 그대로 전하고" in prompt
    assert "날씨랑 토큰 좀 말해봐" in prompt and "한 응답 안에서 함께 부른다" in prompt
    # 일정's description no longer claims every weather question.
    assert "단순한 날씨 질문은 고뭉치가 get_weather로 직접 답한다" in options().agents["schedule"].description


def test_direct_prompts_explain_token_without_offering_a_tool():
    for persona in ("update", "schedule"):
        prompt = build_options(env={}, persona=persona).system_prompt
        assert "'토큰'·'크레딧'은 Chat KHU(Mindlogic) API 크레딧을 말한다(암호화폐가 아님." in prompt
        assert "python -m mungchi --credits" in prompt
        assert "get_credits" not in prompt and "Agent" not in prompt


def test_renderer_answer_starts_after_mungchis_own_tool_calls():
    renderer = Renderer(echo=False)
    renderer.handle(
        AssistantMessage(
            content=[
                TextBlock(text="확인해 볼게요."),
                ToolUseBlock(id="t1", name=CREDITS_TOOL, input={}),
                ToolUseBlock(id="t2", name=WEATHER_TOOL, input={}),
            ],
            model="m",
        )
    )
    renderer.handle(AssistantMessage(content=[TextBlock(text="남은 크레딧은 9,050.5예요.")], model="m"))
    assert renderer.result().text == "남은 크레딧은 9,050.5예요."
    assert renderer.status_lines == []  # no "→ ...에게 물어보는 중" for its own tools


def test_help_says_mungchi_checks_tokens_and_weather_itself():
    help_text = build_parser().format_help()
    assert "Chat KHU 크레딧('토큰')과 오늘·내일 날씨는 고뭉치가 직접 확인합니다(get_credits, get_weather)" in help_text
    assert 'python -m mungchi "날씨랑 토큰 좀 알려줘"' in help_text
    assert "'날씨랑 토큰 좀 알려줘'처럼 짧게 물으면 LLM 호출 없이 바로 답합니다" in help_text
    assert "서비스 상태(실행 중인 코드 버전 포함)" in help_text


# ---------------------------------------------------------------- calendar events from a pasted note

from mungchi.agents import SCHEDULE_DESCRIPTION, SCHEDULE_PROMPT, UPDATE_PROMPT, build_propose_section  # noqa: E402
from mungchi.state import StateStore, utcnow  # noqa: E402
from mungchi.tools import event_proposals, macos_calendar  # noqa: E402
from mungchi.tools.event_proposals import CONFIRM_QUESTION, CreationOutcome, CreationResult  # noqa: E402

NOTE_EVENT = {"title": "신임교수모임 (10월)", "date": "2099-10-22", "start_time": "12:00", "notes": "발표: 홍길동 교수님"}


def _propose_decision(persona="mungchi", agent_type=None, agent_id=None):
    out = gate_decision(PROPOSE_TOOL, {}, agent_type, agent_id, persona=persona)
    return out.get("hookSpecificOutput", {}).get("permissionDecision")


def test_propose_calendar_events_is_gated_per_persona():
    assert PROPOSE_TOOL == "mcp__mungchi__propose_calendar_events"
    # Allowed: 업뎃 directly, 일정 directly, 일정 as 고뭉치's subagent.
    assert _propose_decision(persona="update") == "allow"
    assert _propose_decision(persona="schedule") == "allow"
    assert _propose_decision(agent_type=SCHEDULE, agent_id="a2") == "allow"
    # Denied: 고뭉치 itself (it delegates to 일정), 업뎃 as a subagent (Dropbox only), anyone else.
    assert _propose_decision() == "deny"
    assert _propose_decision(agent_type=UPDATE, agent_id="a1") == "deny"
    assert _propose_decision(agent_type="general-purpose", agent_id="a3") == "deny"
    for persona in ("update", "schedule"):
        assert _propose_decision(persona=persona, agent_id="a1") == "deny"  # never from inside a subagent
    assert _propose_decision(persona="nobody") == "deny"

    async def ask(persona, **extra):
        out = await TOOL_GATES[persona]({"tool_name": PROPOSE_TOOL, "tool_input": {}, **extra}, "t1", None)
        return out["hookSpecificOutput"]["permissionDecision"]

    assert asyncio.run(ask("update")) == "allow" and asyncio.run(ask("schedule")) == "allow"
    assert asyncio.run(ask("mungchi")) == "deny"
    assert asyncio.run(ask("mungchi", agent_type="schedule", agent_id="a2")) == "allow"
    assert asyncio.run(ask("mungchi", agent_type="update", agent_id="a1")) == "deny"
    # The tool lists match the gates.
    mungchi = options()
    assert PROPOSE_TOOL not in mungchi.allowed_tools
    assert PROPOSE_TOOL in mungchi.agents["schedule"].tools and PROPOSE_TOOL not in mungchi.agents["update"].tools
    assert PROPOSE_TOOL in build_options(env={}, persona="update").allowed_tools
    assert PROPOSE_TOOL in build_options(env={}, persona="schedule").allowed_tools


def test_no_tool_can_create_calendar_events():
    names = {t.name for t in ALL_TOOLS}
    assert names == {"check_dropbox_updates", "get_schedule", "get_weather", "get_credits", "propose_calendar_events"}
    [propose] = [t for t in ALL_TOOLS if t.name == "propose_calendar_events"]
    assert "캘린더에 추가하지는 않는다" in propose.description and "네가 추가할 방법은 없다" in propose.description


def _bound_propose_tool(tools):
    [tool] = [t for t in tools if t.name == "propose_calendar_events"]
    return tool


# The user's Mac calendars: the five default categories and one more.
CATEGORY_CALENDARS = ["Family", "Teaching", "Research", "Event-Outside", "Event-KHU"]


def _call_propose(tool, monkeypatch, **args):
    class App:
        def authorization_status(self):
            return macos_calendar.GRANTED

        def list_writable_calendars(self):
            return [{"name": name, "source": "iCloud", "is_default": name == "연구"} for name in ["연구", *CATEGORY_CALENDARS]]

        def find_similar_events(self, start, end, title):
            return []

    monkeypatch.setattr(config, "current_platform", lambda: "darwin")
    monkeypatch.setattr(macos_calendar, "default_adapter", lambda tz: App())
    result = asyncio.run(tool.handler({"events": [NOTE_EVENT], "source_note": "메모", **args}))
    return json.loads(result["content"][0]["text"])


@pytest.mark.parametrize("persona", ["update", "schedule", "mungchi"])
def test_build_options_binds_the_conversation_key_into_its_own_propose_tool(server_tools, monkeypatch, persona):
    """For 고뭉치 the tool sits on the run's shared server, where its 일정 subagent calls it."""
    key = event_proposals.slack_conversation_key(persona, "C1", "1700000000.000100")
    build_options(env={}, persona=persona, conversation_key=key)
    build_options(env={}, persona=persona)  # another run in the same process, without a key
    bound, unbound = (_bound_propose_tool(tools) for tools in server_tools)
    assert bound is not unbound
    payload = _call_propose(bound, monkeypatch, suggested_category="Event-KHU")
    assert payload["ok"] and payload["can_confirm"]
    assert payload["confirm_question"] == (
        "카테고리를 골라주세요 (추천: Event-KHU) — 1 Family · 2 Teaching · 3 Research · 4 Event-Outside · 5 Event-KHU"
        " · 번호/이름으로 답하거나 '네'(추천대로), '아니요'(취소)"
    )
    store = StateStore(config.get_state_path())
    pending = store.pending_proposal(key, utcnow())
    assert pending["events"][0]["title"] == "신임교수모임 (10월)" and pending["suggested_category"] == "Event-KHU"
    assert [c["label"] for c in pending["categories"]] == CATEGORY_CALENDARS
    store.clear_pending_proposal(key)
    assert _call_propose(unbound, monkeypatch)["can_confirm"] is False
    assert store.load().get("pending_events") == {}


def test_prompts_propose_and_end_with_the_exact_question():
    assert CONFIRM_QUESTION == "캘린더에 추가할까요? (네 / 아니요 / 고칠 내용)"
    direct = {p: build_options(env={}, persona=p).system_prompt for p in ("update", "schedule")}
    for prompt in (*direct.values(), SCHEDULE_PROMPT):
        assert "## 메모로 일정 추가 (propose_calendar_events)" in prompt
        assert (
            f'마지막 줄은 결과의 confirm_question(카테고리를 고르라는 질문, 또는 "{CONFIRM_QUESTION}")을 '
            "한 글자도 바꾸지 말고 그대로 쓴다"
        ) in prompt
        assert '예: "신임교수모임 (10월)"' in prompt and '"발표: 홍길동 교수님"' in prompt
        assert '"오후 12시"는 12:00(정오)이다. "오전 12시"는 00:00(자정)으로 넣고' in prompt
        assert "weekday_in_text" in prompt and "오늘이거나 오늘 뒤에 오는 가장 가까운 그 날짜" in prompt
        assert "시작 시각이 없으면 짐작하지 않는다" in prompt and "몇 시인지 묻는다" in prompt
        assert "일정이 추가되었다거나 등록되었다고 절대 말하지 않는다" in prompt
        assert "can_confirm이 false면 이 질문 대신 note를 전한다" in prompt
        # The agent suggests a category; the user picks it.
        assert "suggested_category에 가장 알맞은 카테고리 하나를 추천만 하고, 사용자 대신 고르지 않는다" in prompt
        for line in (
            "- Family: 가족·개인 일",
            "- Teaching: 강의, 수업, 학생, 채점, 조교(TA), 시험",
            "- Research: 논문, 공동 연구, 실험, IRB, 연구 회의",
            "- Event-KHU: 경희대 안의 회의·행사, 학과·단과대 행사(예: 신임교수모임)",
            "- Event-Outside: 경희대 밖의 학회, 워크숍, 세미나, 외부 행사",
        ):
            assert line in prompt
        assert '사용자가 정해 줬을 때만(예: "1번은 Research, 2번은 Event-KHU") 그 일정의 category에 넣는다' in prompt
    for prompt in direct.values():
        assert "사용자 메시지 맨 앞의 [지금: ...] 줄의 날짜를 기준으로" in prompt
        assert "앞의 제안은 이미 취소된 것이다" in prompt
        assert "다시 불러 새 미리보기와 결과의 confirm_question으로 끝낸다" in prompt
        assert "Agent" not in prompt
    assert "고뭉치가 맡긴 글 맨 앞의 [지금: ...] 줄의 날짜를 기준으로" in SCHEDULE_PROMPT
    assert "고뭉치에게 그대로 보고하고" in SCHEDULE_PROMPT
    # 업뎃 handles notes directly, but its subagent under 고뭉치 never proposes.
    assert "아래 '메모로 일정 추가'대로, 사진을 보내면 아래 '사진으로 일정 추가'대로 직접 처리한다" in direct["update"]
    assert "propose_calendar_events" not in UPDATE_PROMPT
    assert options().agents["schedule"].prompt == SCHEDULE_PROMPT
    assert SCHEDULE_PROMPT == build_schedule_prompt() + build_propose_section()


def test_mungchi_routes_notes_to_schedule_and_relays_the_question():
    prompt = options().system_prompt
    section = prompt[prompt.index("## 메모로 일정 추가") : prompt.index("## 그 밖의 요청")]
    assert "'일정' 에이전트에게 맡긴다. 업뎃에게는 맡기지 않는다." in section
    assert "너에게는 일정을 제안하거나 추가하는 도구가 없다" in section
    assert "메모 원문 전체(고치거나 줄이지 말고 그대로)" in section and "[지금: ...] 줄" in section
    assert (
        f'답의 마지막 줄은 보고에 있는 확인 질문(카테고리를 고르라는 질문, 또는 "{CONFIRM_QUESTION}")을 '
        "한 글자도 바꾸지 말고 그대로 쓴다"
    ) in section
    assert "카테고리(넣을 캘린더)는 사용자가 고른다" in section
    assert "일정이 추가되었다거나 등록되었다고 절대 말하지 않는다" in section
    assert "메모 원문과 고칠 내용을 함께 '일정' 에이전트에게 다시 맡겨" in section
    assert "propose_calendar_events" not in prompt  # not its tool
    assert "네가 쓰는 도구는 Agent, get_credits, get_weather 세 가지뿐이다" in prompt
    assert "메모" in SCHEDULE_DESCRIPTION and "캘린더에 추가" in SCHEDULE_DESCRIPTION


def test_nobody_is_told_to_claim_events_were_added():
    texts = [*_every_system_prompt().values(), *(t.description for t in ALL_TOOLS)]
    for text in texts:
        for claim in ("추가했어요", "등록했어요", "추가했습니다", "등록했습니다", "넣었어요"):
            assert claim not in text


def test_renderer_notices_proposals_also_inside_a_subagent():
    direct = Renderer(echo=False)
    direct.handle(AssistantMessage(content=[ToolUseBlock(id="t1", name=PROPOSE_TOOL, input={})], model="m"))
    direct.handle(AssistantMessage(content=[TextBlock(text="• 10/22(목) ...")], model="m"))
    assert direct.result().proposed and direct.result().text == "• 10/22(목) ..."
    nested = Renderer(echo=False)
    nested.handle(
        AssistantMessage(content=[ToolUseBlock(id="t2", name=PROPOSE_TOOL, input={})], model="m", parent_tool_use_id="t0")
    )
    assert nested.result().proposed
    assert not Renderer(echo=False).result().proposed


class ProposingSDKClient(FakeSDKClient):
    """A chat client whose agent proposes calendar events for some messages (the tool's effect, by hand)."""

    store: StateStore
    key: str
    propose_on: set[str] = set()

    async def query(self, prompt, session_id="default"):
        await super().query(prompt, session_id)
        typed = without_now_line(prompt)
        if typed in self.propose_on:
            items, _ = event_proposals.normalize_events([NOTE_EVENT], tz=ZoneInfo("Asia/Seoul"), now=NOW)
            proposal = {"id": typed, "calendar": "연구", "events": [e.to_state() for e in items]}
            self.store.save_pending_proposal(self.key, proposal, utcnow())


@pytest.fixture
def chat(monkeypatch, tmp_path):
    """Runs the terminal chat with scripted input; returns (prompts asked, created proposals, store, client prompts)."""
    store = StateStore(tmp_path / "state.json")
    key = "cli:test"
    ProposingSDKClient.store, ProposingSDKClient.key = store, key
    FakeSDKClient.instances = []
    monkeypatch.setattr(main_module, "ClaudeSDKClient", ProposingSDKClient)

    def run(lines, propose_on, persona="schedule"):
        ProposingSDKClient.propose_on = set(propose_on)
        asked: list[str] = []
        created: list[dict] = []
        feed = iter(lines)

        def fake_input(prompt=""):
            asked.append(prompt)
            try:
                return next(feed)
            except StopIteration:
                raise EOFError from None

        def create(proposal):
            created.append(dict(proposal))
            return CreationOutcome(
                results=[CreationResult(summary="10/22(목) 12:00–13:00 신임교수모임 (10월)", ok=True, calendar="연구")]
            )

        monkeypatch.setattr("builtins.input", fake_input)
        options = build_options(persona=persona, conversation_key=key)
        code = asyncio.run(run_chat(options, persona, conversation_key=key, store=store, create=create))
        assert code == 0
        [client] = FakeSDKClient.instances[-1:]
        return asked, created, store, [without_now_line(p) for p in client.prompts]

    return run


QUESTION = event_proposals.CLI_CONFIRM_PROMPT


def test_chat_yes_creates_by_code(chat, capsys):
    asked, created, store, prompts = chat(["메모", "네", "종료"], {"메모"})
    assert asked == ["\n나> ", QUESTION, "\n나> "]
    assert QUESTION == "캘린더에 추가할까요? [네/아니요] "
    assert [p["id"] for p in created] == ["메모"] and prompts == ["메모"]  # "네" never reaches the agent
    assert "✅ 캘린더에 추가했어요\n• 10/22(목) 12:00–13:00 신임교수모임 (10월) · 캘린더: 연구" in capsys.readouterr().out
    assert store.pending_proposal("cli:test", utcnow()) is None


def test_chat_no_cancels(chat, capsys):
    asked, created, store, prompts = chat(["메모", "아니요", "종료"], {"메모"})
    assert created == [] and prompts == ["메모"] and "취소했어요" in capsys.readouterr().out
    assert store.pending_proposal("cli:test", utcnow()) is None


def test_chat_other_answers_go_to_the_agent_and_the_new_proposal_is_asked_about(chat):
    asked, created, store, prompts = chat(["메모", "시간은 1시로 바꿔줘", "", "네", "종료"], {"메모", "시간은 1시로 바꿔줘"})
    assert prompts == ["메모", "시간은 1시로 바꿔줘"]
    assert asked == ["\n나> ", QUESTION, QUESTION, QUESTION, "\n나> "]  # an empty answer is asked again
    assert [p["id"] for p in created] == ["시간은 1시로 바꿔줘"]  # only the latest proposal


def test_chat_without_a_proposal_never_asks_and_end_of_input_drops_it(chat):
    asked, created, store, prompts = chat(["안녕", "종료"], set())
    assert asked == ["\n나> ", "\n나> "] and created == []
    asked, created, store, prompts = chat(["메모"], {"메모"})  # input ends at the question
    assert asked == ["\n나> ", QUESTION] and created == []
    assert store.pending_proposal("cli:test", utcnow()) is None


def test_main_chat_binds_one_key_to_both_the_tool_and_the_question(monkeypatch, server_tools):
    monkeypatch.setattr(event_proposals, "cli_conversation_key", lambda: "cli:fixed")
    seen = {}

    async def fake_run_chat(options, persona, *, conversation_key=None, **kwargs):
        seen.update(persona=persona, key=conversation_key)
        return 0

    monkeypatch.setattr(main_module, "run_chat", fake_run_chat)
    assert main(["--agent", "update"]) == 0
    assert seen == {"persona": "update", "key": "cli:fixed"}
    _call_propose(_bound_propose_tool(server_tools[-1]), monkeypatch)
    assert StateStore(config.get_state_path()).pending_proposal("cli:fixed", utcnow()) is not None


class OneShotProposingClient(FakeSDKClient):
    async def receive_response(self):
        yield AssistantMessage(
            content=[ToolUseBlock(id="t1", name="Agent", input={"subagent_type": "schedule", "prompt": "..."})], model="m"
        )
        yield AssistantMessage(
            content=[ToolUseBlock(id="t2", name=PROPOSE_TOOL, input={})], model="m", parent_tool_use_id="t1"
        )
        yield AssistantMessage(content=[TextBlock(text="• 10/22(목) 12:00–13:00 신임교수모임 (10월)")], model="m")
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2, session_id="s")


def test_one_shot_shows_the_preview_and_points_to_chat_or_slack(monkeypatch, capsys, server_tools):
    FakeSDKClient.instances = []
    monkeypatch.setattr(main_module, "ClaudeSDKClient", OneShotProposingClient)
    assert main(["이 메모 캘린더에 넣어줘: 10월 22일(목) 오후 12시 신임교수모임"]) == 0
    out, err = capsys.readouterr()
    assert "• 10/22(목) 12:00–13:00 신임교수모임 (10월)" in out
    assert f"[참고] {event_proposals.ONE_SHOT_NOTE}" in err
    assert "대화 모드" in err and "Slack" in err
    # The one-shot run's tool has no conversation: it never stores anything to confirm.
    payload = _call_propose(_bound_propose_tool(server_tools[-1]), monkeypatch)
    assert payload["can_confirm"] is False and StateStore(config.get_state_path()).load().get("pending_events") is None


# ---------------------------------------------------------------- terminal chat with categories

CATEGORY_ROWS = [{"label": n, "calendar": n, "aliases": []} for n in CATEGORY_CALENDARS]
CATEGORY_QUESTION = (
    "카테고리를 골라주세요 (추천: Event-KHU) [1 Family · 2 Teaching · 3 Research · 4 Event-Outside · 5 Event-KHU / 네 / 아니요] "
)


class CategoryProposingSDKClient(ProposingSDKClient):
    async def query(self, prompt, session_id="default"):
        await FakeSDKClient.query(self, prompt, session_id)
        typed = without_now_line(prompt)
        if typed in self.propose_on:
            items, _ = event_proposals.normalize_events([NOTE_EVENT], tz=ZoneInfo("Asia/Seoul"), now=NOW)
            proposal = {
                "id": typed,
                "calendar": None,
                "categories": CATEGORY_ROWS,
                "suggested_category": "Event-KHU",
                "events": [e.to_state() for e in items],
            }
            self.store.save_pending_proposal(self.key, proposal, utcnow())


@pytest.mark.parametrize("answer,label", [("2", "Teaching"), ("research", "Research"), ("네", "Event-KHU"), ("khu", "Event-KHU")])
def test_chat_asks_for_a_numbered_category_and_creates_by_code(chat, monkeypatch, capsys, answer, label):
    monkeypatch.setattr(main_module, "ClaudeSDKClient", CategoryProposingSDKClient)
    asked, created, store, prompts = chat(["메모", answer, "종료"], {"메모"})
    assert asked == ["\n나> ", CATEGORY_QUESTION, "\n나> "]
    assert [p["chosen_category"] for p in created] == [label] and prompts == ["메모"]  # never reaches the agent
    assert "✅ 캘린더에 추가했어요" in capsys.readouterr().out
    assert store.pending_proposal("cli:test", utcnow()) is None


def test_chat_asks_again_for_an_unclear_category_and_cancels_on_no(chat, monkeypatch, capsys):
    monkeypatch.setattr(main_module, "ClaudeSDKClient", CategoryProposingSDKClient)
    asked, created, store, prompts = chat(["메모", "event", "9", "아니요", "종료"], {"메모"})
    assert asked == ["\n나> ", CATEGORY_QUESTION, CATEGORY_QUESTION, CATEGORY_QUESTION, "\n나> "]
    out = capsys.readouterr().out
    assert "'event'에 맞는 카테고리가 여러 개예요: 4 Event-Outside · 5 Event-KHU." in out
    assert "1~5 가운데 번호로 골라 주세요" in out and "취소했어요" in out
    assert created == [] and prompts == ["메모"]


def test_chat_sends_anything_else_to_the_agent_with_categories(chat, monkeypatch):
    monkeypatch.setattr(main_module, "ClaudeSDKClient", CategoryProposingSDKClient)
    asked, created, store, prompts = chat(["메모", "1번은 Research로", "5", "종료"], {"메모", "1번은 Research로"})
    assert prompts == ["메모", "1번은 Research로"]
    assert [(p["id"], p["chosen_category"]) for p in created] == [("1번은 Research로", "Event-KHU")]


# ---------------------------------------------------------------- photos -> calendar: run_turn and --image

import base64  # noqa: E402

from mungchi import images as image_prep  # noqa: E402


class ImageSDKClient(FakeSDKClient):
    """Records what ``query`` got: a string, or the messages of a streaming-input iterable."""

    async def query(self, prompt, session_id="default"):
        if isinstance(prompt, str):
            self.prompts.append(prompt)
            return
        async for message in prompt:
            self.prompts.append(message)


@pytest.fixture
def image_sdk(monkeypatch):
    FakeSDKClient.instances = []
    monkeypatch.setattr(main_module, "ClaudeSDKClient", ImageSDKClient)
    return ImageSDKClient


PHOTO = ("image/jpeg", b"\xff\xd8\xff\xe0fake-jpeg")


def test_run_turn_sends_images_as_content_blocks_in_the_sdk_streaming_format(image_sdk):
    result = asyncio.run(run_turn("이 포스터 일정 넣어줘", persona="update", resume="sess-1", images=[PHOTO, ("image/png", b"png")]))
    assert result.text.endswith("변경 없음")
    [client] = image_sdk.instances
    [message] = client.prompts
    assert set(message) == {"type", "message", "parent_tool_use_id"} and message["type"] == "user"
    assert message["parent_tool_use_id"] is None and message["message"]["role"] == "user"
    first, second, text = message["message"]["content"]
    assert first == {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(PHOTO[1]).decode()}}
    assert second["source"]["media_type"] == "image/png"
    assert text["type"] == "text" and without_now_line(text["text"]) == "이 포스터 일정 넣어줘"  # the time stamp first
    assert client.options.resume == "sess-1"  # a photo turn continues the thread's session like any other


def test_a_photo_without_text_asks_for_its_events(image_sdk):
    asyncio.run(run_turn("  ", persona="schedule", images=[PHOTO]))
    [message] = image_sdk.instances[0].prompts
    assert without_now_line(message["message"]["content"][-1]["text"]) == "이 이미지에 있는 일정을 캘린더에 등록해줘"
    assert image_prep.DEFAULT_IMAGE_PROMPT == "이 이미지에 있는 일정을 캘린더에 등록해줘"


def test_text_turns_are_still_plain_strings(image_sdk):
    asyncio.run(run_turn("질문", persona="update"))
    asyncio.run(run_turn("질문", persona="update", images=[]))
    assert all(isinstance(p, str) for c in image_sdk.instances for p in c.prompts)


def test_only_direct_personas_take_images_and_at_most_five(image_sdk):
    with pytest.raises(ValueError, match="업뎃이나 일정에게만"):
        asyncio.run(run_turn("q", persona="mungchi", images=[PHOTO]))
    with pytest.raises(ValueError, match="5장까지만"):
        asyncio.run(run_turn("q", persona="update", images=[PHOTO] * 6))
    assert image_sdk.instances == []  # nothing was started


def test_direct_prompts_explain_photos_and_moongchi_never_gets_them():
    for persona in ("update", "schedule"):
        prompt = build_options(env={}, persona=persona).system_prompt
        section = prompt[prompt.index("## 사진으로 일정 추가") : prompt.index("## 지금 시각")]
        assert "날짜, 시각, 제목, 장소, 발표자" in section and "한국어와 영어를 모두 읽는다" in section
        assert "지어내지 않고" in section and "미리보기 뒤에 묻는다" in section
        assert "suggested_category를 추천해 propose_calendar_events를 한 번 부르고" in section
        assert "사진에 일정이 없으면" in section and "한 줄로 답한다" in section
        assert "Agent" not in section
    assert "## 사진으로 일정 추가" not in options().system_prompt
    assert "## 사진으로 일정 추가" not in SCHEDULE_PROMPT and "## 사진으로 일정 추가" not in UPDATE_PROMPT


def _photo_file(tmp_path, name="poster.jpg", size=(2000, 1500)):
    from PIL import Image

    path = tmp_path / name
    Image.new("RGB", size, "white").save(path, format="JPEG" if name.endswith(".jpg") else "PNG")
    return str(path)


def test_cli_image_option_rules(tmp_path, capsys):
    parser = build_parser()
    assert parser.parse_args(["--agent", "update", "--image", "a.jpg", "--image", "b.png"]).image == ["a.jpg", "b.png"]
    assert "--image" in parser.format_help() and "최대 5장" in parser.format_help()
    for argv, message in (
        (["--image", "a.jpg"], "--image는 --agent update 또는 --agent schedule과 함께 써야 합니다"),
        (["--agent", "update", *sum((["--image", f"{i}.jpg"] for i in range(6)), [])], "--image는 5장까지만 쓸 수 있습니다"),
        (["--brief", "--image", "a.jpg"], "--image는 --brief"),
        (["--agent", "update", "--image", "a.jpg", "slack"], "--image는 --brief"),
    ):
        with pytest.raises(SystemExit) as caught:
            main(argv)
        assert caught.value.code == 2 and message in capsys.readouterr().err


def test_cli_image_one_shot_sends_the_prepared_photos(image_sdk, tmp_path, capsys):
    photo = _photo_file(tmp_path)
    assert main(["--agent", "update", "--image", photo, "Research로 넣을 거야"]) == 0
    [client] = image_sdk.instances
    [message] = client.prompts
    image_block, text = message["message"]["content"]
    assert image_block["source"]["media_type"] == "image/jpeg"
    from PIL import Image

    assert Image.open(io.BytesIO(base64.b64decode(image_block["source"]["data"]))).size == (1568, 1176)
    assert without_now_line(text["text"]) == "Research로 넣을 거야"
    assert client.options.system_prompt == build_options(env={}, persona="update").system_prompt


def test_cli_image_errors_are_korean_and_exit_1(image_sdk, tmp_path, capsys):
    assert main(["--agent", "schedule", "--image", str(tmp_path / "missing.jpg"), "q"]) == 1
    assert "[오류] 사진 파일을 찾을 수 없어요" in capsys.readouterr().err and image_sdk.instances == []


def test_cli_image_chat_mode_sends_the_photos_with_the_first_message_only(image_sdk, tmp_path, monkeypatch, capsys):
    photo = _photo_file(tmp_path, "a.png", (100, 80))
    lines = iter(["", "고마워", "종료"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))
    assert main(["--agent", "schedule", "--image", photo]) == 0
    [client] = image_sdk.instances
    first, second = client.prompts
    assert [b["type"] for b in first["message"]["content"]] == ["image", "text"]
    assert without_now_line(first["message"]["content"][-1]["text"]) == "이 이미지에 있는 일정을 캘린더에 등록해줘"
    assert isinstance(second, str) and without_now_line(second) == "고마워"
    assert "사진 1장을 첫 메시지와 함께 보냅니다" in capsys.readouterr().out
