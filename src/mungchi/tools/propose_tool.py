"""``propose_calendar_events``: turn a pasted note into a calendar proposal (never creates anything).

The tool is built per run (``make_propose_calendar_events``) with that run's
conversation key bound into it, like the Dropbox tool's briefing mode: the
model cannot choose or see the key, and concurrent runs in one process (two
Slack threads, two bots) never share proposals. Creating the events is left
to code, after the user answers the preview: picks a category (by number,
name, "네" for the suggested one, or a Slack button) or, without categories,
says "네" (``event_proposals``). The model only suggests a category.
"""

from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import SdkMcpTool, ToolAnnotations

from . import event_proposals
from .common import MAX_RESULT_SIZE_CHARS, safe_error, tool_result

TOOL_NAME = "propose_calendar_events"

TOOL_DESCRIPTION = (
    "붙여 넣은 메모·공지에서 뽑은 일정을 캘린더 추가 '제안'으로 만든다. 캘린더에 추가하지는 않는다: "
    "사용자가 미리보기를 보고 카테고리를 고르거나 '네'라고 답해야 프로그램이 추가하며, 네가 추가할 방법은 없다. "
    f"events는 1~{event_proposals.MAX_EVENTS}개. 각 일정: title(짧고 알아보기 쉬운 제목), date(YYYY-MM-DD), "
    "start_time·end_time(24시간제 HH:MM, 모르면 null), all_day(종일 여부), location(있으면), notes(발표자 등 메모), "
    "weekday_in_text(본문에 (목)처럼 요일이 있으면 그 한 글자), category(사용자가 그 일정의 카테고리를 정해 줬을 때만). "
    "suggested_category에는 넣을 카테고리(캘린더) 하나를 추천만 한다(Family, Teaching, Research, Event-Outside, Event-KHU "
    "가운데, 결과의 categories가 다르면 그 가운데서). 고르는 것은 사용자다. "
    "끝 시각이 없으면 기본 길이(DEFAULT_EVENT_MINUTES, 기본 60분)로 잡고, 시작 시각이 없고 종일도 아니면 needs_time으로 "
    "표시한다(시각을 짐작해 넣지 말 것). 요일 불일치, 지난 날짜, 이미 있는 비슷한 일정, 없는 카테고리 캘린더는 warnings로 알려 준다. "
    "결과의 preview(미리보기)와 warnings를 그대로 보여 주고, can_confirm이 true면 결과의 confirm_question을 그대로 마지막 줄로 쓴다. "
    "can_confirm이 false면 note를 전한다. ok=false면 error(와 errors)를 보고 고칠 수 있으면 고쳐 다시 부르고, "
    "설정·권한 문제면 그대로 전한다. 같은 대화에서 다시 부르면 앞의 제안은 사라지고 새 제안만 남는다."
)

# The SDK validates arguments against this schema before the handler runs
# (with English messages), so limits the code checks itself (at most 10
# events, the weekday letter) are left to the code and its Korean errors.
INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "description": f"메모에서 뽑은 일정들(1~{event_proposals.MAX_EVENTS}개).",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "짧고 알아보기 쉬운 제목. 예: 신임교수모임 (10월)"},
                    "date": {
                        "type": "string",
                        "description": "YYYY-MM-DD. 본문에 연도가 없으면 오늘 이후(오늘 포함) 가장 가까운 그 날짜의 연도.",
                    },
                    "start_time": {
                        "type": ["string", "null"],
                        "description": "24시간제 HH:MM. '오후 12시'는 12:00, '오전 12시'는 00:00. 모르면 null.",
                    },
                    "end_time": {"type": ["string", "null"], "description": "24시간제 HH:MM. 없으면 null."},
                    "all_day": {"type": "boolean", "default": False, "description": "종일 일정이면 true."},
                    "location": {"type": ["string", "null"], "description": "장소(있으면)."},
                    "notes": {"type": ["string", "null"], "description": "발표자·준비물 등 메모(있으면). 예: 발표: 김평식 교수님"},
                    "weekday_in_text": {
                        "type": ["string", "null"],
                        "description": "본문에 적힌 요일 한 글자(월·화·수·목·금·토·일, 예: (목) → 목). 없으면 넣지 않는다.",
                    },
                    "category": {
                        "type": ["string", "null"],
                        "description": (
                            "이 일정만의 카테고리. 사용자가 일정마다 정해 줬을 때만 넣는다"
                            "(예: '1번은 Research, 2번은 Event-KHU'). 네가 정하지 않는다. 없으면 넣지 않는다."
                        ),
                    },
                },
                "required": ["title", "date"],
            },
        },
        "source_note": {"type": "string", "description": "사용자가 붙여 넣은 메모 원문."},
        "suggested_category": {
            "type": "string",
            "description": (
                "추천 카테고리 하나(사용자가 고른다). Family: 가족·개인 일. Teaching: 강의·수업·학생·채점·조교(TA)·시험. "
                "Research: 논문·공저자·실험·IRB·연구 회의. Event-KHU: 경희대 안의 회의·행사, 학과·단과대 행사(예: 신임교수모임). "
                "Event-Outside: 경희대 밖의 학회·워크숍·세미나·외부 행사. 애매하면 넣지 않는다."
            ),
        },
        "calendar": {
            "type": "string",
            "description": (
                "사용자가 넣을 캘린더 이름을 말했을 때만(예: '연구 캘린더에 넣어줘' → 연구). 없으면 넣지 않는다. "
                "카테고리를 쓰는 설정이면 추천 카테고리로 본다."
            ),
        },
    },
    "required": ["events"],
}


def make_propose_calendar_events(conversation_key: str | None = None) -> SdkMcpTool[Any]:
    """A ``propose_calendar_events`` tool bound to one run's conversation key.

    ``None`` (one-shot terminal questions, briefings): the preview is
    returned but nothing is stored, so nothing can be confirmed.
    """

    async def propose_calendar_events(args: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = await asyncio.to_thread(event_proposals.run_propose, args, conversation_key)
        except Exception as exc:  # noqa: BLE001 - last line of defence
            payload = {"ok": False, "error": f"일정을 제안하지 못했어요: {safe_error(exc)}"}
        return tool_result(payload)

    return SdkMcpTool(
        name=TOOL_NAME,
        description=TOOL_DESCRIPTION,
        input_schema=INPUT_SCHEMA,
        handler=propose_calendar_events,
        # Writes only the pending proposal in the state file, never the calendar.
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, maxResultSizeChars=MAX_RESULT_SIZE_CHARS
        ),
    )


# Without a conversation key: what ``ALL_TOOLS`` lists.
propose_calendar_events = make_propose_calendar_events()
