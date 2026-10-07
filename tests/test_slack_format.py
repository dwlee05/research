from __future__ import annotations

import pytest

from datetime import datetime
from zoneinfo import ZoneInfo

from mungchi.slack_format import (
    MAX_CHUNK_CHARS,
    SLACK_FORMAT_PROMPT,
    brief_header,
    chunk_text,
    strip_mention,
    to_mrkdwn,
)


# ---------------------------------------------------------------- mentions


def test_strip_mention_removes_bot_mention_and_decodes_entities():
    assert strip_mention("<@UBOT> 오늘 일정 알려줘", "UBOT") == "오늘 일정 알려줘"
    assert strip_mention("안녕 <@UBOT|mungchi>  a &lt; b &amp;&amp; c &gt; d", "UBOT") == "안녕 a < b && c > d"
    # Other people's mentions stay; they are part of the question.
    assert strip_mention("<@UBOT> <@UKIM> 가 뭐 고쳤어?", "UBOT") == "<@UKIM> 가 뭐 고쳤어?"
    # Multi-line questions keep their line breaks.
    assert strip_mention("<@UBOT>\n첫 줄\n둘째 줄", "UBOT") == "첫 줄\n둘째 줄"


def test_strip_mention_empty_after_mention_and_unknown_bot_id():
    assert strip_mention("<@UBOT>", "UBOT") == ""
    assert strip_mention("  <@UBOT>  ", "UBOT") == ""
    assert strip_mention("", "UBOT") == ""
    # Without a known bot id only leading mentions are removed.
    assert strip_mention("<@UBOT> 일정", None) == "일정"
    assert strip_mention("일정 <@UKIM>", None) == "일정 <@UKIM>"


def test_brief_header_uses_local_date():
    now = datetime(2026, 10, 5, 7, 50, tzinfo=ZoneInfo("Asia/Seoul"))
    assert brief_header(now) == "☀️ *오늘의 브리핑 (2026-10-05)*"


def test_slack_prompt_covers_mrkdwn_rules():
    for rule in ("*굵게*", "_기울임_", "#", "표", "<https://example.com|보이는 글자>"):
        assert rule in SLACK_FORMAT_PROMPT


# ---------------------------------------------------------------- mrkdwn


def test_mrkdwn_converts_bold_headings_links_and_bullets():
    md = "\n".join(
        [
            "### ① 공저자 업데이트",
            "## **굵은 제목** ##",
            "* **김OO**: 서론 수정 ([커밋](https://example.com/c/1))",
            "+ 두 번째",
            "- 그대로 둠",
            "***중요*** 와 ~~취소~~",
        ]
    )
    assert to_mrkdwn(md).split("\n") == [
        "*① 공저자 업데이트*",
        "*굵은 제목*",
        "• *김OO*: 서론 수정 (<https://example.com/c/1|커밋>)",
        "• 두 번째",
        "- 그대로 둠",
        "*_중요_* 와 ~취소~",
    ]


def test_mrkdwn_leaves_code_untouched():
    fence = "```python\nx = '**keep**'  # [a](https://b.c)\n# not a heading\n* not a bullet\n```"
    text = f"**바깥**\n{fence}\n`**inline**` 와 **밖** 와 ``[x](https://y.z)``"
    out = to_mrkdwn(text)
    assert fence in out
    assert out.startswith("*바깥*\n")
    assert out.endswith("`**inline**` 와 *밖* 와 ``[x](https://y.z)``")


def test_mrkdwn_handles_unclosed_fence_and_one_line_triple_backticks():
    assert to_mrkdwn("```\n**still code**") == "```\n**still code**"
    # ```x``` on one line is inline code, not a fence that swallows the rest.
    assert to_mrkdwn("```x``` 다음 **굵게**") == "```x``` 다음 *굵게*"


def test_mrkdwn_is_conservative_and_idempotent():
    plain = "C# 과 #hashtag, 2*3*4, snake_case_name, <https://a.b|이미 링크>, a < b"
    assert to_mrkdwn(plain) == plain
    once = to_mrkdwn("# 제목\n**굵게** [링크](https://example.com)")
    assert to_mrkdwn(once) == once


def test_mrkdwn_neutralizes_broadcast_pings_everywhere():
    out = to_mrkdwn("<!channel> 보세요 <!here|here> `<!everyone>` <!subteam^S123|@team>")
    assert "<!" not in out
    assert "@channel" in out and "@here" in out and "@everyone" in out and "@team" in out


# ---------------------------------------------------------------- chunking


def test_short_text_is_one_chunk():
    assert chunk_text("안녕하세요") == ["안녕하세요"]
    assert chunk_text("") == []
    assert chunk_text("\n\n  \n") == []


def test_chunks_respect_limit_and_paragraph_boundaries():
    paragraphs = [f"문단 {i}: " + "가" * 60 for i in range(40)]
    text = "\n\n".join(paragraphs)
    chunks = chunk_text(text, limit=500)
    assert len(chunks) > 1
    assert all(len(chunk) <= 500 for chunk in chunks)
    # Every chunk ends at a paragraph boundary: no paragraph is split.
    for chunk in chunks:
        for para in chunk.split("\n\n"):
            assert para in paragraphs
    assert "\n\n".join(chunks) == text


def test_long_paragraph_splits_on_lines_then_whitespace():
    lines = [f"- 항목 {i} " + "나" * 30 for i in range(50)]
    chunks = chunk_text("\n".join(lines), limit=300)
    assert all(len(c) <= 300 for c in chunks)
    assert "\n".join(chunks).split("\n") == lines
    long_line = " ".join(["단어"] * 400)
    chunks = chunk_text(long_line, limit=200)
    assert all(len(c) <= 200 for c in chunks)
    assert " ".join(chunks).split() == long_line.split()


def test_code_fence_is_moved_whole_to_next_chunk_when_it_fits():
    intro = "소개 " * 40
    fence = "```diff\n" + "\n".join(f"+line {i}" for i in range(10)) + "\n```"
    assert len(intro) + len(fence) > 200
    chunks = chunk_text(f"{intro}\n\n{fence}\n\n끝", limit=200)
    assert all(len(c) <= 200 for c in chunks)
    assert chunks[0].strip() == intro.strip()
    assert chunks[1].startswith(fence)
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0


def test_oversized_code_fence_is_closed_and_reopened():
    fence = "```python\n" + "\n".join(f"value_{i} = {i}" for i in range(200)) + "\n```"
    chunks = chunk_text(f"앞 문단\n\n{fence}\n\n뒷 문단", limit=400)
    assert len(chunks) > 2
    assert all(len(c) <= 400 for c in chunks)
    code_chunks = [c for c in chunks if "value_" in c]
    for chunk in code_chunks:
        assert chunk.count("```") % 2 == 0  # every piece is a complete fence
        assert "```python\n" in chunk
    # No code line is lost or duplicated.
    seen = [line for c in code_chunks for line in c.split("\n") if line.startswith("value_")]
    assert seen == [f"value_{i} = {i}" for i in range(200)]


def test_default_limit_is_3500():
    assert MAX_CHUNK_CHARS == 3_500
    text = "\n".join("줄 " + "다" * 90 for _ in range(100))
    chunks = chunk_text(text)
    assert len(chunks) >= 3
    assert all(len(c) <= 3_500 for c in chunks)


def test_slack_prompt_formats_dropbox_folder_links():
    from mungchi.slack_format import SLACK_FOLDER_LINE_EXAMPLE

    assert SLACK_FOLDER_LINE_EXAMPLE.startswith("• *01_Youn* <https://www.dropbox.com/home/")
    assert SLACK_FOLDER_LINE_EXAMPLE.endswith("|📂 열기>")
    assert "\n" + SLACK_FOLDER_LINE_EXAMPLE + "\n" in SLACK_FORMAT_PROMPT
    assert "<주소|폴더 열기>" not in SLACK_FORMAT_PROMPT


@pytest.mark.parametrize(
    "text,expected",
    [
        ("• 01_Youn (폴더 열기: <https://x.y/a|폴더 열기>)", "• 01_Youn <https://x.y/a|폴더 열기>"),
        ("- 01_Youn (폴더 열기: [폴더 열기](https://x.y/a))", "- 01_Youn <https://x.y/a|폴더 열기>"),
        ("폴더 열기: <https://x.y/a|폴더 열기>", "<https://x.y/a|폴더 열기>"),
        ("• 01_Youn 폴더 열기：<https://x.y/a|폴더 열기> 끝", "• 01_Youn <https://x.y/a|폴더 열기> 끝"),
        ("(폴더 열기: <https://x.y/a|폴더 열기>", "(<https://x.y/a|폴더 열기>"),
    ],
)
def test_mrkdwn_never_shows_a_link_label_twice(text, expected):
    assert to_mrkdwn(text) == expected
    assert to_mrkdwn(expected) == expected


@pytest.mark.parametrize(
    "text",
    [
        "• *01_Youn* <https://x.y/a|📂 열기>",
        "참고: <https://x.y/a|문서>",  # different label: left alone
        "• 01_Youn\n  https://x.y/a",  # terminal layout: plain URL on its own line
        "a" * 5_000,
    ],
)
def test_mrkdwn_keeps_single_labels_as_they_are(text):
    assert to_mrkdwn(text) == text
