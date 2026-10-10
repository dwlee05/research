"""The briefing as a relay: 고뭉치 greets and hands off, 업뎃 and 일정 report their own parts.

``python -m mungchi --brief`` (terminal), ``--brief --slack``, the scheduled
morning briefing of the running Slack bots (``BRIEF_TIME``) and a short
briefing request to 고뭉치 in Slack ("오늘 건너뛴 브리핑 좀 해봐", or a bare
``@고뭉치``) all run the same relay (``start_relay``), in this order:

    고뭉치   좋은 아침이에요! 10월 8일 목요일 브리핑 시작할게요   ← one small LLM call (template if it fails)
             🌤️ 서울 날씨: 대체로 맑음 · ...                     ← code (Open-Meteo), no LLM; BRIEF_WEATHER=off drops it
             💳 Chat KHU 크레딧: ...                              ← code, no LLM
             @업뎃 @일정 아침 보고 부탁해요!                       ← hand-off, picked by code
    업뎃     its own report: Dropbox since the last briefing    ← direct 업뎃 run in briefing mode
    일정     its own report: today's schedule (days=1)          ← direct 일정 run

Everything starts at once: the weather and the credits in worker threads,
고뭉치's greeting (it needs the weather first), and the 업뎃 and 일정 runs.
The callers deliver the parts in that order as each one is ready; weather
and credits only ever appear in 고뭉치's part.

* **Time of day** (``BriefTime``, from the injected clock): the scheduled
  briefing is always 아침, even a late catch-up. A briefing asked for by
  hand (Slack, ``--brief``, ``--brief --slack``) follows the local clock:
  아침 05:00–10:59, 오후 11:00–16:59, 저녁 17:00–20:59, 밤 21:00–04:59.
  The greeting, the hand-off and 업뎃's / 일정's framing all use it, and
  from 17:00 to midnight a manual briefing also covers tomorrow
  (``BriefTime.covers_tomorrow``): 일정 looks at tomorrow too (``days=2``,
  both dates from code) and 고뭉치's part adds tomorrow's weather line
  under today's (``🌧️ 내일(10/11 토): 비 · ...``, left out when the
  forecast has no tomorrow).
* **Greeting**: one tool-less turn with a tiny constant system prompt
  (``GREETING_SYSTEM_PROMPT``). Its user prompt holds the time of day and
  the current time, today's date, a weekday/weekend note, the weather in
  words (numbers removed) and the recent greetings (the last seven
  scheduled ones), whose openings and structure the model is told not to
  repeat. "똑똑" is rare: when one of the recent greetings has it, the
  prompt says not to use it and the check rejects it. The answer is
  checked (``valid_greeting``: today's month/day present, no other date,
  year or weekday, no other numbers, no word of another time of day,
  short, no "똑똑" when it is not allowed, not a recent greeting again); a
  failure, a timeout (30 s) or an invalid answer falls back to a template
  for that time of day (``phrases.GREETING_TEMPLATES``) whose opening
  differs from the recent ones (``fallback_greeting``). Only the scheduled
  briefing stores the greeting it used (``recent_greetings``); every run
  reads the list.
* **업뎃**: ``briefing=True``, so its Dropbox tool looks at the time since the
  last briefing and moves that checkpoint. **일정**: today only, or today's
  rest and tomorrow for a manual briefing from 17:00 to midnight.
* A failed or timed-out (15 min in Slack) report becomes a short apology in
  that bot's voice with a scrubbed reason; the other parts go out anyway.

``last_brief_date`` (the scheduler's "already sent today") is never touched
here. ``brief_due`` is the pure "is the morning briefing due now?" check used
by the scheduler in ``slack_bot``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping, Sequence, TextIO

from . import config, credits, phrases, weather
from .agents import ACCURACY_RULE, VOICES
from .main import TurnResult, run_plain_turn, run_turn
from .personas import MUNGCHI, PERSONA_LABELS, SCHEDULE, UPDATE, josa
from .phrases import AFTERNOON, EVENING, MORNING, NIGHT, TIMES_OF_DAY
from .slack_format import SLACK_FORMAT_PROMPT, WEEKDAYS_KO
from .state import StateStore
from .tools.common import safe_error, scrub

log = logging.getLogger("mungchi.briefing")

RunTurn = Callable[..., Awaitable[TurnResult]]
CreditFetch = Callable[[], credits.CreditReport]
WeatherFetch = Callable[[], weather.WeatherReport]
# Takes the greeting's user prompt, returns the model's text (raises when it fails).
GreetingGenerate = Callable[[str], Awaitable[str]]

# Who reports after 고뭉치, in posting order.
REPORTERS = (UPDATE, SCHEDULE)

GREETING_TIMEOUT_SECONDS = 30.0
MAX_GREETING_CHARS = 120
MAX_REASON_CHARS = 200
# Room for the reason, an excerpt of the API's error text and a "→ ..." hint.
MAX_ERROR_CHARS = 600

CREDIT_UNSUPPORTED_NOTE = "확인 안 함 (Chat KHU 게이트웨이를 쓰지 않아요)"
CREDIT_FAILED_NOTE = "⚠️ 확인하지 못했어요 ({reason})"
# "크레딧을 확인하지 못했습니다 (HTTP 500)." -> "HTTP 500" (the note says the rest).
_CREDIT_ERROR_RE = re.compile(r"^크레딧(?:을 확인하지| 응답을 읽지) 못했습니다\s*\((.+)\)\.?$")

# ``brief_due`` outcomes.
DUE = "due"
OFF = "off"  # BRIEF_TIME unset or invalid
ALREADY = "already"  # today's briefing was already started (last_brief_date)
DAY_OFF = "day_off"  # BRIEF_DAYS=weekdays on a weekend
EARLY = "early"  # before BRIEF_TIME
MISSED = "missed"  # past the catch-up cutoff: skipped for today


# ---------------------------------------------------------------- when


def _instant(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc)


def brief_due(schedule: config.BriefSchedule, now: datetime, last_brief_date: str | None) -> str:
    """Whether the scheduled briefing should go out at ``now`` (an aware datetime).

    Due when the local day (in the schedule's TIMEZONE) is an allowed day,
    the local time is at or after BRIEF_TIME and before the catch-up cutoff,
    and today's date is not ``last_brief_date``. Wall-clock times are turned
    into instants in that zone and compared as instants, so the check reads
    the clock afresh on every call and survives the Mac having been asleep.
    """
    if not schedule.enabled or schedule.at is None:
        return OFF
    tz = schedule.timezone
    local = now.astimezone(tz)
    today = local.date()
    if last_brief_date == today.isoformat():
        return ALREADY
    if schedule.days == config.BRIEF_DAYS_WEEKDAYS and today.weekday() >= 5:
        return DAY_OFF
    if _instant(local) < _instant(datetime.combine(today, schedule.at, tzinfo=tz)):
        return EARLY
    if schedule.catchup_until is not None and _instant(local) >= _instant(
        datetime.combine(today, schedule.catchup_until, tzinfo=tz)
    ):
        return MISSED
    return DUE


# ---------------------------------------------------------------- time of day

# A briefing asked for by hand from this hour until midnight also covers tomorrow's schedule.
TOMORROW_FROM_HOUR = 17


def time_of_day(now: datetime) -> str:
    """The time of day of a local wall-clock time: 아침 05:00–10:59, 오후 11:00–16:59, 저녁 17:00–20:59, 밤 21:00–04:59."""
    if 5 <= now.hour < 11:
        return MORNING
    if 11 <= now.hour < 17:
        return AFTERNOON
    if 17 <= now.hour < 21:
        return EVENING
    return NIGHT


@dataclass(frozen=True)
class BriefTime:
    """When one relay briefing runs: the local time, whether it is the scheduled one, and its time of day.

    Made once per relay from the injected clock (``BriefTime.at``) and passed
    to every part. The scheduled briefing (``BRIEF_TIME``) is always 아침,
    even a late catch-up; a manual one (Slack request, ``--brief``,
    ``--brief --slack``) follows the clock (``time_of_day``).
    """

    now: datetime  # in TIMEZONE
    scheduled: bool
    period: str  # 아침, 오후, 저녁 or 밤

    @classmethod
    def at(cls, now: datetime, *, scheduled: bool = False) -> "BriefTime":
        return cls(now, scheduled, MORNING if scheduled else time_of_day(now))

    @property
    def clock(self) -> str:
        """``17:50``."""
        return f"{self.now:%H:%M}"

    @property
    def covers_tomorrow(self) -> bool:
        """A manual briefing from 17:00 to 23:59: 일정 adds tomorrow's schedule, 고뭉치 tomorrow's weather."""
        return not self.scheduled and self.now.hour >= TOMORROW_FROM_HOUR

    @property
    def schedule_days(self) -> int:
        """일정's ``days``: 2 (today and tomorrow) for a manual briefing from 17:00 to 23:59, else 1 (today)."""
        return 2 if self.covers_tomorrow else 1

    @property
    def tomorrow(self) -> date:
        """The local date after ``now``'s."""
        return self.now.date() + timedelta(days=1)


# ---------------------------------------------------------------- weather and credits (code, no LLM)


def crash_kind(exc: BaseException) -> str:
    return "시간 초과" if isinstance(exc, asyncio.TimeoutError) else type(exc).__name__


def credit_section(
    env: Mapping[str, str] | None = None,
    *,
    fetch: CreditFetch | None = None,
    now: datetime | None = None,
    slack: bool = False,
) -> str:
    """The credit summary (``credits.summary_text``), or a one-line Korean note. Never raises.

    Only the gateway's credit endpoints are called, never a model. Blocking:
    run it in a worker thread from async code.
    """
    label = "*Chat KHU 크레딧*" if slack else "Chat KHU 크레딧"
    try:
        report = (fetch or (lambda: credits.fetch_report(env)))()
        if not report.supported:
            return f"💳 {label}: {CREDIT_UNSUPPORTED_NOTE}"
        if not report.ok or report.balance is None:
            lines = (report.error or "").strip().splitlines()
            reason = " ".join((lines[0] if lines else "응답 없음").split())
            match = _CREDIT_ERROR_RE.match(reason)
            reason = match.group(1) if match else reason.rstrip(".")
            return f"💳 {label}: " + CREDIT_FAILED_NOTE.format(reason=scrub(reason)[:200])
        return credits.summary_text(report, env=env, now=now, slack=slack)
    except Exception as exc:  # noqa: BLE001 - the briefing goes out anyway
        return f"💳 {label}: " + CREDIT_FAILED_NOTE.format(reason=type(exc).__name__)


def weather_section(
    env: Mapping[str, str] | None = None,
    *,
    fetch: WeatherFetch | None = None,
    slack: bool = False,
    tomorrow: date | None = None,
) -> tuple[str, str]:
    """``(today's line, tomorrow's line)``. Never raises.

    Today's is ``weather.report_line`` or the short failure note. Tomorrow's
    (``weather.tomorrow_line``, labelled with ``tomorrow``) only when
    ``tomorrow`` is given (a manual briefing from 17:00 to 23:59) and the
    forecast has that day; else "" (no note). Only Open-Meteo is called,
    never a model, in one fetch for both. Blocking: run it in a worker
    thread from async code.
    """
    label = config.DEFAULT_WEATHER_LABEL
    try:
        if fetch is None:
            cfg = weather.load_config(env)  # logs a warning for unusable coordinates
            label = cfg.label
            report = weather.fetch_report(cfg, tomorrow=tomorrow)
        else:
            report = fetch()
            label = report.label
        if not report.ok:
            weather.log.warning("브리핑의 날씨를 가져오지 못했습니다: %s", scrub(report.error or "응답 없음"))
        today = weather.report_line(report, slack=slack)
    except Exception as exc:  # noqa: BLE001 - the briefing goes out anyway
        weather.log.warning("브리핑의 날씨를 가져오지 못했습니다: %s", type(exc).__name__)
        return weather.failed_line(label, slack=slack), ""
    later = ""
    if tomorrow is not None and report.ok:
        try:
            later = weather.tomorrow_line(tomorrow, report.tomorrow, report.tomorrow_air, slack=slack)
        except Exception as exc:  # noqa: BLE001 - today's line goes out anyway
            weather.log.warning("브리핑의 내일 날씨를 만들지 못했습니다: %s", type(exc).__name__)
        if not later:
            weather.log.info("내일 날씨가 응답에 없어 브리핑에서 내일 날씨 줄을 뺍니다.")
    return today, later


# ---------------------------------------------------------------- 고뭉치's greeting


def date_fields(day: date | datetime) -> dict[str, str]:
    """``date`` "2026년 10월 8일(목)", ``short`` "10월 8일(목)", ``md`` "10월 8일", ``wd`` "목요일"."""
    weekday = WEEKDAYS_KO[day.weekday()]
    md = f"{day.month}월 {day.day}일"
    return {"date": f"{day.year}년 {md}({weekday})", "short": f"{md}({weekday})", "md": md, "wd": f"{weekday}요일"}


# Constant (no date, no time, no time of day): the time of day and the clock go in the user prompt.
GREETING_SYSTEM_PROMPT = (
    "너는 한 연구자의 비서실장 '고뭉치'다. 브리핑을 여는 인사만 쓴다. 날씨 줄과 Chat KHU 크레딧은 프로그램이, "
    "Dropbox 소식과 오늘 일정은 팀원 업뎃과 일정이 따로 전한다.\n\n"
    "## 말투\n" + VOICES[MUNGCHI] + "\n\n"
    "## 규칙\n"
    "- 인사만 1~2문장으로 짧게 쓴다(80자 안팎). 따옴표, 목록, 제목, 설명은 붙이지 않는다.\n"
    '- 사용자 메시지에 있는 오늘 날짜를 "M월 D일(요일)" 꼴로 꼭 넣는다. 다른 날짜, 연도, 요일은 쓰지 않는다.\n'
    "- 숫자는 날짜에만 쓴다. 기온, 강수확률 같은 숫자는 쓰지 않는다.\n"
    "- 날씨는 주어진 요약에 맞을 때만 가볍게 한마디 해도 된다. 요약에 없는 날씨는 말하지 않는다.\n"
    "- 일정, 파일, 크레딧 이야기는 하지 않고, 아무도 멘션하지 않는다.\n"
    "- 절기, 공휴일, 기념일 같은 달력 이야기는 하지 않는다.\n"
    "- 최근 인사들이 주어지면 그 인사들과 여는 말(첫 마디)도, 문장 구조도 겹치지 않게 쓰고 같은 표현을 되풀이하지 않는다.\n"
    "- '똑똑'은 아주 가끔만 쓴다. 사용자 메시지에서 '똑똑'을 쓰지 말라고 하면 쓰지 않는다.\n"
    f"- {ACCURACY_RULE}\n"
)

# The knock 고뭉치 may open with now and then: only when none of the recent greetings (the last seven) has it.
KNOCK = "똑똑"


def uses_knock(text: str) -> bool:
    """True when ``text`` says "똑똑" (spaces ignored: "똑 똑" too)."""
    return KNOCK in "".join((text or "").split())


def weather_words(line: str) -> str:
    """The weather line without its label and without any number: ``대체로 맑음 · 미세먼지 보통``."""
    text = re.sub(r"[*_]", "", line or "")
    if ":" in text:
        text = text.split(":", 1)[1]
    parts = [part.strip() for part in text.split("·")]
    return " · ".join(part for part in parts if part and not re.search(r"\d", part) and weather.FAILED_NOTE not in part)


# What a greeting for each time of day may sound like (in the user prompt only).
GREETING_EXAMPLES = {
    MORNING: '"좋은 아침이에요", "아침 브리핑입니다"',
    AFTERNOON: '"오후 브리핑 시작할게요", "오후도 힘내요"',
    EVENING: '"저녁 브리핑이에요~", "오늘 하루도 수고 많으셨어요"',
    NIGHT: '"늦은 시간까지 수고 많으세요", "브리핑 짧게 전할게요"',
}

# Words that name a time of day: a greeting may use only its own time's words.
TIME_WORDS = {MORNING: ("아침", "모닝"), AFTERNOON: ("오후",), EVENING: ("저녁",), NIGHT: ("밤",)}


def other_time_words(text: str, period: str) -> list[str]:
    """The words in ``text`` that name a time of day other than ``period`` ("좋은 아침" in the evening, ...)."""
    return [word for other, words in TIME_WORDS.items() if other != period for word in words if word in text]


def greeting_prompt(
    now: datetime, *, period: str | None = None, weather_text: str = "", recent: Sequence[str] = ()
) -> str:
    """The greeting's user prompt: the time of day and the time, today's date (from code), a weekday note,
    the weather in words, the recent greetings (oldest first) and whether "똑똑" may be used.
    ``period`` defaults to the clock's time of day."""
    period = period or time_of_day(now)
    fields = date_fields(now)
    if now.weekday() >= 5:
        day_note = f"오늘은 주말({fields['wd']})이에요."
    elif now.weekday() == 0:
        day_note = "오늘은 한 주를 시작하는 월요일이에요."
    else:
        day_note = "오늘은 평일이에요."
    others = "·".join(f"'{other}'" for other in TIMES_OF_DAY if other != period)
    lines = [
        f"{period} 브리핑을 여는 인사를 1~2문장으로 써 줘.",
        f"- 지금: {period} 브리핑 ({now:%H:%M}). {period}에 맞는 인사로 써 줘(예: {GREETING_EXAMPLES[period]}). 시각은 쓰지 마.",
        f"- {others} 같은 다른 때를 가리키는 말은 쓰지 마.",
    ]
    if period == NIGHT and now.hour < 5:
        lines.append("- 자정을 넘긴 늦은 밤이에요. 날짜 바로 뒤에 '밤'을 붙이지 마(그날 밤으로 읽혀요).")
    lines += [
        f"- 오늘 날짜: {now.year}년 {fields['md']} {fields['wd']} (짧게 쓰면 {fields['short']})",
        f"- {day_note}",
        f"- 날씨 요약: {weather_words(weather_text) or '없음'}",
    ]
    recent = [text for text in recent if text and text.strip()]
    if recent:
        lines.append(f"- 최근 인사 {len(recent)}개(오래된 것부터). 이 인사들과 여는 말(첫 마디)이나 문장 구조가 겹치지 않게 써 줘:")
        lines += [f'  {number}. "{text}"' for number, text in enumerate(recent, 1)]
    if any(uses_knock(text) for text in recent):
        lines.append(f"- 최근 인사에 '{KNOCK}'이 이미 있으니 이번에는 '{KNOCK}'을 쓰지 마.")
    else:
        lines.append(f"- '{KNOCK}'은 아주 가끔만 쓰는 말이라 굳이 쓰지 않아도 돼.")
    return "\n".join(lines)


_MONTH_DAY_RE = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_SLASH_DATE_RE = re.compile(r"(?<![\d.])(\d{1,2})\s*[/.]\s*(\d{1,2})(?![\d.])")
_YEAR_RE = re.compile(r"(\d{4})\s*년")
_WEEKDAY_RE = re.compile(r"([월화수목금토일])요일|\(([월화수목금토일])\)")
_QUOTES = "\"'“”‘’「」"


def clean_greeting(text: str | None) -> str:
    """The model's answer trimmed: outer quotes, extra spaces and blank lines removed."""
    lines = [" ".join(line.split()) for line in (text or "").strip().strip(_QUOTES).splitlines()]
    return "\n".join(line.strip(_QUOTES).strip() for line in lines if line.strip())


def valid_greeting(text: str, now: datetime, period: str | None = None, *, allow_knock: bool = True) -> bool:
    """True when ``text`` is a short greeting with today's date and nothing that could be wrong.

    Today's month and day must appear ("10월 8일", or "10/8"); any other date,
    year or weekday, any other number, a word of another time of day than
    ``period`` (default: the clock's; e.g. "좋은 아침" in the evening), Slack
    markup or mention, more than two lines or more than ``MAX_GREETING_CHARS``
    characters makes it invalid, and so does "똑똑" unless ``allow_knock``.
    """
    if not text or len(text) > MAX_GREETING_CHARS or text.count("\n") > 1:
        return False
    if not allow_knock and uses_knock(text):
        return False
    if any(char in text for char in "<>@*#`|[]_"):
        return False
    if other_time_words(text, period or time_of_day(now)):
        return False
    if sum(1 for char in text if unicodedata.category(char) == "So") > 3:
        return False
    dates = [(int(m), int(d)) for m, d in _MONTH_DAY_RE.findall(text)]
    dates += [(int(m), int(d)) for m, d in _SLASH_DATE_RE.findall(text)]
    if not dates or any(found != (now.month, now.day) for found in dates):
        return False
    if any(int(year) != now.year for year in _YEAR_RE.findall(text)):
        return False
    weekday = WEEKDAYS_KO[now.weekday()]
    if any((full or short) != weekday for full, short in _WEEKDAY_RE.findall(text)):
        return False
    rest = _YEAR_RE.sub("", _SLASH_DATE_RE.sub("", _MONTH_DAY_RE.sub("", text)))
    return not re.search(r"\d", rest)


def greeting_templates(now: date | datetime, period: str = MORNING) -> list[str]:
    """The fallback templates for ``period`` on ``now``'s day (weekend and Monday ones added on those days)."""
    pool = list(phrases.GREETING_TEMPLATES[period])
    if now.weekday() >= 5:
        pool += phrases.WEEKEND_GREETING_TEMPLATES.get(period, ())
    if now.weekday() == 0:
        pool += phrases.MONDAY_GREETING_TEMPLATES.get(period, ())
    return pool


def greeting_opening(text: str) -> str:
    """A greeting's opening: its first word without punctuation or emoji, digits as 0, a weekday as "0요일".

    "똑똑! 🚪 ..." -> "똑똑", "좋은 아침이에요!" -> "좋은", "10월 8일(목) ..." -> "00월",
    "목요일 아침이에요" -> "0요일" (every greeting that starts with the date, or
    with the weekday, has the same opening).
    """
    for word in unicodedata.normalize("NFC", text or "").split():
        letters = "".join(char for char in word if unicodedata.category(char)[0] in "LN")
        if letters:
            return re.sub(r"^[월화수목금토일]요일", "0요일", re.sub(r"\d", "0", letters))
    return ""


def fallback_greeting(
    now: datetime,
    *,
    period: str | None = None,
    rng: random.Random | None = None,
    recent: Sequence[str] = (),
) -> str:
    """A template greeting for ``now`` and ``period`` (default: the clock's), the date filled in by code.

    ``recent``: the recent greetings (oldest first). No "똑똑" template when
    one of them has it; then a template whose opening none of them used (or,
    when every opening was used, the one used longest ago), never a recent
    greeting again when there is another one. ``rng`` makes it repeatable.
    """
    fields = date_fields(now)
    pool = [template.format(**fields) for template in greeting_templates(now, period or time_of_day(now))]
    recent = [text for text in recent if text]
    if any(uses_knock(text) for text in recent):
        pool = [text for text in pool if not uses_knock(text)] or pool
    pool = [text for text in pool if text not in recent] or pool
    openings = [greeting_opening(text) for text in recent]

    def last_used(text: str) -> int:
        """Where the text's opening was last used in ``recent`` (-1: not at all)."""
        opening = greeting_opening(text)
        return max((index for index, used in enumerate(openings) if used == opening), default=-1)

    oldest = min(last_used(text) for text in pool)
    return (rng or random).choice([text for text in pool if last_used(text) == oldest])


async def llm_greeting(prompt: str, env: Mapping[str, str] | None = None) -> str:
    """The real greeting call: one tool-less turn with ``GREETING_SYSTEM_PROMPT``. Raises when the turn fails."""
    result = await run_plain_turn(prompt, system_prompt=GREETING_SYSTEM_PROMPT, env=env)
    if result.failed:
        raise RuntimeError(result.error or "인사를 만들지 못했습니다")
    return result.text


# What ``make_greeting`` calls when no generator is given (tests replace it).
generate_greeting: Callable[[str, Mapping[str, str] | None], Awaitable[str]] = llm_greeting


@dataclass
class Greeting:
    text: str
    source: str  # "llm" or "template"


async def make_greeting(
    when: BriefTime,
    *,
    weather_text: str = "",
    env: Mapping[str, str] | None = None,
    store: StateStore | None = None,
    generate: GreetingGenerate | None = None,
    rng: random.Random | None = None,
    timeout: float = GREETING_TIMEOUT_SECONDS,
) -> Greeting:
    """고뭉치's greeting for ``when``: the model's when it is valid and new, else a template. Never raises.

    The time of day and the time go into the prompt with the recent
    greetings (state file, the last seven), whose openings every run is
    told not to repeat; "똑똑" is refused while one of them has it. Only the
    scheduled briefing stores the one it used: a manual evening briefing
    never adds to the list.
    """
    now, period = when.now, when.period
    store = store or StateStore(config.get_state_path(env))
    try:
        recent = [text for _day, text in store.recent_greetings()]
    except OSError:
        recent = []
    allow_knock = not any(uses_knock(text) for text in recent)
    prompt = greeting_prompt(now, period=period, weather_text=weather_text, recent=recent)
    greeting: Greeting | None = None
    try:
        call = generate(prompt) if generate is not None else generate_greeting(prompt, env)
        text = clean_greeting(await asyncio.wait_for(call, timeout))
        if valid_greeting(text, now, period, allow_knock=allow_knock) and text not in recent:
            greeting = Greeting(text, "llm")
        else:
            log.info("%s 인사가 형식에 맞지 않아(날짜·시간대·'똑똑'·최근 인사 확인 등) 준비된 인사를 씁니다.", period)
    except Exception as exc:  # noqa: BLE001 - a template greeting is always fine
        reason = crash_kind(exc) if isinstance(exc, asyncio.TimeoutError) else safe_error(exc)
        log.warning("%s 인사를 만들지 못해 준비된 인사를 씁니다: %s", period, reason)
    if greeting is None:
        greeting = Greeting(fallback_greeting(now, period=period, rng=rng, recent=recent), "template")
    if when.scheduled:
        try:
            store.mark_greeting(now.date().isoformat(), greeting.text)
        except OSError as exc:
            log.warning("아침 인사를 상태 파일에 기록하지 못했습니다: %s", safe_error(exc))
    return greeting


# ---------------------------------------------------------------- 업뎃's and 일정's reports


# 일정's opening line, per time of day.
SCHEDULE_LEADS = {MORNING: "밝은 아침 한마디", AFTERNOON: "밝은 오후 한마디", EVENING: "밝은 저녁 한마디", NIGHT: "차분한 밤 한마디"}
# Outside 아침: no morning wording (일정's evening prompt says it in its own words).
NOT_MORNING_RULE = "- 지금은 {period} 브리핑이니 여는 말도 그때에 맞게 쓰고, '좋은 아침'이나 '아침 보고'처럼 아침을 가리키는 말은 쓰지 마.\n"


def _korean_day(day: date) -> str:
    """``2026-10-08 (목요일)``."""
    return f"{day.isoformat()} ({WEEKDAYS_KO[day.weekday()]}요일)"


def _day_heading(day: date, slack: bool) -> str:
    """``10/08 (목)``, bold in Slack: ``*10/08 (목)*``."""
    text = f"{day:%m/%d} ({WEEKDAYS_KO[day.weekday()]})"
    return f"*{text}*" if slack else text


def _report_opening(when: BriefTime, others: str) -> str:
    return (
        f"{when.period} 브리핑에서 네가 맡은 부분을 보고할 차례야(지금 {when.clock}). "
        f"고뭉치가 인사와 날씨, Chat KHU 크레딧을 이미 전했고, {others}\n"
    )


def _update_report_prompt(when: BriefTime) -> str:
    return (
        _report_opening(when, "일정 보고는 일정이 따로 해.")
        + "- check_dropbox_updates를 since_hours 없이(0) 한 번만 불러 Dropbox 업데이트를 확인해. "
        "이번 실행은 브리핑이라 도구가 지난 브리핑 이후를 본다. 기간은 결과의 since_basis대로 써.\n"
        f'- 업뎃다운 짧은 {when.period} 인사 한 줄(예: "업뎃 보고드립니다! Dropbox 업데이트 전해드려요 📂")로 시작하고, '
        "그다음은 네 형식(하위 폴더, 링크, "
        "사람별 파일과 수정 시각)과 규칙을 그대로 따라. 변경이 없으면 그 한 줄이면 돼.\n"
        + ("" if when.period == MORNING else NOT_MORNING_RULE.format(period=when.period))
        + "- 날씨, 크레딧, 일정은 쓰지 말고, 다른 팀원을 부르거나 멘션하지 마. 짧게."
    )


def _schedule_report_prompt(when: BriefTime, slack: bool) -> str:
    today = when.now.date()
    iso, lead = today.isoformat(), SCHEDULE_LEADS[when.period]
    opening = _report_opening(when, "Dropbox 소식은 업뎃이 따로 보고해.")
    closing = "- 날씨, 크레딧, Dropbox는 쓰지 말고, 다른 팀원을 부르거나 멘션하지 마. 짧게."
    if when.schedule_days == 1:
        return (
            opening
            + f'- get_schedule을 date="{iso}", days=1로 한 번만 불러 오늘({_korean_day(today)}) 하루치 일정만 확인해. get_weather는 부르지 마.\n'
            f"- 일정다운 {lead}로 시작하고, 지금 / 바로 다음 일정을 맨 앞에, 이어서 오늘 일정을 네 형식대로 시간 순으로 써. "
            '겹침과 쓸모 있는 빈 시간(1~3개)도 형식대로. 오늘 일정이 없으면 "오늘 일정 없음"이라고 써.\n'
            + ("" if when.period == MORNING else NOT_MORNING_RULE.format(period=when.period))
            + closing
        )
    # A manual briefing from 17:00 to midnight: the rest of today, then tomorrow (both dates from code).
    tomorrow = today + timedelta(days=1)
    return (
        opening
        + f'- get_schedule을 date="{iso}", days=2로 한 번만 불러 오늘({_korean_day(today)})의 남은 일정과 '
        f"내일({_korean_day(tomorrow)}) 일정을 확인해. get_weather는 부르지 마.\n"
        f"- 일정다운 {lead}로 시작하고, 지금 / 바로 다음 일정을 맨 앞에 써(바로 다음 일정이 내일이면 날짜도 함께).\n"
        f"- 이어서 날짜 제목 줄({_day_heading(today, slack)}, {_day_heading(tomorrow, slack)})을 두고 날짜별로 네 형식대로 써. "
        '오늘은 지금 진행 중이거나 아직 시작하지 않은 일정만 쓰고(이미 끝난 일정은 빼고), 없으면 "오늘 남은 일정 없음"이라고 써. '
        '내일은 하루치 일정을 시간 순으로 쓰고, 없으면 "내일 일정 없음"이라고 써. '
        "겹침과 쓸모 있는 빈 시간(1~3개, 이미 지난 시간은 빼고)도 형식대로.\n"
        "- 오늘 하루는 거의 지나갔어. '좋은 아침'이나 '아침 보고', '여유로운 하루예요'처럼 하루를 앞두고 하는 말은 쓰지 말고, "
        "남은 일정과 내일 준비에 어울리는 말로 써.\n"
        "- 내일 일정도 결과에 있는 것만 쓰고, 결과에 없는 공휴일·절기는 덧붙이지 마.\n"
        + closing
    )


def report_prompt(persona: str, when: BriefTime, *, slack: bool = False) -> str:
    """업뎃's or 일정's user prompt for its part: the time of day, the time and the dates from code (no weather, no credits)."""
    if persona == UPDATE:
        return _update_report_prompt(when)
    if persona == SCHEDULE:
        return _schedule_report_prompt(when, slack)
    raise ValueError(f"no report for persona {persona!r}")


@dataclass
class Report:
    """업뎃's or 일정's part: the text to post (its report, or a short apology) and the run behind it."""

    persona: str
    text: str
    result: TurnResult | None = None
    crash: BaseException | None = None

    @property
    def failed(self) -> bool:
        return self.result is None or self.result.failed or not (self.result.text or "").strip()

    @property
    def session_id(self) -> str | None:
        return self.result.session_id if self.result is not None else None


def failure_reason(result: TurnResult | None, crash: BaseException | None) -> str:
    """A short, scrubbed reason for an apology line."""
    if crash is not None:
        return crash_kind(crash)
    if result is None:
        return "결과 없음"
    if result.failed:
        lines = scrub(result.error or "").strip().splitlines()
        return (lines[0] if lines else "응답을 마치지 못했어요")[:MAX_REASON_CHARS]
    return "빈 응답"


def report_text(
    persona: str, result: TurnResult | None, crash: BaseException | None = None, *, rng: random.Random | None = None
) -> str:
    """The report as written, plus a short note when the run failed after writing it; else an apology in the bot's voice."""
    text = (result.text or "").strip() if result is not None else ""
    if crash is None and text:
        if result is not None and result.failed:
            reason = scrub(result.error or "")[:MAX_ERROR_CHARS]
            return f"{text}\n\n⚠️ {reason}" if reason else text
        return text
    return phrases.pick(phrases.APOLOGY_TEMPLATES[persona], rng).format(reason=failure_reason(result, crash))


async def run_report(
    persona: str,
    *,
    run: RunTurn,
    when: BriefTime,
    slack: bool,
    run_timeout: float | None = None,
    on_status: Callable[[str], Any] | None = None,
    rng: random.Random | None = None,
) -> Report:
    """One direct 업뎃 / 일정 run for the relay briefing. Never raises (except when cancelled).

    업뎃 runs in briefing mode (its Dropbox checkpoint moves); 일정 looks at
    today (and tomorrow for a manual briefing from 17:00, ``when.schedule_days``).
    """
    label = PERSONA_LABELS[persona]
    result: TurnResult | None = None
    crash: BaseException | None = None
    try:
        turn = run(
            report_prompt(persona, when, slack=slack),
            on_status=on_status,
            extra_system_prompt=SLACK_FORMAT_PROMPT if slack else "",
            persona=persona,
            briefing=persona == UPDATE,
        )
        result = await (asyncio.wait_for(turn, run_timeout) if run_timeout else turn)
    except Exception as exc:  # noqa: BLE001 - reported as a short apology, details in the log
        crash = exc
        reason = crash_kind(exc) if isinstance(exc, asyncio.TimeoutError) else safe_error(exc)
        log.error("%s의 %s 보고를 만들지 못했습니다: %s", label, when.period, reason)
    else:
        if result.failed:
            log.warning("%s의 %s 보고가 완전하지 않습니다: %s", label, when.period, scrub(result.error or ""))
    return Report(persona, report_text(persona, result, crash, rng=rng), result, crash)


# ---------------------------------------------------------------- the relay


@dataclass
class Head:
    """고뭉치's part before its closing lines: the greeting, the weather line ("" when off), the credits,
    and tomorrow's weather line (a manual briefing from 17:00 to 23:59 only, else "")."""

    greeting: Greeting
    weather: str
    credits: str
    weather_tomorrow: str = ""

    def text(self, closing: Sequence[str] = ()) -> str:
        return compose_head(self.greeting.text, self.weather, self.credits, closing, weather_tomorrow=self.weather_tomorrow)


def compose_head(
    greeting: str, weather_text: str, credit_text: str, closing: Sequence[str] = (), *, weather_tomorrow: str = ""
) -> str:
    """Greeting, then the weather lines (today, tomorrow) and the credits together, then the closing lines (notes, hand-off)."""
    data = "\n".join(
        part.strip() for part in (weather_text, weather_tomorrow, credit_text) if part and part.strip()
    )
    tail = "\n".join(line.strip() for line in closing if line and line.strip())
    return "\n\n".join(part for part in (greeting.strip(), data, tail) if part)


async def _build_head(
    when: BriefTime,
    *,
    env: Mapping[str, str] | None,
    slack: bool,
    credit_fetch: CreditFetch | None,
    weather_fetch: WeatherFetch | None,
    greeting_generate: GreetingGenerate | None,
    store: StateStore | None,
    rng: random.Random | None,
    greeting_timeout: float,
) -> Head:
    credit_task = asyncio.ensure_future(
        asyncio.to_thread(credit_section, env, fetch=credit_fetch, now=when.now, slack=slack)
    )
    try:
        weather_text, weather_tomorrow = (
            await asyncio.to_thread(
                weather_section,
                env,
                fetch=weather_fetch,
                slack=slack,
                tomorrow=when.tomorrow if when.covers_tomorrow else None,
            )
            if config.get_brief_weather(env)
            else ("", "")
        )
        greeting = await make_greeting(
            when,
            weather_text=weather_text,
            env=env,
            store=store,
            generate=greeting_generate,
            rng=rng,
            timeout=greeting_timeout,
        )
        credit_text = await credit_task
    finally:
        credit_task.cancel()  # a no-op once done
    return Head(greeting, weather_text, credit_text, weather_tomorrow)


class Relay:
    """The running parts of one relay briefing, awaited in posting order: ``head()``, then ``report(persona)``.

    Use it as ``async with start_relay(...) as relay``: leaving the block
    cancels whatever is still running (e.g. when the bots stop).
    """

    def __init__(self, head: asyncio.Future[Head], reports: dict[str, asyncio.Future[Report]], when: BriefTime):
        self._head = head
        self._reports = reports
        self.when = when  # the time of day for the callers' own lines (the hand-off)

    @property
    def now(self) -> datetime:
        return self.when.now

    @property
    def personas(self) -> list[str]:
        return list(self._reports)

    async def head(self) -> Head:
        return await self._head

    async def report(self, persona: str) -> Report:
        return await self._reports[persona]

    async def aclose(self) -> None:
        tasks = [self._head, *self._reports.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def __aenter__(self) -> "Relay":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()


def start_relay(
    *,
    personas: Sequence[str] = REPORTERS,
    run: RunTurn | None = None,
    now: datetime | None = None,
    scheduled: bool = False,
    env: Mapping[str, str] | None = None,
    slack: bool = False,
    credit_fetch: CreditFetch | None = None,
    weather_fetch: WeatherFetch | None = None,
    greeting_generate: GreetingGenerate | None = None,
    store: StateStore | None = None,
    rng: random.Random | None = None,
    run_timeout: float | None = None,
    greeting_timeout: float = GREETING_TIMEOUT_SECONDS,
    on_status: Callable[[str], Any] | None = None,
) -> Relay:
    """Start every part of today's relay briefing at once (call it inside a running event loop).

    ``personas``: who reports after 고뭉치 (업뎃, 일정; a bot that is not set
    up is left out by the caller). ``scheduled``: the ``BRIEF_TIME`` briefing
    (always 아침); otherwise the wording follows ``now``'s time of day
    (``BriefTime``). ``slack`` picks Slack formatting. ``run_timeout``
    (seconds) bounds each report run.
    """
    tz = config.get_timezone(env)
    when = BriefTime.at((now or datetime.now(tz)).astimezone(tz), scheduled=scheduled)
    run = run or run_turn
    rng = rng or random.Random()
    reports = {
        persona: asyncio.ensure_future(
            run_report(persona, run=run, when=when, slack=slack, run_timeout=run_timeout, on_status=on_status, rng=rng)
        )
        for persona in personas
    }
    head = asyncio.ensure_future(
        _build_head(
            when,
            env=env,
            slack=slack,
            credit_fetch=credit_fetch,
            weather_fetch=weather_fetch,
            greeting_generate=greeting_generate,
            store=store,
            rng=rng,
            greeting_timeout=greeting_timeout,
        )
    )
    return Relay(head, reports, when)


# ---------------------------------------------------------------- terminal


def run_brief_cli(
    env: Mapping[str, str] | None = None,
    *,
    run: RunTurn | None = None,
    now: datetime | None = None,
    credit_fetch: CreditFetch | None = None,
    weather_fetch: WeatherFetch | None = None,
    greeting_generate: GreetingGenerate | None = None,
    store: StateStore | None = None,
    rng: random.Random | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """``python -m mungchi --brief``: the three parts on stdout under ``[고뭉치]``, ``[업뎃]``, ``[일정]``.

    A manual briefing: its wording follows the time of day. Progress and
    errors go to stderr. Exits 1 when 업뎃's or 일정's part could not be made.
    """
    out = out or sys.stdout
    err = err or sys.stderr
    rng = rng or random.Random()

    def status(line: str) -> None:
        print(line, file=err, flush=True)

    def show(persona: str, text: str) -> None:
        print(f"[{PERSONA_LABELS[persona]}]\n{scrub(text).strip()}\n", file=out, flush=True)

    async def relay() -> int:
        failed = False
        async with start_relay(
            run=run,
            now=now,
            env=env,
            slack=False,
            credit_fetch=credit_fetch,
            weather_fetch=weather_fetch,
            greeting_generate=greeting_generate,
            store=store,
            rng=rng,
            on_status=status,
        ) as parts:
            head = await parts.head()
            handoff = phrases.pick(phrases.HANDOFF_TEMPLATES[parts.when.period], rng).format(
                bots=phrases.teammate_names(REPORTERS)
            )
            show(MUNGCHI, head.text([handoff]))
            for persona in REPORTERS:
                report = await parts.report(persona)
                if report.crash is not None:
                    label = PERSONA_LABELS[persona]
                    print(f"[오류] {josa(label, '을', '를')} 실행하지 못했습니다: {safe_error(report.crash)}", file=err)
                failed = failed or report.failed
                show(persona, report.text)
        return 1 if failed else 0

    return asyncio.run(relay())
