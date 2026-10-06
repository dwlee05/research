"""Helpers shared by the data tools: secret scrubbing, argument coercion, JSON results."""

from __future__ import annotations

import json
import re
from datetime import datetime, tzinfo
from typing import Any, Iterable

from .. import config

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

_URL_RE = re.compile(r"(?i)\b(?:https?|webcals?)://\S+")


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
