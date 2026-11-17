"""Persona keys and names shared by the agents, the CLI, the Slack bots and stored state.

This module imports nothing from the package, so every other module can use it.
"""

from __future__ import annotations

MUNGCHI = "mungchi"
UPDATE = "update"
SCHEDULE = "schedule"

PERSONAS = (MUNGCHI, UPDATE, SCHEDULE)
# Personas that can also talk to the user directly (their own Slack bot, ``--agent``).
DIRECT_PERSONAS = (UPDATE, SCHEDULE)

# Korean names shown to the user. '일정' is also the ordinary word for
# "schedule", so prompts call it "'일정' 에이전트" where that could be confusing.
PERSONA_LABELS = {MUNGCHI: "고뭉치", UPDATE: "업뎃", SCHEDULE: "일정"}

# Slack: ASCII bot handle (manifest ``bot_user.display_name``) and app name.
SLACK_HANDLES = {MUNGCHI: "moongchi", UPDATE: "update", SCHEDULE: "schedule"}
SLACK_APP_NAMES = {MUNGCHI: "비서실 고뭉치", UPDATE: "업뎃", SCHEDULE: "일정"}


def has_final_consonant(word: str) -> bool:
    """True if the last Hangul syllable of ``word`` ends in a consonant (받침)."""
    last = (word or " ")[-1]
    if "가" <= last <= "힣":
        return (ord(last) - ord("가")) % 28 != 0
    return False


def josa(word: str, after_consonant: str, after_vowel: str) -> str:
    """``word`` plus the particle form that fits it, e.g. ``josa("업뎃", "이", "가") == "업뎃이"``."""
    return word + (after_consonant if has_final_consonant(word) else after_vowel)


def call_name(persona: str) -> str:
    """The friendly form a teammate's name takes in casual speech: ``업뎃이``, ``일정이``, ``고뭉치``.

    A Korean name ending in a consonant takes 이 when it is called or talked
    about warmly ("업뎃이가 채널에 없어서", "일정이에게 물어보는 중"); one
    ending in a vowel stays as it is (고뭉치가). Particles go after this form.
    """
    label = PERSONA_LABELS[persona]
    return label + ("이" if has_final_consonant(label) else "")
