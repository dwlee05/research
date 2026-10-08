"""Short, varied lines that give the bots a human voice in Slack, chosen by code (no LLM).

Each persona has its own small pool per situation, so the bots do not open
every message the same way:

* ``PLACEHOLDER_POOLS``: the first reply while an agent works ("잠시만요, 금방 확인해 볼게요 🗂️").
* ``WEATHER_LEADS`` / ``CREDIT_LEADS`` / ``BOTH_LEADS``: one line in front of
  the weather and credit shortcut replies. They only say that the bot
  checked; the data line under them is code's and never changes.
* The relay morning briefing (``briefing``): 고뭉치's fallback greetings
  (when the small LLM greeting fails), its hand-off lines to 업뎃이 and
  일정이, and 업뎃's / 일정's short apology when their part could not be made.

Voices: 고뭉치 is a warm, slightly playful chief of staff (0–2 emoji),
업뎃 a tidy, earnest research assistant (📂 at most), 일정 a bright,
upbeat schedule manager (0–1 emoji). Teammates are called 업뎃이 and
일정이 (``personas.call_name``).

Every pick takes an optional ``random.Random`` so tests can seed it. This
module imports only ``personas``, so every other module can use it. Pure.
"""

from __future__ import annotations

import random
from typing import Sequence

from .personas import MUNGCHI, SCHEDULE, UPDATE, call_name, josa

# ---------------------------------------------------------------- placeholders

PLACEHOLDER_POOLS: dict[str, tuple[str, ...]] = {
    MUNGCHI: (
        "잠시만요, 금방 확인해 볼게요 🗂️",
        "네, 바로 알아볼게요!",
        "알겠어요, 하나씩 챙겨 볼게요 🙂",
        "잠깐만요, 고뭉치가 살펴보는 중이에요...",
        "좋아요, 금방 다녀올게요!",
    ),
    UPDATE: (
        "네, 바로 확인해 보겠습니다.",
        "확인 들어갑니다! 금방 보고드릴게요.",
        "잠시만요, 꼼꼼히 살펴보고 말씀드릴게요 📂",
        "업뎃이 확인 중입니다...",
    ),
    SCHEDULE: (
        "잠깐만요, 금방 확인해 드릴게요! ⏰",
        "네! 바로 살펴볼게요 😊",
        "일정이 확인 중이에요~ 조금만 기다려 주세요",
        "좋아요, 금방 알려드릴게요!",
    ),
}

# ---------------------------------------------------------------- shortcut lead-ins (weather, credits)
#
# Neutral on purpose: they also sit above a "가져오지 못했어요" line.

WEATHER_LEADS: dict[str, tuple[str, ...]] = {
    MUNGCHI: ("날씨 확인해 봤어요!", "네, 바로 하늘 좀 살펴봤어요 🙂", "오늘 날씨 챙겨 봤어요."),
    UPDATE: ("날씨 확인했습니다.", "오늘 날씨 확인해 봤습니다."),
    SCHEDULE: ("오늘 날씨 확인해 봤어요 😊", "네! 날씨 살펴봤어요.", "바로 확인해 봤어요!"),
}
CREDIT_LEADS: dict[str, tuple[str, ...]] = {
    MUNGCHI: ("크레딧 확인해 봤어요.", "네, Chat KHU 크레딧 살펴봤어요 🙂", "바로 확인해 봤어요!"),
    UPDATE: ("크레딧 확인했습니다.", "크레딧 현황 확인해 봤습니다."),
    SCHEDULE: ("크레딧 확인해 봤어요!", "네! 바로 확인해 봤어요."),
}
BOTH_LEADS: dict[str, tuple[str, ...]] = {
    MUNGCHI: ("날씨랑 크레딧, 같이 확인해 봤어요 🙂", "두 가지 다 챙겨 왔어요!"),
    UPDATE: ("날씨와 크레딧 확인했습니다.", "두 가지 모두 확인해 봤습니다."),
    SCHEDULE: ("날씨랑 크레딧 한 번에 확인해 봤어요!", "네! 둘 다 살펴봤어요 😊"),
}


# ---------------------------------------------------------------- the relay morning briefing
#
# Greeting templates are formatted with: {date} "2026년 10월 8일(목)",
# {short} "10월 8일(목)", {md} "10월 8일", {wd} "목요일". Every one keeps
# the date, so a fallback greeting is always correct.

GREETING_TEMPLATES: tuple[str, ...] = (
    "똑똑! 🚪 {date} 아침 브리핑입니다~",
    "좋은 아침이에요! {md} {wd} 브리핑 시작할게요 🙂",
    "안녕하세요, 고뭉치예요. {short} 아침 소식 챙겨 왔어요!",
    "{short} 아침이 밝았어요. 오늘도 차근차근 같이 챙겨 봐요 ✨",
    "똑똑, 고뭉치예요! {date} 아침 브리핑 들어갑니다.",
)
WEEKEND_GREETING_TEMPLATES: tuple[str, ...] = (
    "주말 아침이에요! {short} 브리핑 살짝 놓고 갈게요 🙂",
    "똑똑! 🚪 {short} 주말 아침 브리핑이에요. 편하게 보세요~",
)
MONDAY_GREETING_TEMPLATES: tuple[str, ...] = ("한 주의 시작이에요! {short} 아침 브리핑입니다 💪",)

# 고뭉치 hands over to the other two. {bots}: Slack mentions ("<@U1> <@U2>")
# or, in the terminal, their names ("업뎃이, 일정이").
HANDOFF_TEMPLATES: tuple[str, ...] = (
    "{bots} 아침 보고 부탁해요!",
    "그럼 {bots} 차례예요. 오늘 소식 들려주세요 🙌",
    "{bots} 이어서 부탁할게요~",
    "이제 {bots} 보고 들어볼까요?",
)
# DM mode: each bot reports in its own DM. {names}: "업뎃이와 일정이는", "업뎃이는", ...
DM_HANDOFF_TEMPLATES: tuple[str, ...] = (
    "{names} 각자 DM으로 아침 보고를 드릴 거예요 📬",
    "{names} 자기 DM에서 따로 보고드릴 거예요!",
    "이어서 {names} 각자 DM으로 찾아갈 거예요 🙂",
)

# 업뎃 / 일정 when their part could not be made. {reason}: short, already scrubbed.
APOLOGY_TEMPLATES: dict[str, tuple[str, ...]] = {
    UPDATE: (
        "업뎃입니다. 죄송합니다, 오늘은 Dropbox를 확인하지 못했습니다. (사유: {reason})",
        "업뎃입니다. 오늘 Dropbox 보고는 드리지 못하게 되었습니다. 죄송합니다. (사유: {reason})",
    ),
    SCHEDULE: (
        "일정이에요. 오늘 일정을 확인하지 못했어요 😥 (사유: {reason})",
        "일정이에요. 미안해요, 오늘 일정을 불러오지 못했어요 😥 (사유: {reason})",
    ),
}


def teammate_names(personas: Sequence[str], *, topic: bool = False) -> str:
    """``업뎃이, 일정이``; with ``topic``: ``업뎃이와 일정이는`` / ``업뎃이는`` (the subject of a DM note)."""
    names = [call_name(persona) for persona in personas]
    if not topic:
        return ", ".join(names)
    return " ".join([josa(name, "과", "와") for name in names[:-1]] + [josa(names[-1], "은", "는")])


# ---------------------------------------------------------------- picking


def pick(pool: Sequence[str], rng: random.Random | None = None, *, avoid: str | None = None) -> str:
    """One line from ``pool``; not ``avoid`` when the pool has another one. ``rng`` makes it repeatable."""
    choices = [line for line in pool if line != avoid] or list(pool)
    return (rng or random).choice(choices)


def pick_placeholder(persona: str, rng: random.Random | None = None) -> str:
    """The first reply of ``persona``'s bot while its agent works."""
    return pick(PLACEHOLDER_POOLS[persona], rng)
