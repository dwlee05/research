"""Adding calendar events from a pasted note: propose, then create only after "네".

The model only *proposes* (``propose_calendar_events``): it extracts the
events from the note, and code here checks and normalizes them, flags
problems (missing time, weekday mismatch, past date, likely duplicates),
stores the proposal under the run's conversation key and returns a Korean
preview. No agent tool can create an event. Code creates them
(``create_proposal_events``) only after the user answered the preview with an
explicit yes (``reply_kind``): the Slack handler and the terminal chat check
that answer before any model runs.

A conversation key names where the answer will come from: a Slack thread of
one bot (``slack_conversation_key``) or one terminal chat
(``cli_conversation_key``). It is bound into each run's tool when the run's
options are built, never global, so concurrent runs never share proposals.
Proposals live in the state file (``StateStore``) and expire after 24 hours.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, Callable, Mapping, Sequence

from .. import config
from ..personas import PERSONAS
from ..slack_format import WEEKDAYS_KO
from ..state import StateStore, utcnow
from . import macos_calendar
from .common import safe_error

AdapterFactory = Callable[[tzinfo], macos_calendar.CalendarAdapter]

MAX_EVENTS = 10
MAX_TITLE_CHARS = 200
MAX_LOCATION_CHARS = 200
MAX_NOTES_CHARS = 2_000
MAX_SOURCE_EXCERPT_CHARS = 500
MAX_PREVIEW_NOTES_CHARS = 80
MAX_DUPLICATES_SHOWN = 3

# The question every preview ends with, word for word.
CONFIRM_QUESTION = "캘린더에 추가할까요? (네 / 아니요 / 고칠 내용)"
# The terminal chat asks this after a turn that made a proposal.
CLI_CONFIRM_PROMPT = "캘린더에 추가할까요? [네/아니요] "
CANCELLED_TEXT = "취소했어요"
ADDED_HEADER = "✅ 캘린더에 추가했어요"
NONE_ADDED_HEADER = "❌ 캘린더에 추가하지 못했어요"
DEFAULT_CALENDAR_LABEL = "기본 캘린더"
MAC_ONLY_TEXT = "Mac 캘린더 모드에서만 일정을 추가할 수 있어요."
ICS_SOURCE_NOTE = (
    "지금은 ICS 주소(CALENDAR_ICS_URLS)로 캘린더를 읽고 있어 일정을 넣을 수 없어요. "
    "Mac에서 .env의 CALENDAR_ICS_URLS를 비우거나 CALENDAR_SOURCE=macos로 바꾼 뒤 봇을 다시 시작하세요."
)
NOT_MAC_NOTE = "Mac 캘린더 앱(EventKit)은 macOS에서만 쓸 수 있어요."
ONE_SHOT_NOTE = (
    "질문 한 번(python -m mungchi \"...\")으로 실행해서 추가할지 확인을 받을 수 없어요. 이 제안은 저장하지 않았으니, "
    "대화 모드(python -m mungchi, --agent update, --agent schedule)나 Slack에서 다시 부탁해 주세요."
)
WRITE_ONLY_NOTE = "캘린더 권한이 '쓰기 전용'이라 이미 있는 일정과 겹치는지는 확인하지 못했어요."
WRITE_ONLY_CHOICE_TEXT = (
    "캘린더 권한이 '쓰기 전용'이라 캘린더를 골라 넣을 수 없어요(기본 캘린더에만 넣을 수 있어요). "
    f"{macos_calendar.SETTINGS_PATH}에서 봇을 실행하는 앱을 '전체 접근'으로 바꾸거나, CALENDAR_WRITE_TARGET을 비우세요."
)
NEEDS_TIME_WARNING = "시작 시각이 없어요. 몇 시인지 알려 주세요 (시각을 짐작해 넣지 않았어요)."
MIDNIGHT_WARNING = "시작이 자정(00:00)이에요. '오전 12시'였다면 자정이 맞는지 확인해 주세요."


# ---------------------------------------------------------------- conversation keys


def slack_conversation_key(persona: str, channel: str, thread_ts: str) -> str:
    """One Slack thread of one bot: where "네" for a proposal made there must come from."""
    if persona not in PERSONAS:
        raise ValueError(f"unknown persona: {persona!r}")
    return f"slack:{persona}:{channel}:{thread_ts}"


def cli_conversation_key() -> str:
    """One terminal chat session (a fresh key per ``python -m mungchi`` chat)."""
    return f"cli:{uuid.uuid4().hex}"


# ---------------------------------------------------------------- yes / no


YES = "yes"
NO = "no"
# Only short, exact answers count; anything longer goes to the model.
MAX_REPLY_CHARS = 12
AFFIRMATIVE_REPLIES = frozenset(
    {
        "네", "예", "응", "ㅇㅇ", "좋아", "추가해", "추가해줘", "등록해", "등록해줘", "넣어줘",
        "ok", "okay", "yes", "y", "👍", ":+1:", ":thumbsup:",
        # The same answers, politer or doubled.
        "넵", "네네", "좋아요", "추가해주세요", "등록해주세요", "넣어주세요", "ㅇㅋ", "오케이",
    }
)
NEGATIVE_REPLIES = frozenset(
    {
        "아니", "아니요", "아뇨", "취소", "됐어", "no", "n",
        "아니오", "취소해", "취소해줘", "취소해주세요", "됐어요",
    }
)
_SKIN_TONE_CODE_RE = re.compile(r"::skin-tone-\d:")
_SKIN_TONE_RE = re.compile("[\U0001F3FB-\U0001F3FF️]")
_REPLY_TRIM = ".!~,…。"


def normalize_reply(text: Any) -> str:
    """NFC, case-folded, without spaces, skin tones or trailing ``.!~``."""
    text = unicodedata.normalize("NFC", str(text or "")).casefold()
    text = _SKIN_TONE_RE.sub("", _SKIN_TONE_CODE_RE.sub(":", text))
    return "".join(text.split()).strip(_REPLY_TRIM)


def reply_kind(text: Any) -> str | None:
    """``YES`` / ``NO`` for a short, explicit answer to a preview, else None (a message for the model)."""
    reply = normalize_reply(text)
    if not reply or len(reply) > MAX_REPLY_CHARS:
        return None
    if reply in AFFIRMATIVE_REPLIES:
        return YES
    if reply in NEGATIVE_REPLIES:
        return NO
    return None


# ---------------------------------------------------------------- normalizing what the model extracted


@dataclass
class ProposedEvent:
    """One event of a proposal, checked and normalized.

    ``start``/``end`` are aware datetimes in ``TIMEZONE`` (``end`` exclusive:
    an all-day event ends at the next midnight), both None when the time is
    missing (``needs_time``: never created).
    """

    title: str
    day: date
    start: datetime | None
    end: datetime | None
    all_day: bool = False
    location: str = ""
    notes: str = ""
    needs_time: bool = False
    end_defaulted: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_state(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "date": self.day.isoformat(),
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "all_day": self.all_day,
            "location": self.location,
            "notes": self.notes,
            "needs_time": self.needs_time,
        }

    @classmethod
    def from_state(cls, data: Mapping[str, Any], tz: tzinfo) -> "ProposedEvent":
        """Rebuild a stored event; raises ``ValueError`` for anything malformed."""
        title = str(data.get("title") or "").strip()
        if not title:
            raise ValueError("제목이 없어요")
        day = date.fromisoformat(str(data.get("date") or ""))
        needs_time = bool(data.get("needs_time"))
        start = end = None
        if not needs_time:
            start = datetime.fromisoformat(str(data.get("start") or ""))
            end = datetime.fromisoformat(str(data.get("end") or ""))
            if start.tzinfo is None or end.tzinfo is None or end < start:
                raise ValueError("시각이 올바르지 않아요")
            start, end = start.astimezone(tz), end.astimezone(tz)
        return cls(
            title=title,
            day=day,
            start=start,
            end=end,
            all_day=bool(data.get("all_day")),
            location=str(data.get("location") or ""),
            notes=str(data.get("notes") or ""),
            needs_time=needs_time,
        )

    def to_payload(self) -> dict[str, Any]:
        """What the model sees for this event."""
        return {
            "title": self.title,
            "date": self.day.isoformat(),
            "weekday": WEEKDAYS_KO[self.day.weekday()],
            "start_time": None if self.all_day or self.start is None else f"{self.start:%H:%M}",
            "end_time": None if self.all_day or self.end is None else f"{self.end:%H:%M}",
            "all_day": self.all_day,
            "location": self.location,
            "notes": self.notes,
            "needs_time": self.needs_time,
            "end_defaulted": self.end_defaulted,
            "warnings": list(self.warnings),
        }


_FULL_DATE_RE = re.compile(r"^(\d{4})[-./](\d{1,2})[-./](\d{1,2})$")
_MONTH_DAY_RE = re.compile(r"^(\d{1,2})[-./](\d{1,2})$")
_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def resolve_event_date(text: Any, today: date) -> date:
    """``YYYY-MM-DD`` as written; ``MM-DD`` as its next occurrence on or after ``today``.

    Raises ``ValueError`` for anything else.
    """
    value = str(text or "").strip()
    full = _FULL_DATE_RE.match(value)
    if full:
        return date(int(full.group(1)), int(full.group(2)), int(full.group(3)))
    month_day = _MONTH_DAY_RE.match(value)
    if not month_day:
        raise ValueError(value)
    month, day = int(month_day.group(1)), int(month_day.group(2))
    for year in range(today.year, today.year + 5):  # Feb 29 may be a few years away
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= today:
            return candidate
    raise ValueError(value)


def parse_clock(value: Any) -> time | None:
    """``"HH:MM"`` (24-hour clock) -> ``time``; None/empty -> None; raises ``ValueError`` otherwise."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("null", "none"):
        return None
    match = _CLOCK_RE.match(text)
    if not match:
        raise ValueError(text)
    return time(int(match.group(1)), int(match.group(2)))


def _flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "예", "네")
    return bool(value)


def _clean(value: Any, limit: int, *, keep_lines: bool = False) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    if keep_lines:
        lines = [" ".join(line.split()) for line in text.splitlines()]
        text = "\n".join(line for line in lines if line)
    else:
        text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _weekday_in_text(value: Any) -> str | None:
    text = str(value or "").strip().strip("()（）").replace("요일", "").strip()
    return text if text in WEEKDAYS_KO else None


def date_label(day: date, today: date) -> str:
    """``10/22(목)``, with the year in front when it is not this year (``2027/01/05(화)``)."""
    stamp = f"{day:%m/%d}" if day.year == today.year else f"{day:%Y/%m/%d}"
    return f"{stamp}({WEEKDAYS_KO[day.weekday()]})"


def normalize_event(
    raw: Any, *, tz: tzinfo, now: datetime, default_minutes: int = config.DEFAULT_EVENT_MINUTES
) -> tuple[ProposedEvent | None, list[str]]:
    """One event as the model sent it -> ``(event, problems)``. Problems mean it cannot be proposed.

    Warnings (shown in the preview, the proposal still stands): missing start
    time (``needs_time``, never guessed), weekday in the note that does not
    match the date, a date or time already past, a start at midnight.
    """
    if not isinstance(raw, Mapping):
        return None, ["일정 하나는 title, date 등이 든 객체여야 해요."]
    now = now.astimezone(tz)
    today = now.date()
    problems: list[str] = []
    title = _clean(raw.get("title"), MAX_TITLE_CHARS)
    if not title:
        problems.append("제목(title)이 없어요.")
    try:
        day = resolve_event_date(raw.get("date"), today)
    except ValueError:
        problems.append(f"날짜(date) 값을 읽지 못했어요: '{raw.get('date')}'. YYYY-MM-DD로 적으세요.")
        day = None
    times: dict[str, time | None] = {}
    for key in ("start_time", "end_time"):
        try:
            times[key] = parse_clock(raw.get(key))
        except ValueError:
            problems.append(f"{key} 값을 읽지 못했어요: '{raw.get(key)}'. 24시간제 HH:MM으로 적으세요(없으면 null).")
    if problems or day is None:
        return None, problems

    all_day = _flag(raw.get("all_day"))
    event = ProposedEvent(
        title=title,
        day=day,
        start=None,
        end=None,
        all_day=all_day,
        location=_clean(raw.get("location"), MAX_LOCATION_CHARS),
        notes=_clean(raw.get("notes"), MAX_NOTES_CHARS, keep_lines=True),
    )
    start_t, end_t = times.get("start_time"), times.get("end_time")
    if all_day:
        event.start = datetime.combine(day, time.min, tzinfo=tz)
        event.end = event.start + timedelta(days=1)
    elif start_t is None:
        event.needs_time = True
        event.warnings.append(NEEDS_TIME_WARNING)
    else:
        event.start = datetime.combine(day, start_t, tzinfo=tz)
        length = timedelta(minutes=default_minutes)
        if end_t is None:
            event.end, event.end_defaulted = event.start + length, True
        else:
            end = datetime.combine(day, end_t, tzinfo=tz)
            if end <= event.start:
                event.warnings.append(
                    f"끝 시각({end_t:%H:%M})이 시작({start_t:%H:%M})보다 빨라서 {default_minutes}분짜리로 잡았어요. "
                    "끝 시각을 확인해 주세요."
                )
                end, event.end_defaulted = event.start + length, True
            event.end = end
        if start_t == time(0, 0):
            event.warnings.append(MIDNIGHT_WARNING)

    in_text = _weekday_in_text(raw.get("weekday_in_text"))
    actual = WEEKDAYS_KO[day.weekday()]
    if in_text and in_text != actual:
        event.warnings.append(f"요일 불일치: 본문은 ({in_text})인데 날짜는 {actual}요일")

    if day < today:
        event.warnings.append(f"지난 날짜예요: {date_label(day, today)}. 연도나 날짜가 맞는지 확인해 주세요.")
    elif not all_day and event.start is not None and event.start < now:
        event.warnings.append(f"이미 지난 시각이에요: 오늘 {event.start:%H:%M}.")
    return event, []


def normalize_events(
    raw_events: Any, *, tz: tzinfo, now: datetime, default_minutes: int = config.DEFAULT_EVENT_MINUTES
) -> tuple[list[ProposedEvent], list[str]]:
    """Every event of a proposal -> ``(events, problems)``; any problem rejects the whole proposal."""
    if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)) or not raw_events:
        return [], ["events에 일정을 하나 이상 넣으세요."]
    if len(raw_events) > MAX_EVENTS:
        return [], [f"한 번에 {MAX_EVENTS}개까지만 제안할 수 있어요(받은 일정 {len(raw_events)}개). 나눠서 제안하세요."]
    events: list[ProposedEvent] = []
    problems: list[str] = []
    for index, raw in enumerate(raw_events, start=1):
        event, event_problems = normalize_event(raw, tz=tz, now=now, default_minutes=default_minutes)
        problems.extend(f"{index}번 일정: {problem}" for problem in event_problems)
        if event is not None:
            events.append(event)
    return (events, []) if not problems else ([], problems)


# ---------------------------------------------------------------- preview and report text


def _when(event: ProposedEvent) -> str:
    if event.all_day:
        return "종일"
    if event.needs_time or event.start is None or event.end is None:
        return "시간 미정"
    if event.end.date() != event.start.date() and event.end.time() != time.min:
        return f"{event.start:%H:%M}–{event.end:%m/%d %H:%M}"
    end = "24:00" if event.end.date() != event.start.date() else f"{event.end:%H:%M}"
    return f"{event.start:%H:%M}–{end}"


def _one_line(text: str, limit: int) -> str:
    flat = " / ".join(line.strip() for line in text.splitlines() if line.strip())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def event_summary(event: ProposedEvent, today: date) -> str:
    """``10/22(목) 12:00–13:00 신임교수모임 (10월)``."""
    return f"{date_label(event.day, today)} {_when(event)} {event.title}"


def event_line(event: ProposedEvent, calendar_label: str, today: date) -> str:
    """``• 10/22(목) 12:00–13:00 신임교수모임 (10월) · 메모: 발표: 김평식 교수님 · 캘린더: 연구``."""
    parts = [event_summary(event, today)]
    if event.location:
        parts.append(f"장소: {event.location}")
    if event.notes:
        parts.append(f"메모: {_one_line(event.notes, MAX_PREVIEW_NOTES_CHARS)}")
    if calendar_label:
        parts.append(f"캘린더: {calendar_label}")
    return "• " + " · ".join(parts)


def preview_text(events: Sequence[ProposedEvent], calendar_label: str, today: date, notes: Sequence[str] = ()) -> str:
    """The Korean preview block: one line per event, its warnings (⚠️) under it, general notes last."""
    lines: list[str] = []
    for event in events:
        lines.append(event_line(event, calendar_label, today))
        lines.extend(f"  ⚠️ {warning}" for warning in event.warnings)
    lines.extend(f"⚠️ {note}" for note in notes)
    return "\n".join(lines)


def _duplicate_text(record: Mapping[str, Any], today: date) -> str:
    start, end = record.get("start"), record.get("end")
    if record.get("all_day") or not isinstance(start, datetime) or not isinstance(end, datetime):
        when = "종일" if isinstance(start, datetime) else ""
        day = date_label(start.date(), today) if isinstance(start, datetime) else ""
    else:
        day, when = date_label(start.date(), today), f"{start:%H:%M}–{end:%H:%M}"
    title = str(record.get("title") or "").strip() or "(제목 없음)"
    calendar = str(record.get("calendar") or "")
    return " ".join(part for part in (day, when, title) if part) + (f" ({calendar})" if calendar else "")


# ---------------------------------------------------------------- propose (the tool's work)


def _calendar_source_problem(cfg: config.CalendarConfig) -> str | None:
    """None when events can be added (Mac Calendar app mode), else the Korean reason."""
    if cfg.source == config.CALENDAR_SOURCE_MACOS:
        return None
    if cfg.source == config.CALENDAR_SOURCE_ICS:
        return f"{MAC_ONLY_TEXT} {ICS_SOURCE_NOTE}"
    return f"{MAC_ONLY_TEXT} {NOT_MAC_NOTE} {cfg.hint}".strip()


def run_propose(
    args: Mapping[str, Any],
    conversation_key: str | None,
    *,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
    store: StateStore | None = None,
    adapter_factory: AdapterFactory | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    """Check the model's events and store them as the pending proposal of ``conversation_key``.

    Every call first drops the conversation's previous proposal, so only the
    latest preview can ever be confirmed (a call that fails leaves none).
    Without a key (a one-shot terminal question, a briefing run) nothing is
    stored: the preview is returned with ``can_confirm: false``. Creates nothing.
    """
    tz = config.get_timezone(env)
    now = (now or utcnow()).astimezone(tz)
    today = now.date()
    store = store or StateStore(config.get_state_path(env))
    if conversation_key:
        store.clear_pending_proposal(conversation_key)

    events, problems = normalize_events(
        args.get("events"), tz=tz, now=now, default_minutes=config.get_default_event_minutes(env)
    )
    if problems:
        return {"ok": False, "error": "일정을 제안하지 못했어요. 아래를 고쳐 다시 부르세요.", "errors": problems}

    cfg = config.load_calendar_config(env, platform=platform)
    source_problem = _calendar_source_problem(cfg)
    if source_problem:
        return {"ok": False, "error": source_problem}
    try:
        adapter = (adapter_factory or macos_calendar.default_adapter)(tz)
    except macos_calendar.EventKitUnavailable:
        return {"ok": False, "error": macos_calendar.EVENTKIT_MISSING_HINT}
    status = adapter.authorization_status()
    if status not in macos_calendar.WRITE_STATUSES:
        return {"ok": False, "error": macos_calendar.write_permission_hint(status)}

    requested = " ".join(str(args.get("calendar") or "").split()) or None
    target = config.get_calendar_write_target(env)
    if status == macos_calendar.WRITE_ONLY and (requested or target):
        return {"ok": False, "error": WRITE_ONLY_CHOICE_TEXT}
    writable = adapter.list_writable_calendars() if status == macos_calendar.GRANTED else []
    calendar_name, calendar_error = macos_calendar.resolve_write_calendar(writable, requested, target)
    if calendar_error:
        return {"ok": False, "error": calendar_error}
    calendar_label = calendar_name or DEFAULT_CALENDAR_LABEL

    notes: list[str] = []
    if status == macos_calendar.GRANTED:
        for event in events:
            start = event.start or datetime.combine(event.day, time.min, tzinfo=tz)
            end = event.end or start + timedelta(days=1)
            try:
                similar = adapter.find_similar_events(start, end, event.title)
            except Exception as exc:  # noqa: BLE001 - the proposal still stands
                event.warnings.append(f"이미 있는 일정과 겹치는지 확인하지 못했어요 ({safe_error(exc)}).")
                continue
            for record in similar[:MAX_DUPLICATES_SHOWN]:
                event.warnings.append(f"비슷한 일정이 이미 있어요: {_duplicate_text(record, today)}")
    else:
        notes.append(WRITE_ONLY_NOTE)

    stored = False
    if conversation_key:
        proposal = {
            "id": uuid.uuid4().hex,
            "calendar": calendar_name,
            "calendar_label": calendar_label,
            "events": [event.to_state() for event in events],
            "source_excerpt": _clean(args.get("source_note"), MAX_SOURCE_EXCERPT_CHARS, keep_lines=True),
        }
        store.save_pending_proposal(conversation_key, proposal, now)
        stored = True

    warnings = [f"{index}번 {event.title}: {warning}" for index, event in enumerate(events, 1) for warning in event.warnings]
    payload: dict[str, Any] = {
        "ok": True,
        "calendar": calendar_label,
        "events": [event.to_payload() for event in events],
        "warnings": warnings + notes,
        "needs_time": sum(1 for event in events if event.needs_time),
        "preview": preview_text(events, calendar_label, today, notes),
        "can_confirm": stored,
    }
    if stored:
        payload["confirm_question"] = CONFIRM_QUESTION
    else:
        payload["note"] = ONE_SHOT_NOTE
    return payload


# ---------------------------------------------------------------- create (code only, after "네")


@dataclass
class CreationResult:
    """What happened to one event of a confirmed proposal."""

    summary: str
    ok: bool = False
    skipped: bool = False
    calendar: str = ""
    error: str = ""


@dataclass
class CreationOutcome:
    """All events of one confirmed proposal; ``fatal`` when nothing could even be tried."""

    results: list[CreationResult] = field(default_factory=list)
    fatal: str = ""

    @property
    def created(self) -> int:
        return sum(1 for result in self.results if result.ok)


def create_proposal_events(
    proposal: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
    adapter_factory: AdapterFactory | None = None,
    platform: str | None = None,
) -> CreationOutcome:
    """Create every event of a confirmed proposal through the Calendar app adapter. Never raises.

    Only the Slack handler and the terminal chat call this, right after the
    user answered the preview with an explicit yes. Events without a start
    time (``needs_time``) are skipped; each other event succeeds or fails on
    its own.
    """
    tz = config.get_timezone(env)
    today = (now or utcnow()).astimezone(tz).date()
    outcome = CreationOutcome()
    # In proposal order: the event, or the result explaining why it could not be read back.
    events: list[ProposedEvent | CreationResult] = []
    for index, data in enumerate(proposal.get("events") or [], start=1):
        try:
            events.append(ProposedEvent.from_state(data, tz))
        except (ValueError, TypeError, AttributeError) as exc:
            events.append(CreationResult(summary=f"{index}번 일정", error=f"저장된 제안을 읽지 못했어요 ({exc})"))
    cfg = config.load_calendar_config(env, platform=platform)
    problem = _calendar_source_problem(cfg)
    adapter = None
    if problem is None:
        try:
            adapter = (adapter_factory or macos_calendar.default_adapter)(tz)
            status = adapter.authorization_status()
            if status not in macos_calendar.WRITE_STATUSES:
                problem = macos_calendar.write_permission_hint(status)
        except macos_calendar.EventKitUnavailable:
            problem = macos_calendar.EVENTKIT_MISSING_HINT
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            problem = f"Mac 캘린더를 열지 못했어요 ({safe_error(exc)})"
    if problem is not None:
        outcome.fatal = problem
        return outcome
    calendar_name = proposal.get("calendar") or None
    for event in events:
        if isinstance(event, CreationResult):
            outcome.results.append(event)
            continue
        summary = event_summary(event, today)
        if event.needs_time or event.start is None or event.end is None:
            outcome.results.append(CreationResult(summary=summary, skipped=True))
            continue
        try:
            created = adapter.create_event(
                event.title,
                event.start,
                event.end,
                event.all_day,
                location=event.location or None,
                notes=event.notes or None,
                calendar_name=calendar_name,
            )
        except Exception as exc:  # noqa: BLE001 - the other events still go in
            created = {"ok": False, "error": safe_error(exc)}
        outcome.results.append(
            CreationResult(
                summary=summary,
                ok=bool(created.get("ok")),
                calendar=str(created.get("calendar") or ""),
                error=str(created.get("error") or ("" if created.get("ok") else "알 수 없는 오류")),
            )
        )
    return outcome


def creation_report(outcome: CreationOutcome) -> str:
    """Korean reply after "네": ``✅ 캘린더에 추가했어요`` and one line per event (❌ for failures)."""
    if outcome.fatal:
        return f"{NONE_ADDED_HEADER}: {outcome.fatal}"
    lines = [ADDED_HEADER if outcome.created else NONE_ADDED_HEADER]
    for result in outcome.results:
        if result.ok:
            lines.append(f"• {result.summary}" + (f" · 캘린더: {result.calendar}" if result.calendar else ""))
        elif result.skipped:
            lines.append(f"⏭️ {result.summary}: 시작 시각이 없어 추가하지 않았어요. 시각을 알려 주시면 다시 제안할게요.")
        else:
            lines.append(f"❌ {result.summary} 추가 실패: {result.error}")
    return "\n".join(lines)


def answer_text(
    proposal: Mapping[str, Any],
    kind: str,
    *,
    create: Callable[[Mapping[str, Any]], CreationOutcome] | None = None,
) -> str:
    """The reply to a yes / no for a proposal already taken out of the store.

    Only ``YES`` creates anything. Blocking: EventKit runs here. Never raises.
    """
    if kind != YES:
        return CANCELLED_TEXT
    try:
        outcome = (create or create_proposal_events)(proposal)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        outcome = CreationOutcome(fatal=f"오류가 났어요 ({safe_error(exc)})")
    return creation_report(outcome)


def confirm_proposal(
    store: StateStore,
    key: str,
    kind: str,
    *,
    now: datetime | None = None,
    create: Callable[[Mapping[str, Any]], CreationOutcome] | None = None,
) -> str | None:
    """Apply a yes / no answer to the pending proposal of ``key``; the reply text, or None if none is pending.

    The proposal is taken out of the store first (so it is never created
    twice) and is gone afterwards whatever happens. Blocking: EventKit runs here.
    """
    proposal = store.take_pending_proposal(key, now or utcnow())
    if proposal is None:
        return None
    return answer_text(proposal, kind, create=create)
