"""Short, varied lines that give the bots a human voice in Slack, chosen by code (no LLM).

Each persona has its own small pool per situation, so the bots do not open
every message the same way:

* ``PLACEHOLDER_POOLS``: the first reply while an agent works ("잠시만요, 금방 확인해 볼게요 🗂️").
* ``WEATHER_LEADS`` / ``CREDIT_LEADS`` / ``BOTH_LEADS``: one line in front of
  the weather and credit shortcut replies. They only say that the bot
  checked; the data line under them is code's and never changes.

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

from .personas import MUNGCHI, SCHEDULE, UPDATE

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


# ---------------------------------------------------------------- picking


def pick(pool: Sequence[str], rng: random.Random | None = None, *, avoid: str | None = None) -> str:
    """One line from ``pool``; not ``avoid`` when the pool has another one. ``rng`` makes it repeatable."""
    choices = [line for line in pool if line != avoid] or list(pool)
    return (rng or random).choice(choices)


def pick_placeholder(persona: str, rng: random.Random | None = None) -> str:
    """The first reply of ``persona``'s bot while its agent works."""
    return pick(PLACEHOLDER_POOLS[persona], rng)
