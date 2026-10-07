"""Short Slack questions answered by code, without an agent turn (no LLM call).

``parse_quick_info`` reads a message (the bot's mention already removed) that
only asks for today's weather, the Chat KHU credits, or both at once:

    뭉치야 날씨랑 토큰 좀 말해봐   -> {"weather", "credits"}
    토큰 좀 알려줘                -> {"credits"}   ("토큰" is what the user calls the credits)
    날씨 알려줘                   -> {"weather"}

Everything in such a message is either a keyword (날씨; 크레딧, 토큰, 잔액,
사용량) or filler from a small closed list (vocatives, connectors, time and
place words, a few particles and request tails), with or without spaces. Any
other word ("내일", "아끼려면", "Dropbox", ...) means the message needs the
agent, so the result is empty.

``is_briefing_request`` reads a short request for today's briefing the same
way (keyword 브리핑, its own closed filler list):

    오늘 건너뛴 브리핑 좀 해봐    -> True
    브리핑 형식 바꿔줘            -> False ("형식", "바꿔" are not filler)

``query_text`` is the normalization shared with ``weather.is_weather_query``
and ``credits.is_credit_query``. Everything here is pure: no LLM call, no I/O.
"""

from __future__ import annotations

import functools
import re
import unicodedata

WEATHER = "weather"
CREDITS = "credits"

# Slack sends emoji as ":name:" (":pray:", ":+1::skin-tone-2:").
_TRAILING_SHORTCODES_RE = re.compile(r"(?:\s*:[a-z0-9_+'.-]+:)+\s*$")


def _is_trailing_noise(char: str) -> bool:
    """Space, punctuation, symbols (emoji included) and the marks / joiners emoji are built with."""
    category = unicodedata.category(char)
    return char.isspace() or category[0] in ("P", "S") or category in ("Mn", "Me", "Cf")


def query_text(text: str | None) -> str:
    """NFC, lower case, single spaces, trailing punctuation / emoji / ":shortcode:" removed."""
    text = " ".join(unicodedata.normalize("NFC", text or "").lower().split())
    while True:
        before = text
        text = _TRAILING_SHORTCODES_RE.sub("", text)
        while text and _is_trailing_noise(text[-1]):
            text = text[:-1]
        if text == before:
            return text


# ---------------------------------------------------------------- the combined question

KEYWORDS: dict[str, str] = {
    "날씨": WEATHER,
    "크레딧": CREDITS,
    "토큰": CREDITS,  # the user's word for the Chat KHU credits
    "잔액": CREDITS,
    "사용량": CREDITS,
}

# Filler, deliberately a small closed list. The configured WEATHER_LABEL counts as a place too.
VOCATIVES = ("뭉치야", "고뭉치야", "뭉치", "고뭉치", "비서실")
CONNECTORS = ("랑", "이랑", "하고", "와", "과", "그리고", "및", ",", "/")
TIME_PLACE_WORDS = ("오늘", "오늘의", "지금", "현재", "남은", "잔여", "서울", "여기")
PARTICLES = ("좀", "는", "은", "가", "도")
REQUEST_TAILS = (
    "말해봐",
    "말해 줘",
    "말해줘",
    "알려줘",
    "알려 줘",
    "알려줄래",
    "알려주세요",
    "보여줘",
    "확인",
    "확인해줘",
    "어때",
    "어때요",
    "얼마",
    "얼마나",
    "남았어",
    "궁금해",
)
FILLER = (*VOCATIVES, *CONNECTORS, *TIME_PLACE_WORDS, *PARTICLES, *REQUEST_TAILS)


def _word(text: str) -> str:
    """A vocabulary word as it is matched: NFC, lower case, no whitespace (punctuation kept: "," and "/")."""
    return "".join(unicodedata.normalize("NFC", text).lower().split())


@functools.lru_cache(maxsize=16)
def _vocabulary(label: str) -> tuple[tuple[str, str | None], ...]:
    """``(word without spaces, keyword kind or None for filler)``, longest first."""
    words: dict[str, str | None] = {_word(word): None for word in FILLER}
    place = _word(label)
    if place:
        words.setdefault(place, None)
    words.update(KEYWORDS)
    return tuple(sorted(((w, k) for w, k in words.items() if w), key=lambda item: (-len(item[0]), item[0])))


def parse_quick_info(text: str | None, label: str | None = None) -> set[str]:
    """What a short message asks for: a subset of ``{"weather", "credits"}``, empty when it needs the agent.

    The message (mention already removed) is normalized like
    ``weather.is_weather_query`` does and must consist only of keywords and
    filler, in any order and with or without spaces. ``label`` is the
    configured ``WEATHER_LABEL`` (서울 and 여기 always count). Pure.
    """
    compact = query_text(text).replace(" ", "")
    if not compact:
        return set()
    return set(_keywords(compact, _vocabulary(label or "")) or ())


def _keywords(compact: str, vocabulary: tuple[tuple[str, str | None], ...]) -> frozenset[str] | None:
    """The keyword kinds in ``compact`` when it splits completely into ``vocabulary`` words, else None."""
    # found[i]: the keywords of the ways compact[:i] splits into known words, None when it cannot.
    found: list[frozenset[str] | None] = [None] * (len(compact) + 1)
    found[0] = frozenset()
    for start in range(len(compact)):
        before = found[start]
        if before is None:
            continue
        for word, kind in vocabulary:
            if compact.startswith(word, start):
                end = start + len(word)
                kinds = before | {kind} if kind else before
                found[end] = kinds if found[end] is None else found[end] | kinds
    return found[-1]


# ---------------------------------------------------------------- a short briefing request

BRIEFING = "briefing"
BRIEFING_KEYWORD = "브리핑"
# Filler around 브리핑, deliberately a small closed list of its own.
BRIEFING_TIME_WORDS = ("오늘", "오늘의", "아침", "지금", "다시", "한번", "한 번")
BRIEFING_MISSED_WORDS = ("건너뛴", "못 받은", "놓친", "빠진")
BRIEFING_PARTICLES = ("좀", "을", "를", "도")
BRIEFING_REQUEST_TAILS = (
    "해줘",
    "해 줘",
    "해봐",
    "해 봐",
    "해주세요",
    "해줄래",
    "보여줘",
    "줘",
    "부탁해",
    "부탁",
    "받을래",
    "받아볼래",
)
BRIEFING_FILLER = (
    *VOCATIVES,
    *BRIEFING_TIME_WORDS,
    *BRIEFING_MISSED_WORDS,
    *BRIEFING_PARTICLES,
    *BRIEFING_REQUEST_TAILS,
)


@functools.lru_cache(maxsize=1)
def _briefing_vocabulary() -> tuple[tuple[str, str | None], ...]:
    words: dict[str, str | None] = {_word(word): None for word in BRIEFING_FILLER}
    words[_word(BRIEFING_KEYWORD)] = BRIEFING
    return tuple(sorted(words.items(), key=lambda item: (-len(item[0]), item[0])))


def is_briefing_request(text: str | None) -> bool:
    """True when a short message (mention already removed) only asks for today's briefing.

    "오늘 건너뛴 브리핑 좀 해봐", "브리핑", "뭉치야 아침 브리핑 보여줘": the
    message holds 브리핑 and otherwise only filler from the closed lists
    above, in any order and with or without spaces, normalized like
    ``parse_quick_info``. Any other word ("형식", "날씨", "어제", a particle
    such as "에") means it needs the agent: False. Filler alone is False. Pure.
    """
    compact = query_text(text).replace(" ", "")
    if not compact:
        return False
    return BRIEFING in (_keywords(compact, _briefing_vocabulary()) or ())
