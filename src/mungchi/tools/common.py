"""Helpers shared by the data tools: diff truncation, output budget, secret scrubbing."""

from __future__ import annotations

import json
import re
from datetime import datetime, tzinfo
from pathlib import PurePosixPath
from typing import Any, Iterable

from .. import config

TEXT_EXTENSIONS = frozenset(
    {".tex", ".bib", ".md", ".txt", ".py", ".r", ".m", ".sty", ".cls", ".csv"}
)
MAX_TEXT_FILE_BYTES = 200 * 1024
MAX_DIFF_LINES = 80
# LaTeX paragraphs often live on a single very long line, so cap characters too.
MAX_DIFF_CHARS = 8_000
MAX_TOTAL_CHARS = 30_000
# Part of the total budget kept for metadata (paths, names, timestamps).
METADATA_RESERVE_CHARS = 5_000

OMITTED_FOR_BUDGET = "전체 출력 한도(약 30,000자)를 넘어 diff 생략"
# Results stay inline up to this size instead of being written to a file
# (subagents have no Read tool to open such a file).
MAX_RESULT_SIZE_CHARS = 60_000

MAX_SINCE_HOURS = 24 * 90
SINCE_HOURS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "since_hours": {
            "type": "integer",
            "minimum": 0,
            "default": 0,
            "description": (
                "몇 시간 전부터 확인할지. 0(기본값)이면 마지막 확인 시각 이후, "
                "확인 기록이 없으면 LOOKBACK_DAYS일(기본 7일) 전부터."
            ),
        }
    },
    "required": [],
}


def is_text_path(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in TEXT_EXTENSIONS


def truncate_diff(
    text: str, max_lines: int = MAX_DIFF_LINES, max_chars: int = MAX_DIFF_CHARS
) -> tuple[str, int, bool]:
    """Clip ``text`` to ``max_lines`` lines and ``max_chars`` characters.

    Returns ``(clipped_text, omitted_line_count, truncated)``. When anything
    was cut a Korean marker line is appended so the model knows the diff is
    partial.
    """
    lines = text.splitlines()
    kept: list[str] = []
    used = 0
    cut_long_line = False
    for line in lines[:max_lines]:
        room = max_chars - used
        if len(line) + 1 > room:
            if room > 200:
                kept.append(line[:room] + " …")
                cut_long_line = True
            break
        kept.append(line)
        used += len(line) + 1
    omitted = len(lines) - len(kept)
    if omitted > 0:
        kept.append(f"… ({omitted}줄 생략)")
    elif cut_long_line:
        kept.append("… (긴 줄 일부 생략)")
    return "\n".join(kept), omitted, bool(omitted or cut_long_line)


class OutputBudget:
    """Keeps the total diff text of one tool call near ``MAX_TOTAL_CHARS``."""

    def __init__(
        self,
        max_chars: int = MAX_TOTAL_CHARS,
        reserve: int = METADATA_RESERVE_CHARS,
        max_lines: int = MAX_DIFF_LINES,
        max_diff_chars: int = MAX_DIFF_CHARS,
    ):
        self.max_chars = max_chars
        self.remaining = max(max_chars - reserve, 0)
        self.max_lines = max_lines
        self.max_diff_chars = max_diff_chars
        self.shortened: list[str] = []
        self.omitted: list[str] = []

    def fit(self, label: str, text: str) -> dict[str, Any]:
        """Return ``{"diff": ..., "diff_truncated": bool, ...}`` for one file/commit."""
        clipped, omitted_lines, truncated = truncate_diff(
            text, self.max_lines, self.max_diff_chars
        )
        if len(clipped) > self.remaining:
            self.omitted.append(label)
            return {"diff": None, "diff_note": OMITTED_FOR_BUDGET}
        self.remaining -= len(clipped)
        if truncated:
            self.shortened.append(label)
        result: dict[str, Any] = {"diff": clipped, "diff_truncated": truncated}
        if omitted_lines:
            result["omitted_lines"] = omitted_lines
        return result

    def report(self) -> dict[str, Any] | None:
        if not self.shortened and not self.omitted:
            return None
        return {
            "note": (
                f"diff는 파일당 약 {self.max_lines}줄, 전체 약 {self.max_chars:,}자로 잘랐습니다. "
                "잘린 부분은 직접 확인이 필요할 수 있습니다."
            ),
            "shortened": self.shortened,
            "omitted": self.omitted,
        }


def shrink_to_limit(payload: dict[str, Any], max_chars: int = MAX_TOTAL_CHARS) -> dict[str, Any]:
    """Last-resort guard: drop diffs (latest first) until the JSON fits."""
    if len(dumps(payload)) <= max_chars:
        return payload
    holders: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("diff"):
                holders.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    dropped = 0
    for holder in reversed(holders):
        holder["diff"] = None
        holder["diff_note"] = OMITTED_FOR_BUDGET
        dropped += 1
        if len(dumps(payload)) <= max_chars:
            break
    truncation = payload.setdefault("truncation", {})
    if isinstance(truncation, dict):
        truncation["dropped_for_total_limit"] = dropped
    return payload


# ---------------------------------------------------------------- secrets

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # HTTP auth headers (git -c http.extraHeader=..., requests, etc.)
    (re.compile(r"(?i)(authorization:\s*(?:basic|bearer|token))\s+\S+"), r"\1 ***"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{20,}"), r"\1 ***"),
    # Credentials embedded in URLs: https://user:secret@host
    (re.compile(r"(?i)\b(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1***@"),
    # Well-known token shapes.
    (re.compile(r"\bsl\.[A-Za-z0-9_-]{20,}"), "***"),  # Dropbox short-lived token
    (re.compile(r"\bolp_[A-Za-z0-9]{10,}"), "***"),  # Overleaf git token
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}"), "***"),  # Anthropic API key
    (re.compile(r"\bxox[a-z]-[A-Za-z0-9-]{10,}"), "***"),  # Slack bot/user/refresh token
    (re.compile(r"\bxapp-[A-Za-z0-9-]{10,}"), "***"),  # Slack app-level token
)

_URL_RE = re.compile(r"(?i)\b(?:https?|webcal)://\S+")


def scrub(text: str, secrets: Iterable[str] | None = None, redact_urls: bool = False) -> str:
    """Remove secrets from ``text``.

    ``secrets`` defaults to every configured secret env value. With
    ``redact_urls`` all URLs are replaced too (used for private ICS feeds,
    whose URL itself is the secret, including after redirects).
    """
    if not text:
        return text
    values = config.secret_values() if secrets is None else list(secrets)
    for value in sorted({v for v in values if v and len(v) >= 6}, key=len, reverse=True):
        text = text.replace(value, "***")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    if redact_urls:
        text = _URL_RE.sub("<URL>", text)
    return text


def safe_error(exc: BaseException, secrets: Iterable[str] | None = None, redact_urls: bool = False) -> str:
    message = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return scrub(message, secrets, redact_urls=redact_urls)[:500]


# ---------------------------------------------------------------- results


def int_arg(value: Any, default: int, minimum: int, maximum: int) -> int:
    """Coerce a tool argument to an int within bounds (models sometimes send strings)."""
    try:
        number = int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def unconfigured(missing: list[str], hint: str) -> dict[str, Any]:
    return {"configured": False, "missing": list(missing), "hint": hint}


def scrub_payload(node: Any, secrets: list[str]) -> Any:
    """Scrub every string inside a JSON-like structure.

    Scrubbing string by string (instead of the serialized JSON) keeps the
    regex patterns from matching across field boundaries and breaking JSON.
    """
    if isinstance(node, str):
        return scrub(node, secrets)
    if isinstance(node, dict):
        return {key: scrub_payload(value, secrets) for key, value in node.items()}
    if isinstance(node, (list, tuple)):
        return [scrub_payload(item, secrets) for item in node]
    return node


def tool_result(payload: dict[str, Any], secrets: Iterable[str] | None = None) -> dict[str, Any]:
    """Wrap a payload as an MCP text result, scrubbing secrets one final time."""
    values = config.secret_values() if secrets is None else list(secrets)
    return {"content": [{"type": "text", "text": dumps(scrub_payload(payload, values))}]}


def to_local_iso(dt: datetime, tz: tzinfo) -> str:
    return dt.astimezone(tz).isoformat(timespec="minutes")
