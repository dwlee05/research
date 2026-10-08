"""The bots' human voices: persona prompts, varied placeholders and shortcut lead-ins.

Everything that varies is picked by code from small pools (seedable), and the
system prompts that carry the voices stay constant (cached).
"""

from __future__ import annotations

import asyncio
import random
import re
import unicodedata

import pytest

from mungchi import agents, phrases, slack_bot
from mungchi.agents import ACCURACY_RULE, MUNGCHI_SYSTEM_PROMPT, SUBAGENT_VOICE, VOICES
from mungchi.main import build_options
from mungchi.personas import PERSONAS, call_name
from mungchi.slack_format import SLACK_FORMAT_PROMPT
from mungchi.state import ThreadSessions

OWNER = "UOWNER1"
CHANNEL = "C0123ABCD"


def emoji_count(text: str) -> int:
    return sum(1 for char in text if unicodedata.category(char) == "So")


# ---------------------------------------------------------------- persona prompts


def test_each_persona_prompt_has_its_voice_and_the_accuracy_rule():
    prompts = {
        "mungchi": build_options(env={}).system_prompt,
        "update": build_options(env={}, persona="update").system_prompt,
        "schedule": build_options(env={}, persona="schedule").system_prompt,
    }
    assert prompts["mungchi"] == MUNGCHI_SYSTEM_PROMPT
    for persona, prompt in prompts.items():
        assert "## 말투\n" + VOICES[persona] in prompt, persona
        assert ACCURACY_RULE in prompt, persona
        assert "매번 같은 말로 시작하지 않는다" in prompt and "하지 않은 일을 했다고 말하지 않는다" in prompt
        assert "성격은 짧은 한 줄(여는 말, 넘어가는 말, 맺는 말)에만 담는다" in prompt
        # The other personas' voices are not in it.
        for other in PERSONAS:
            if other != persona:
                assert VOICES[other] not in prompt, (persona, other)
    # Distinct voices, as the user asked.
    assert "똑똑!" in VOICES["mungchi"] and "이모지는 한 메시지에 0~2개" in VOICES["mungchi"]
    assert "'업뎃이', '일정이'" in VOICES["mungchi"]
    assert "업뎃 보고드립니다!" in VOICES["update"] and "📂 하나" in VOICES["update"]
    assert "오늘은 여유로운 편이에요 😊" in VOICES["schedule"] and "0~1개" in VOICES["schedule"]
    assert "실제 일정에 맞을 때만" in VOICES["schedule"]  # no cheerful "여유로운 편" on a packed day
    for numbers in ("숫자", "날짜", "시각", "파일 이름", "사람 이름"):
        assert numbers in ACCURACY_RULE


def test_subagents_report_plainly_with_the_same_accuracy_rule():
    opts = build_options(env={})
    for name, agent in opts.agents.items():
        assert SUBAGENT_VOICE in agent.prompt, name
        assert ACCURACY_RULE in agent.prompt
        assert not any(text in agent.prompt for text in VOICES.values()), name  # 고뭉치 speaks to the user
    assert "담백하게 보고한다" in SUBAGENT_VOICE


def test_voice_prompts_keep_the_functional_rules():
    # The voice is added; the tools, gating, proposal flow and since_basis wording are all still there.
    update = build_options(env={}, persona="update").system_prompt
    schedule = build_options(env={}, persona="schedule").system_prompt
    for prompt in (update, schedule, MUNGCHI_SYSTEM_PROMPT):
        assert "confirm_question" in prompt or "확인 질문" in prompt
    assert "briefing_checkpoint → \"지난 브리핑(10/06 07:50) 이후\"" in update
    assert "propose_calendar_events" in update and "propose_calendar_events" in schedule
    assert "네가 쓰는 도구는 Agent, get_credits, get_weather 세 가지뿐이다" in MUNGCHI_SYSTEM_PROMPT


def test_voice_prompts_are_constant_and_have_no_placeholders_left():
    for persona in PERSONAS:
        first = build_options(env={}, persona=persona, extra_system_prompt=SLACK_FORMAT_PROMPT).system_prompt
        again = build_options(env={}, persona=persona, extra_system_prompt=SLACK_FORMAT_PROMPT).system_prompt
        assert first.encode("utf-8") == again.encode("utf-8")
        assert not re.search(r"\{[a-z_]+\}", first), persona
    assert agents.voice_section("update") == agents.voice_section("update")


def test_call_names_take_i_after_a_final_consonant():
    assert [call_name(p) for p in PERSONAS] == ["고뭉치", "업뎃이", "일정이"]


# ---------------------------------------------------------------- placeholders


@pytest.mark.parametrize("persona", PERSONAS)
def test_placeholder_pools_are_varied_and_every_variant_is_valid(persona):
    pool = phrases.PLACEHOLDER_POOLS[persona]
    assert 3 <= len(pool) <= 5 and len(set(pool)) == len(pool)
    limit = {"mungchi": 2, "update": 1, "schedule": 1}[persona]
    for line in pool:
        assert line.strip() == line and line and "\n" not in line and len(line) <= 40
        assert re.search(r"[가-힣]", line)
        assert "*" not in line and "{" not in line and "<" not in line  # nothing Slack or format() would touch
        assert emoji_count(line) <= limit, line
        if persona == "update":
            assert all(unicodedata.category(c) != "So" or c == "📂" for c in line)
    # The old fixed texts are gone.
    assert not set(pool) & {"🗂️ 고뭉치가 확인 중이에요...", "📝 업뎃이 확인 중이에요...", "⏰ 일정이 확인 중이에요..."}


def test_the_pick_is_deterministic_with_a_seeded_rng_and_varies_without_one():
    for persona in PERSONAS:
        picks = [phrases.pick_placeholder(persona, random.Random(seed)) for seed in range(40)]
        assert picks == [phrases.pick_placeholder(persona, random.Random(seed)) for seed in range(40)]
        assert set(picks) == set(phrases.PLACEHOLDER_POOLS[persona])  # every variant shows up
    rng = random.Random(3)
    assert phrases.pick(["가", "나"], rng, avoid="가") == "나"
    assert phrases.pick(["가"], rng, avoid="가") == "가"  # nothing else to pick


class FakeClient:
    def __init__(self):
        self.posts: list[dict] = []
        self.updates: list[dict] = []

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True, "channel": kwargs["channel"], "ts": f"1.{len(self.posts):06d}"}

    async def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return {"ok": True}


def _handler(tmp_path, persona="mungchi", seed=11, **kwargs):
    from mungchi.main import TurnResult

    async def run(prompt, **_kwargs):
        return TurnResult(text="답이에요.", session_id="11111111-1111-1111-1111-111111111111")

    client = FakeClient()
    handler = slack_bot.SlackHandler(
        client,
        allowed_user_ids={OWNER},
        sessions=ThreadSessions(tmp_path / f"t-{persona}-{seed}.json"),
        persona=persona,
        run=run,
        bot_user_id="UBOT",
        status_interval=0,
        rng=random.Random(seed),
        **kwargs,
    )
    return handler, client


def _dm(text, ts="1700000000.000200"):
    return {"type": "message", "channel_type": "im", "user": OWNER, "text": text, "ts": ts, "channel": "D0123ABCD"}


@pytest.mark.parametrize("persona", PERSONAS)
def test_the_handler_picks_its_placeholder_from_its_own_pool_with_the_injected_rng(tmp_path, persona):
    handler, client = _handler(tmp_path, persona)
    asyncio.run(handler.handle_event(_dm("안녕"), event_id="E1", source="dm"))
    expected = phrases.pick_placeholder(persona, random.Random(11))
    assert client.posts[0]["text"] == expected
    assert client.updates[-1]["text"] == "답이에요."


# ---------------------------------------------------------------- shortcut lead-ins


WEATHER = "🌤️ *서울 날씨*: 대체로 맑음 · 최저 12° / 최고 23° · 강수확률 10% · 미세먼지 보통"
CREDITS = "💳 *Chat KHU 크레딧*: 9,050.5 남음 / 10,000 (90.5%) · 11/01 갱신\n이번 달 사용: 949.5 (10/01–10/07, 94회)"


@pytest.mark.parametrize("persona", PERSONAS)
@pytest.mark.parametrize(
    "text,leads,data",
    [
        ("날씨", phrases.WEATHER_LEADS, WEATHER),
        ("토큰", phrases.CREDIT_LEADS, CREDITS),
        ("날씨랑 토큰 좀 말해봐", phrases.BOTH_LEADS, f"{WEATHER}\n\n{CREDITS}"),
    ],
)
def test_shortcut_lead_ins_never_change_the_data_lines(tmp_path, persona, text, leads, data):
    handler, client = _handler(tmp_path, persona, weather_text=lambda: WEATHER, credit_text=lambda: CREDITS)
    asyncio.run(handler.handle_event(_dm(text), event_id="E1", source="dm"))
    [post] = client.posts
    lead, body = post["text"].split("\n", 1)
    assert body == data  # exactly what code fetched
    assert lead == phrases.pick(leads[persona], random.Random(11))  # the seeded pick, in this bot's voice


def test_lead_in_pools_are_short_and_never_state_data():
    for pools in (phrases.WEATHER_LEADS, phrases.CREDIT_LEADS, phrases.BOTH_LEADS):
        assert set(pools) == set(PERSONAS)
        for persona, pool in pools.items():
            assert len(pool) >= 2
            for line in pool:
                assert len(line) <= 30 and "\n" not in line and not re.search(r"\d", line), line
                assert emoji_count(line) <= (2 if persona == "mungchi" else 1)
                if persona == "update":
                    assert emoji_count(line) == 0


@pytest.mark.parametrize(
    "weather_text,credit_text,expected",
    [
        (lambda: "🌤️ *서울 날씨*: 가져오지 못했어요", None, "🌤️ *서울 날씨*: 가져오지 못했어요"),
        (None, lambda: "⚠️ 크레딧을 확인하지 못했습니다 (HTTP 500).", "⚠️ 크레딧을 확인하지 못했습니다 (HTTP 500)."),
    ],
)
def test_failure_lines_get_no_lead_in(tmp_path, weather_text, credit_text, expected):
    extra = {"weather_text": weather_text} if weather_text else {"credit_text": credit_text}
    handler, client = _handler(tmp_path, **extra)
    asyncio.run(handler.handle_event(_dm("날씨" if weather_text else "크레딧"), event_id="E1", source="dm"))
    assert [p["text"] for p in client.posts] == [expected]
