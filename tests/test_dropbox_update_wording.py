"""업뎃 says "Dropbox 업데이트", never "공저자".

The user felt "공저자 업데이트" / "공저자 변경" read like keeping watch on
co-authors. Every text a bot can show, and every prompt that shapes what the
bots say, now calls it "Dropbox 업데이트". The only "공저자" left is in 업뎃's
wording rule, which tells it not to use the word. Behaviour is unchanged: only
files changed by someone else are listed, each under the editor's name.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from dropbox.files import FileMetadata, FileSharingInfo

import mungchi
from mungchi import agents, briefing, main as main_module, phrases, slack_bot
from mungchi.agents import (
    MUNGCHI_SYSTEM_PROMPT,
    UPDATE_WORDING_RULE,
    build_direct_prompt,
    build_propose_section,
    build_schedule_prompt,
    build_update_prompt,
)
from mungchi.main import build_options, build_parser
from mungchi.personas import PERSONAS, SCHEDULE, UPDATE
from mungchi.slack_format import SLACK_FORMAT_PROMPT
from mungchi.tools import ALL_TOOLS
from mungchi.tools.dropbox_tool import collect_updates

WORD = "공저자"
SEOUL = ZoneInfo("Asia/Seoul")
SRC = Path(mungchi.__file__).resolve().parent
REPO = SRC.parent.parent

# Morning (scheduled), afternoon, evening (today and tomorrow) and night runs.
BRIEF_TIMES = [
    briefing.BriefTime.at(datetime(2026, 10, 8, 7, 0, tzinfo=SEOUL), scheduled=True),
    briefing.BriefTime.at(datetime(2026, 10, 8, 14, 0, tzinfo=SEOUL)),
    briefing.BriefTime.at(datetime(2026, 10, 8, 18, 30, tzinfo=SEOUL)),
    briefing.BriefTime.at(datetime(2026, 10, 8, 22, 0, tzinfo=SEOUL)),
]


def _pool_lines(pools: dict[str, tuple[str, ...]]) -> list[str]:
    return [line for pool in pools.values() for line in pool]


def system_prompts() -> dict[str, str]:
    """Every persona / system prompt and agent description a model sees."""
    prompts = {
        "mungchi": MUNGCHI_SYSTEM_PROMPT,
        "update subagent": build_update_prompt(),
        "update direct body": build_update_prompt(direct=True),
        "schedule subagent": build_schedule_prompt(),
        "schedule direct body": build_schedule_prompt(direct=True),
        "update direct": build_direct_prompt(UPDATE),
        "schedule direct": build_direct_prompt(SCHEDULE),
        "propose subagent": build_propose_section(),
        "propose direct": build_propose_section(direct=True),
        "image": agents.IMAGE_SECTION,
        "slack format": SLACK_FORMAT_PROMPT,
        "greeting": briefing.GREETING_SYSTEM_PROMPT,
        "subagent voice": agents.SUBAGENT_VOICE,
        "update description": agents.UPDATE_DESCRIPTION,
        "schedule description": agents.SCHEDULE_DESCRIPTION,
    }
    for persona in PERSONAS:
        prompts[f"{persona} voice"] = agents.VOICES[persona]
        for slack in (False, True):
            extra = SLACK_FORMAT_PROMPT if slack else ""
            prompts[f"{persona} options slack={slack}"] = build_options(
                env={}, persona=persona, extra_system_prompt=extra
            ).system_prompt
    for name, agent in build_options(env={}).agents.items():
        prompts[f"agent {name} prompt"] = agent.prompt
        prompts[f"agent {name} description"] = agent.description
    for tool in ALL_TOOLS:
        prompts[f"tool {tool.name}"] = tool.description + json.dumps(tool.input_schema, ensure_ascii=False)
    return prompts


def per_run_prompts() -> dict[str, str]:
    """The relay briefing's per-run prompts (업뎃's and 일정's parts, 고뭉치's greeting)."""
    prompts = {}
    for when in BRIEF_TIMES:
        for persona in (UPDATE, SCHEDULE):
            for slack in (False, True):
                prompts[f"report {persona} {when.period} slack={slack}"] = briefing.report_prompt(persona, when, slack=slack)
        prompts[f"greeting {when.period}"] = briefing.greeting_prompt(when.now, period=when.period)
    return prompts


def user_facing_texts() -> dict[str, str]:
    """Fixed lines code shows the user: phrase pools, CLI greetings and help, bare-mention prompts."""
    texts = {
        "cli help": build_parser().format_help(),
        **{f"chat greeting {persona}": text for persona, text in main_module.CHAT_GREETINGS.items()},
        **{f"empty mention {persona}": text for persona, text in slack_bot.EMPTY_MENTION_PROMPTS.items()},
    }
    for name in (
        "PLACEHOLDER_POOLS",
        "WEATHER_LEADS",
        "CREDIT_LEADS",
        "BOTH_LEADS",
        "GREETING_TEMPLATES",
        "WEEKEND_GREETING_TEMPLATES",
        "MONDAY_GREETING_TEMPLATES",
        "HANDOFF_TEMPLATES",
        "DM_HANDOFF_TEMPLATES",
        "APOLOGY_TEMPLATES",
    ):
        texts[f"phrases.{name}"] = "\n".join(_pool_lines(getattr(phrases, name)))
    return texts


# ---------------------------------------------------------------- no "공저자"


def test_the_wording_rule_says_dropbox_update_and_bans_the_word():
    assert "'Dropbox 업데이트'라고 부른다" in UPDATE_WORDING_RULE
    assert "'공저자'라는 말은 쓰지 않고" in UPDATE_WORDING_RULE
    assert UPDATE_WORDING_RULE.count(WORD) == 1
    # Both 업뎃 prompts carry it once, right under the role line; nobody else's does.
    for prompt in (build_update_prompt(), build_update_prompt(direct=True), build_direct_prompt(UPDATE)):
        assert prompt.count(UPDATE_WORDING_RULE) == 1
        role, rule = prompt.split("\n")[:2]
        assert role.startswith("너는 비서실의 '업뎃'이다(Dropbox 업데이트 담당).") and rule == UPDATE_WORDING_RULE
    for prompt in (MUNGCHI_SYSTEM_PROMPT, build_direct_prompt(SCHEDULE), build_schedule_prompt()):
        assert UPDATE_WORDING_RULE not in prompt


def test_no_prompt_says_gongjeoja_outside_the_wording_rule():
    for name, prompt in {**system_prompts(), **per_run_prompts()}.items():
        assert WORD not in prompt.replace(UPDATE_WORDING_RULE, ""), name


def test_no_user_facing_text_says_gongjeoja():
    for name, text in user_facing_texts().items():
        assert WORD not in text, name
    assert slack_bot.EMPTY_MENTION_PROMPTS[UPDATE] == "Dropbox 업데이트 확인해줘"
    assert main_module.CHAT_GREETINGS[UPDATE].startswith("업뎃입니다. Dropbox 업데이트를 확인해 드릴게요.")
    assert "업뎃(Dropbox 업데이트)" in build_parser().format_help()


def test_no_source_file_or_doc_says_gongjeoja_outside_the_wording_rule():
    """Catches lines code builds on the fly too (e.g. ``--dropbox-check`` output), not only constants."""
    for path in sorted(SRC.rglob("*.py")):
        allowed = UPDATE_WORDING_RULE.count(WORD) if path.name == "agents.py" and path.parent == SRC else 0
        assert path.read_text(encoding="utf-8").count(WORD) == allowed, path.relative_to(SRC)
    docs = [REPO / "README.md", REPO / ".env.example", REPO / "pyproject.toml", *sorted((REPO / "slack_manifests").glob("*.yaml"))]
    for path in docs:
        if path.exists():  # the checkout, not an installed wheel
            assert WORD not in path.read_text(encoding="utf-8"), path.name


# ---------------------------------------------------------------- "Dropbox 업데이트" where it matters


def test_the_no_changes_lines_say_dropbox_update_and_keep_the_window():
    for prompt in (build_update_prompt(), build_update_prompt(direct=True)):
        section = prompt[prompt.index("## Dropbox 업데이트가 없을 때 (total_files가 0)") :].split("\n## ")[0]
        lines = [line for line in section.split("\n") if line.startswith("- ")]
        assert len(lines) == 7
        for line in lines:
            assert '"Dropbox 업데이트 없음' in line, line
        # The since_basis detail is unchanged.
        assert '"Dropbox 업데이트 없음: 최근 24시간 동안 바뀐 파일이 없어요"' in section
        assert '"Dropbox 업데이트 없음: 지난 브리핑(10/06 07:50) 이후 바뀐 파일이 없어요"' in section
        assert '"Dropbox 업데이트 없음: 지난 브리핑 기록이 없어 최근 24시간 기준으로 봤는데, 바뀐 파일이 없어요"' in section
        assert '"Dropbox 업데이트 없음: 10/03 14:20 이후 바뀐 파일이 없어요"' in section
        assert '"Dropbox 업데이트 없음 (최근 24시간 기준): 기간 안에 바뀐 파일 5개는 모두 박사님이 수정하신 거예요"' in section
        assert '8개 가운데 5개는 박사님이 수정하셨고, 3개는 수정한 사람을 알 수 없어 뺐어요' in section
        assert "내가 수정" not in prompt
        assert 'stats가 없으면 "Dropbox 업데이트 없음"만 쓴다.' in section
        assert "변경 없음 (Dropbox" not in prompt
    # 고뭉치 passes that line on as it is.
    assert '업뎃이 "Dropbox 업데이트 없음"을 이유와 함께 한 줄로 보고하면 그 줄을 그대로 옮긴다.' in MUNGCHI_SYSTEM_PROMPT


@pytest.mark.parametrize("when", BRIEF_TIMES, ids=[when.period for when in BRIEF_TIMES])
def test_the_relay_prompt_asks_for_a_dropbox_update(when):
    prompt = briefing.report_prompt(UPDATE, when)
    assert "한 번만 불러 Dropbox 업데이트를 확인해." in prompt
    assert f'짧은 {when.period} 인사 한 줄(예: "업뎃 보고드립니다! Dropbox 업데이트 전해드려요 📂")로 시작하고' in prompt
    assert WORD not in prompt


def test_the_voices_and_roles_describe_the_job_neutrally():
    assert '"업뎃 보고드립니다! Dropbox 업데이트 전해드려요 📂"' in agents.VOICES[UPDATE]
    for prompt in (build_update_prompt(), build_direct_prompt(UPDATE)):
        assert "Dropbox 공유 폴더(기본 /20_연구-진행)에서 사용자가 아닌 다른 분이 수정한 파일을 확인해" in prompt
        assert "1. 다른 분이 수정한 파일만 알린다. 사용자 본인의 작업은 절대 알리지 않는다." in prompt
    assert '업뎃 (subagent_type: "update"): Dropbox 업데이트 담당.' in MUNGCHI_SYSTEM_PROMPT
    assert agents.UPDATE_DESCRIPTION.startswith("Dropbox 업데이트 확인 담당.")
    assert "Dropbox 업데이트는 업뎃" in build_direct_prompt(SCHEDULE)


# ---------------------------------------------------------------- behaviour unchanged: the editor's name stays


def test_each_file_line_still_names_the_editor():
    for prompt in (build_update_prompt(), build_update_prompt(direct=True)):
        assert "  - <사람>: <path> (<modified>), <path> (<modified>) 외 n개" in prompt
        assert "groups 순서대로 하위 폴더마다 link를 한 번만 붙이고(아래 형식의 예처럼), 그 아래에 사람별로" in prompt
    assert "    - 김공저: draft.tex (<modified>)" in MUNGCHI_SYSTEM_PROMPT  # 고뭉치 relays the same lines
    assert "사람별로 바뀐 파일과 수정 시각을 적는다" in MUNGCHI_SYSTEM_PROMPT
    assert "사람별 파일 줄은 그 아래에 들여 쓴다" in SLACK_FORMAT_PROMPT


class _OneFolder:
    """Just enough of a Dropbox client: one listing page, my account and one other editor."""

    ME, OTHER = "dbid:" + "A" * 35, "dbid:" + "B" * 35

    def __init__(self, entries):
        self.entries = entries

    def users_get_current_account(self):
        return SimpleNamespace(account_id=self.ME)

    def files_list_folder(self, path, recursive=False):
        return SimpleNamespace(entries=self.entries, has_more=False, cursor="")

    def users_get_account(self, account_id):
        return SimpleNamespace(name=SimpleNamespace(display_name={self.OTHER: "Sora Youn"}[account_id]))


def _file(path, modified_by):
    when = datetime(2026, 10, 8, 9, 30)  # naive UTC, as Dropbox returns it
    return FileMetadata(
        name=path.rsplit("/", 1)[-1],
        id="id:xxxxxxxxxx",
        client_modified=when,
        server_modified=when,
        rev="0123456789",
        size=1,
        path_lower=path.lower(),
        path_display=path,
        sharing_info=FileSharingInfo(read_only=False, parent_shared_folder_id="1", modified_by=modified_by),
    )


def test_the_tool_still_lists_only_other_peoples_files_under_their_names():
    root = "/20_연구-진행"
    dbx = _OneFolder(
        [
            _file(f"{root}/oTree codes/slides.pptx", _OneFolder.OTHER),
            _file(f"{root}/oTree codes/mine.tex", _OneFolder.ME),
        ]
    )
    payload = collect_updates(dbx, root, datetime(2026, 10, 8, tzinfo=timezone.utc), SEOUL, name_cache={})
    assert payload["total_files"] == 1 and payload["stats"]["excluded_mine"] == 1
    [group] = payload["groups"]
    assert group["subfolder"] == "oTree codes"
    assert group["by"] == [{"name": "Sora Youn", "files": [{"path": "slides.pptx", "modified": "2026-10-08 18:30"}]}]


# ---------------------------------------------------------------- still byte-stable


def test_prompts_stay_byte_stable_and_time_free():
    first, again = system_prompts(), system_prompts()
    for name, prompt in first.items():
        assert prompt.encode("utf-8") == again[name].encode("utf-8"), name
    for prompt in (build_update_prompt(), build_direct_prompt(UPDATE), MUNGCHI_SYSTEM_PROMPT):
        assert not re.search(r"\{[a-z_]+\}", prompt)
        assert not re.search(r"20\d\d-\d\d-\d\d", prompt)
