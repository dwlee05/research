"""``get_credits``: 고뭉치's read-only look at the Chat KHU (Mindlogic) API credits.

The data and the Korean summary come from ``mungchi.credits``, the same code
as ``--credits``, the Slack shortcut and the low-credit alert. Only the
gateway's two credit endpoints are called; the key stays in request headers.
"""

from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import ToolAnnotations, tool

from .common import MAX_RESULT_SIZE_CHARS, safe_error, tool_result


def run_credits() -> dict[str, Any]:
    """``credits.credit_payload()``. Blocking; never raises."""
    # Imported here: ``mungchi.credits`` imports ``tools.common`` (and so this
    # package), so importing it at the top would be circular.
    from .. import credits

    return credits.credit_payload()


@tool(
    "get_credits",
    (
        "Chat KHU(Mindlogic) API 크레딧을 돌려준다. 사용자가 말하는 '토큰'·'크레딧'은 이것이다(암호화폐 아님). "
        "summary: 한국어 요약(그대로 써도 됨). total·monthly: quota(한도), used(사용), remaining(남음), "
        "total.remaining_percent(남은 비율 %). renewal_date: 갱신일(YYYY-MM-DD). "
        "usage: 이번 주기 사용(start, end, calls, credits, models: 많이 쓴 모델 순). "
        "projection: 이 속도면 이번 주기 끝까지 쓸 양(credits, percent_of_quota), 아직 모르면 null. "
        "인자 없음, 읽기 전용, 모델 호출 없음. ok=false면 error(와 hint)를 짧게 전하고 재시도하지 말 것."
    ),
    {"type": "object", "properties": {}, "required": []},
    annotations=ToolAnnotations(readOnlyHint=True, maxResultSizeChars=MAX_RESULT_SIZE_CHARS),
)
async def get_credits(args: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = await asyncio.to_thread(run_credits)
    except Exception as exc:  # noqa: BLE001 - last line of defence
        payload = {"configured": True, "ok": False, "error": safe_error(exc)}
    return tool_result(payload)
