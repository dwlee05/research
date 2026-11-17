"""The combined quick-info question ("뭉치야 날씨랑 토큰 좀 말해봐"): pure parsing, no LLM, no I/O."""

from __future__ import annotations

import unicodedata

import pytest

from mungchi import credits, weather
from mungchi.quick_info import CREDIT_DETAIL, CREDITS, WEATHER, is_briefing_request, parse_quick_info, query_text
from mungchi.slack_bot import quick_info_request

BOTH = {WEATHER, CREDITS}


@pytest.mark.parametrize(
    "text,expected",
    [
        # The agreed examples.
        ("뭉치야 날씨랑 토큰 좀 말해봐", BOTH),
        ("날씨하고 크레딧", BOTH),
        ("오늘 날씨랑 남은 토큰 알려줘", BOTH),
        ("토큰 좀 알려줘", {CREDITS}),
        ("날씨 알려줘", {WEATHER}),
        # Same shape: spaces optional, any order, every filler kind, trailing punctuation and emoji.
        ("뭉치야날씨랑토큰좀말해봐", BOTH),
        ("고뭉치야, 토큰이랑 날씨 알려 줘!", BOTH),
        ("비서실 고뭉치 날씨도 토큰도", BOTH),
        ("토큰과 날씨 말해 줘", BOTH),
        ("날씨와 잔액", BOTH),
        ("날씨 그리고 사용량 확인해줘", BOTH),
        ("날씨 및 크레딧 보여줘", BOTH),
        ("날씨, 토큰", BOTH),
        ("날씨/토큰?", BOTH),
        ("지금 서울 날씨는 어때요? 남은 토큰은 얼마나 남았어?", set()),  # "?" in the middle is not filler
        ("지금 서울 날씨는 어때요 남은 토큰은 얼마나 남았어", BOTH),
        ("현재 날씨가 궁금해 :pray:", {WEATHER}),
        ("오늘의 날씨 🙏", {WEATHER}),
        ("여기 날씨 알려줄래", {WEATHER}),
        ("잔여 토큰 확인", {CREDITS}),
        ("토큰 얼마", {CREDITS}),
        ("크레딧 알려주세요", {CREDITS}),
        ("뭉치야 토큰", {CREDITS}),
        ("날씨랑 날씨", {WEATHER}),
    ],
)
def test_quick_info_questions(text, expected):
    assert parse_quick_info(text, "서울") == expected
    assert parse_quick_info(text) == expected  # 서울 is always a place
    assert quick_info_request(text, "서울") == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        # The agreed examples: a detail word with a credit keyword asks for the detailed credits.
        ("크레딧 자세히", {CREDITS, CREDIT_DETAIL}),
        ("토큰 내역", {CREDITS, CREDIT_DETAIL}),
        # Same shape, other detail words and fillers.
        ("크레딧 상세", {CREDITS, CREDIT_DETAIL}),
        ("크레딧 상세 내역 보여줘", {CREDITS, CREDIT_DETAIL}),
        ("모델별 토큰 사용량", {CREDITS, CREDIT_DETAIL}),
        ("토큰 사용 내역 알려줘", {CREDITS, CREDIT_DETAIL}),
        ("뭉치야 크레딧 자세하게 알려줘!", {CREDITS, CREDIT_DETAIL}),
        ("토큰 좀 자세히", {CREDITS, CREDIT_DETAIL}),
        ("잔액 상세히", {CREDITS, CREDIT_DETAIL}),
        # Combined: short unless a detail word is there too.
        ("날씨랑 크레딧", BOTH),
        ("날씨랑 크레딧 자세히", {WEATHER, CREDITS, CREDIT_DETAIL}),
        # A detail word without a credit keyword needs the agent.
        ("날씨 자세히", set()),
        ("자세히", set()),
        ("내역", set()),
        ("모델별로 알려줘", set()),
    ],
)
def test_detailed_credit_requests(text, expected):
    assert parse_quick_info(text, "서울") == expected
    assert quick_info_request(text, "서울") == expected


@pytest.mark.parametrize(
    "text",
    [
        # The agreed examples: these need the agent.
        "내일 비 오면 일정 바꿔야 할까?",
        "날씨 좋은 날 야외 미팅 잡아줘",
        "토큰 아끼려면 어떻게 해?",
        "이번 주 Dropbox 변경이랑 날씨",  # Dropbox is not a quick-info keyword
        # More of the same: any word outside the closed lists.
        "토큰이 뭐야?",
        "크레딧 아끼려면?",
        "내일 날씨랑 토큰",
        "날씨 어때 그리고 일정도",
        "오늘 일정이랑 날씨",
        "부산 날씨랑 토큰",  # not the configured place
        "날씨 예보",
        "<@U123> 날씨랑 토큰",
        "토큰 가격",
        "비트코인 토큰 시세 알려줘",
        # Filler alone, or nothing at all.
        "",
        "   ",
        "뭉치야",
        "좀 알려줘",
        "가",
        ",",
        "🙏",
        ":pray:",
    ],
)
def test_other_messages_are_not_quick_info(text):
    assert parse_quick_info(text, "서울") == set()
    assert parse_quick_info(None) == set()


def test_the_configured_label_is_a_place_and_matched_like_the_text():
    assert parse_quick_info("부산 날씨랑 토큰", "부산") == BOTH
    assert parse_quick_info("우리동네 날씨", "우리 동네") == {WEATHER}
    assert parse_quick_info("a.b 날씨", "a.b") == {WEATHER}
    assert parse_quick_info("axb 날씨", "a.b") == set()
    assert parse_quick_info("서울 날씨", "부산") == {WEATHER}  # 서울 and 여기 always count


def test_normalization_matches_the_weather_question():
    assert parse_quick_info(unicodedata.normalize("NFD", "뭉치야 날씨랑 토큰 좀 말해봐")) == BOTH
    assert parse_quick_info("날씨\t랑\n토큰") == BOTH
    assert query_text("  날씨  알려줘 :pray::skin-tone-2: ") == "날씨 알려줘"
    assert query_text("Credits？！") == "credits"
    assert query_text(None) == ""


@pytest.mark.parametrize("text", ["날씨가 어떠니", "날씨 어떤가요?", "날씨 어떄", "오늘 서울의 날씨"])
def test_weather_phrasings_outside_the_lists_still_reach_the_weather_shortcut(text):
    assert parse_quick_info(text) == set()
    assert weather.is_weather_query(text)
    assert quick_info_request(text, "서울") == {WEATHER}


@pytest.mark.parametrize("text", ["credits", "Credit?", "CREDITS 알려줘", "크레딧얼마남았나", "사용량 보여줘."])
def test_credit_phrasings_outside_the_lists_still_reach_the_credit_shortcut(text):
    assert credits.is_credit_query(text)
    assert quick_info_request(text, "서울") == {CREDITS}


@pytest.mark.parametrize("text", ["토큰 아끼려면 어떻게 해?", "토큰이 뭐야?", "내일 비 오면 일정 바꿔야 할까?", "오늘 일정 알려줘"])
def test_quick_info_request_is_empty_for_agent_questions(text):
    assert quick_info_request(text, "서울") == set()


# ---------------------------------------------------------------- a short briefing request ("오늘 건너뛴 브리핑 좀 해봐")


@pytest.mark.parametrize(
    "text",
    [
        # The agreed examples.
        "오늘 건너뛴 브리핑 좀 해봐",  # what the user actually sent
        "브리핑",
        "브리핑 해줘",
        "오늘 브리핑 다시 해줘",
        "뭉치야 아침 브리핑 보여줘",
        "못 받은 브리핑 줘",
        # Same shape: spaces optional, any order, every filler kind, trailing punctuation and emoji.
        "오늘건너뛴브리핑좀해봐",
        "고뭉치야 브리핑",
        "비서실 고뭉치 오늘의 브리핑 부탁해",
        "놓친 브리핑을 한 번 받아볼래",
        "빠진 브리핑도 줘",
        "지금 브리핑 한번 받을래?",
        "브리핑 해 봐!",
        "브리핑 부탁 🙏",
        "아침 브리핑을 해주세요",
        "브리핑 해줄래 :pray:",
        "뭉치 브리핑 해 줘",
    ],
)
def test_briefing_requests(text):
    assert is_briefing_request(text)
    assert parse_quick_info(text) == set()  # never a weather or credit shortcut


@pytest.mark.parametrize(
    "text",
    [
        # The agreed examples: these need the agent.
        "브리핑 형식 바꿔줘",
        "브리핑에 날씨 빼줘",
        "어제 브리핑에서 말한 파일 뭐였지?",
        # More of the same: any word outside the closed list.
        "내일 브리핑 해줘",
        "브리핑 몇 시에 와?",
        "브리핑이 안 왔어",
        "브리핑 말고 일정만 알려줘",
        "날씨랑 브리핑",
        "업뎃 브리핑",
        "브리핑 왜 건너뛰었어?",
        "브리핑? 해줘",  # "?" in the middle is not filler
        "<@U123> 브리핑",
        # Filler alone, or nothing at all.
        "",
        "   ",
        "뭉치야",
        "오늘 좀 해줘",
        "건너뛴",
        "🙏",
        ":pray:",
    ],
)
def test_other_messages_are_not_briefing_requests(text):
    assert not is_briefing_request(text)
    assert not is_briefing_request(None)


def test_briefing_request_normalization_matches_the_quick_info_question():
    assert is_briefing_request(unicodedata.normalize("NFD", "오늘 건너뛴 브리핑 좀 해봐"))
    assert is_briefing_request("브리핑\t좀\n해줘")
    assert is_briefing_request("BRIEFING") is False  # Korean only
