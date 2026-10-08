"""Pure helpers for the Slack front end: prompt text, mention stripping,
Markdown -> Slack mrkdwn safety net and message chunking. No Slack I/O here."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Callable

from .personas import MUNGCHI
from .phrases import PLACEHOLDER_POOLS

# Slack recommends keeping message text well under 4,000 characters.
MAX_CHUNK_CHARS = 3_500

# The first reply of each bot while its agent works is picked at random from
# its pool (``phrases.pick_placeholder``); this one is only the default of
# ``slack_bot.StatusUpdater``.
PLACEHOLDER_TEXT = PLACEHOLDER_POOLS[MUNGCHI][0]
WEEKDAYS_KO = ("월", "화", "수", "목", "금", "토", "일")
# First line of every briefing: "☀️ 오늘의 브리핑 (10/08 목)", bold in Slack.
BRIEF_TITLE = "오늘의 브리핑 ({date})"

# One Dropbox subfolder, as the prompts show it: the folder's link exactly
# once. In the terminal the bare link goes on the line under the folder name;
# in Slack the folder name is bold and the link follows on the same line.
EXAMPLE_FOLDER_LINK = "https://www.dropbox.com/home/20_%EC%97%B0%EA%B5%AC-%EC%A7%84%ED%96%89/01_ProjectA"
SLACK_FOLDER_LINK_LABEL = "📂 열기"
SLACK_FOLDER_LINE_EXAMPLE = f"• *01_ProjectA* <{EXAMPLE_FOLDER_LINK}|{SLACK_FOLDER_LINK_LABEL}>"

SLACK_FORMAT_PROMPT = (
    """\
## Slack 출력 (위 '출력' 지침보다 우선)
이 대화는 Slack 메시지로 오가고, 네 답은 Slack 스레드에 그대로 올라간다. 답은 Slack mrkdwn 문법으로 쓴다.
- 굵게는 *굵게*(별표 하나), 기울임은 _기울임_, 취소선은 ~취소선~ 으로 쓴다. **별표 두 개**는 쓰지 않는다.
- 제목에 # 을 쓰지 않는다. 섹션 제목은 *① 오늘의 일정* 처럼 굵은 한 줄로 쓴다.
- 목록은 "• " 로 시작하는 짧은 줄로 쓰고, 들여쓰기는 한 단계까지만 한다.
- Markdown 표를 쓰지 않는다. 표가 필요하면 목록으로 바꾼다.
- 링크는 <https://example.com|보이는 글자> 형식으로 쓴다. [글자](주소) 형식은 쓰지 않는다.
- Dropbox 하위 폴더는 위에서 말한 '이름 다음 줄에 주소' 대신, 폴더 이름을 굵게 쓰고 그 뒤에 그 폴더의 link를 한 번만 붙여 한 줄로 쓴다. 예:
"""
    + SLACK_FOLDER_LINE_EXAMPLE
    + """
  '폴더 열기:' 같은 말을 링크 앞에 덧붙이거나 같은 링크를 두 번 쓰지 않는다. 사람별 파일 줄은 그 아래에 들여 쓴다.
- 코드나 diff 조각은 ``` 로 감싼다.
- @channel, @here 같은 전체 알림은 절대 쓰지 않는다.
"""
)

# ---------------------------------------------------------------- incoming text

_ANY_LEADING_MENTION_RE = re.compile(r"^\s*(?:<@[A-Z0-9]+(?:\|[^>]*)?>\s*)+")


def strip_mention(text: str, bot_user_id: str | None = None) -> str:
    """Remove the bot's ``<@BOTID>`` mention(s) and decode Slack's ``&lt; &gt; &amp;``.

    Without a known bot id only leading mentions are removed.
    """
    text = text or ""
    if bot_user_id:
        text = re.sub(rf"<@{re.escape(bot_user_id)}(?:\|[^>]*)?>", " ", text)
    else:
        text = _ANY_LEADING_MENTION_RE.sub("", text)
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def brief_header(now: datetime, *, slack: bool = True) -> str:
    """``☀️ *오늘의 브리핑 (10/08 목)*`` (``now`` already in TIMEZONE); without the bold outside Slack."""
    title = BRIEF_TITLE.format(date=f"{now:%m/%d} {WEEKDAYS_KO[now.weekday()]}")
    return f"☀️ *{title}*" if slack else f"☀️ {title}"


# ---------------------------------------------------------------- code fences

_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")


def _fence_marker(line: str) -> str | None:
    match = _FENCE_RE.match(line)
    if not match:
        return None
    marker = match.group(1)
    # "```x```" on one line is inline code, not a fence opener.
    if marker[0] == "`" and "`" in line[match.end():]:
        return None
    return marker


def _closes(line: str, marker: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(marker[0] * len(marker)) and set(stripped) <= {marker[0]}


def _segments(text: str) -> list[tuple[bool, str]]:
    """Split ``text`` into ``(is_code_fence, text)`` runs of whole lines."""
    segments: list[tuple[bool, list[str]]] = []
    marker: str | None = None
    for line in text.split("\n"):
        if marker is None:
            opener = _fence_marker(line)
            if opener:
                marker = opener
                segments.append((True, [line]))
                continue
            if segments and not segments[-1][0]:
                segments[-1][1].append(line)
            else:
                segments.append((False, [line]))
        else:
            segments[-1][1].append(line)
            if _closes(line, marker):
                marker = None
    return [(is_code, "\n".join(lines)) for is_code, lines in segments]


# ---------------------------------------------------------------- mrkdwn

_INLINE_CODE_RE = re.compile(r"(`+)(?:(?!\1).)+?\1")
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_BULLET_RE = re.compile(r"^([ \t]*)[*+][ \t]+(?=\S)")
_BOLD_ITALIC_RE = re.compile(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*")
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_LINK_RE = re.compile(r"\[([^\[\]\n]+)\]\(((?:https?://|mailto:)[^\s()<>]+)\)")
# "폴더 열기: <url|폴더 열기>", possibly in parentheses, shows the same label twice in Slack
# ("01_ProjectA (폴더 열기: 폴더 열기)"). The label in front of the link is dropped.
_DUPLICATE_LABEL_RE = re.compile(
    r"(\(\s*)?(?<![^\s(])([^\s()<>|:：][^()<>|:：\n]{0,40}?)\s*[:：]\s*"
    r"<((?:https?://|mailto:)[^|>\s]+)\|\2>(\s*\))?"
)
# @channel/@here/@everyone pings and user-group pings are never sent by the bots.
_BROADCAST_RE = re.compile(r"<!(here|channel|everyone)(?:\|[^>]*)?>")
_SUBTEAM_RE = re.compile(r"<!subteam\^[A-Z0-9]+(?:\|([^>]*))?>")


def _outside_inline_code(line: str, convert: Callable[[str], str]) -> str:
    out: list[str] = []
    pos = 0
    for match in _INLINE_CODE_RE.finditer(line):
        out.append(convert(line[pos:match.start()]))
        out.append(match.group(0))
        pos = match.end()
    out.append(convert(line[pos:]))
    return "".join(out)


def _drop_duplicate_label(match: re.Match[str]) -> str:
    opened, label, url, closed = match.groups()
    # Parentheses that wrapped only "label: <link>" go with the label; a lone one stays.
    before = opened if opened and not closed else ""
    after = closed if closed and not opened else ""
    return f"{before}<{url}|{label}>{after}"


def _inline(text: str) -> str:
    text = _LINK_RE.sub(lambda m: f"<{m.group(2)}|{m.group(1).replace('|', '/')}>", text)
    text = _DUPLICATE_LABEL_RE.sub(_drop_duplicate_label, text)
    text = _BOLD_ITALIC_RE.sub(r"*_\1_*", text)
    text = _BOLD_RE.sub(r"*\1*", text)
    return _STRIKE_RE.sub(r"~\1~", text)


def _convert_line(line: str) -> str:
    heading = _HEADING_RE.match(line)
    if heading:
        inner = _outside_inline_code(heading.group(1), lambda s: _inline(s.replace("**", "")).replace("*", ""))
        return f"*{inner}*"
    line = _BULLET_RE.sub(r"\1• ", line)
    return _outside_inline_code(line, _inline)


def neutralize_broadcasts(text: str) -> str:
    text = _BROADCAST_RE.sub(lambda m: "@" + m.group(1), text)
    return _SUBTEAM_RE.sub(lambda m: "@" + (m.group(1) or "group").lstrip("@"), text)


def to_mrkdwn(text: str) -> str:
    """Conservative Markdown -> Slack mrkdwn safety net.

    Converts ``**x**`` -> ``*x*``, ``# heading`` -> ``*heading*``,
    ``[text](url)`` -> ``<url|text>`` (plus ``~~x~~`` and ``*``/``+`` bullets),
    and drops a label written twice (``폴더 열기: <url|폴더 열기>`` -> ``<url|폴더 열기>``).
    Code fences and inline code are left untouched. Broadcast pings
    (``<!channel>``, ``<!here>``, ...) are neutralized everywhere.
    """
    out: list[str] = []
    for is_code, segment in _segments(text or ""):
        if is_code:
            out.append(segment)
        else:
            out.append("\n".join(_convert_line(line) for line in segment.split("\n")))
    return neutralize_broadcasts("\n".join(out))


# ---------------------------------------------------------------- chunking


def _units(text: str) -> list[tuple[str, str | None, str]]:
    """Paragraphs and whole code fences as ``(text, fence_opener, separator_before)``."""
    units: list[tuple[str, str | None, str]] = []
    blank_before = False

    def emit(unit: str, opener: str | None) -> None:
        nonlocal blank_before
        units.append((unit, opener, "\n\n" if blank_before else "\n"))
        blank_before = False

    for is_code, segment in _segments(text):
        if is_code:
            emit(segment, segment.split("\n", 1)[0])
            continue
        para: list[str] = []
        for line in segment.split("\n"):
            if line.strip():
                para.append(line)
                continue
            if para:
                emit("\n".join(para), None)
                para = []
            blank_before = True
        if para:
            emit("\n".join(para), None)
    return units


def _hard_split(line: str, limit: int) -> list[str]:
    """Split one over-long line, preferring whitespace."""
    pieces: list[str] = []
    while len(line) > limit:
        cut = line.rfind(" ", limit // 2, limit + 1)
        if cut <= 0:
            cut = limit
        pieces.append(line[:cut].rstrip())
        line = line[cut:].lstrip()
    if line:
        pieces.append(line)
    return pieces


def _split_lines(lines: list[str], limit: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    for line in lines:
        for part in _hard_split(line, limit) if len(line) > limit else [line]:
            candidate = f"{current}\n{part}" if current else part
            if current and len(candidate) > limit:
                pieces.append(current)
                current = part
            else:
                current = candidate
    if current:
        pieces.append(current)
    return pieces


def _split_fence(block: str, opener: str, limit: int) -> list[str]:
    """Split an over-long code fence, closing and reopening it in every piece."""
    lines = block.split("\n")
    marker = _fence_marker(opener) or "```"
    body = lines[1:]
    if body and _closes(body[-1], marker):
        body = body[:-1]
    room = limit - len(opener) - len(marker) - 2
    if room < 20:  # limit too small for fences: plain line split
        return _split_lines(lines, limit)
    return [f"{opener}\n{piece}\n{marker}" for piece in _split_lines(body, room)]


def chunk_text(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split ``text`` into Slack-sized chunks of at most ``limit`` characters.

    Breaks on paragraph boundaries first, then line boundaries. A code fence
    is kept whole when it fits; otherwise it is closed at the end of a chunk
    and reopened at the start of the next.
    """
    chunks: list[str] = []
    current = ""
    for unit, opener, sep in _units((text or "").strip("\n")):
        if len(unit) <= limit:
            pieces = [unit]
        elif opener is not None:
            pieces = _split_fence(unit, opener, limit)
        else:
            pieces = _split_lines(unit.split("\n"), limit)
        for piece in pieces:
            candidate = f"{current}{sep}{piece}" if current else piece
            if current and len(candidate) > limit:
                chunks.append(current)
                current = piece
            else:
                current = candidate
            sep = "\n"
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]
