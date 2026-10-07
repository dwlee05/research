"""``get_weather``: today's and tomorrow's weather for the 일정 agent (Open-Meteo, no key).

The data and the Korean line come from ``mungchi.weather``, the same code as
the briefing's weather line, ``--weather`` and the Slack shortcut.
"""

from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import ToolAnnotations, tool

from .common import MAX_RESULT_SIZE_CHARS, safe_error, tool_result


def run_weather() -> dict[str, Any]:
    """``weather.weather_payload()``. Blocking; never raises."""
    # Imported here: ``mungchi.weather`` imports ``tools.common`` (and so this
    # package), so importing it at the top would be circular.
    from .. import weather

    return weather.weather_payload()


@tool(
    "get_weather",
    (
        "설정된 곳(WEATHER_LABEL, 기본 서울)의 오늘·내일 날씨를 돌려준다(Open-Meteo). "
        "summary: 오늘 날씨 한 줄(그대로 써도 됨). today·tomorrow: description(날씨), min·max(°C), "
        "precipitation_probability(강수확률 %). current: temp(°C), description(지금 날씨). "
        "fine_dust: 미세먼지 등급(좋음/보통/나쁨/매우나쁨, 없으면 null). 인자 없음, 읽기 전용. "
        "ok=false면 error를 짧게 전하고 재시도하지 말 것. 모레 이후 날씨는 없다."
    ),
    {"type": "object", "properties": {}, "required": []},
    annotations=ToolAnnotations(readOnlyHint=True, maxResultSizeChars=MAX_RESULT_SIZE_CHARS),
)
async def get_weather(args: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = await asyncio.to_thread(run_weather)
    except Exception as exc:  # noqa: BLE001 - last line of defence
        payload = {"configured": True, "ok": False, "error": safe_error(exc)}
    return tool_result(payload)
