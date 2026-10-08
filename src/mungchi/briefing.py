"""The morning briefing as a relay: 고뭉치 greets and hands off, 업뎃 and 일정 report their own parts.

``python -m mungchi --brief`` (terminal), ``--brief --slack``, the scheduled
morning briefing of the running Slack bots (``BRIEF_TIME``) and a short
briefing request to 고뭉치 in Slack ("오늘 건너뛴 브리핑 좀 해봐", or a bare
``@고뭉치``) all run the same relay (``start_relay``), in this order:

    고뭉치   똑똑! 🚪 2026년 10월 8일(목) 아침 브리핑입니다~      ← one small LLM call (template if it fails)
             🌤️ 서울 날씨: 대체로 맑음 · ...                     ← code (Open-Meteo), no LLM; BRIEF_WEATHER=off drops it
             💳 Chat KHU 크레딧: ...                              ← code, no LLM
             @업뎃 @일정 아침 보고 부탁해요!                       ← hand-off, picked by code
    업뎃     its own report: Dropbox since the last briefing    ← direct 업뎃 run in briefing mode
    일정     its own report: today's schedule (days=1)          ← direct 일정 run

Everything starts at once: the weather and the credits in worker threads,
고뭉치's greeting (it needs the weather first), and the 업뎃 and 일정 runs.
The callers deliver the parts in that order as each one is ready; weather
and credits only ever appear in 고뭉치's part.

* **Greeting**: one tool-less turn with a tiny constant system prompt
  (``GREETING_SYSTEM_PROMPT``). Its user prompt holds today's date, a
  weekday/weekend note, the weather in words (numbers removed) and the last
  greeting, which the model is told not to repeat. The answer is checked
  (``valid_greeting``: today's month/day present, no other date, year or
  weekday, no other numbers, short); a failure, a timeout (30 s) or an
  invalid answer falls back to a template (``phrases.GREETING_TEMPLATES``).
  The greeting used is stored in the state file (``last_greeting``).
* **업뎃**: ``briefing=True``, so its Dropbox tool looks at the time since the
  last briefing and moves that checkpoint. **일정**: today only.
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
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Sequence, TextIO

from . import config, credits, phrases, weather
from .agents import ACCURACY_RULE, VOICES
from .main import TurnResult, run_plain_turn, run_turn
from .personas import MUNGCHI, PERSONA_LABELS, SCHEDULE, UPDATE, josa
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
) -> str:
    """Today's weather line (``weather.report_line``), or the short failure note. Never raises.

    Only Open-Meteo is called, never a model. Blocking: run it in a worker
    thread from async code.
    """
    label = config.DEFAULT_WEATHER_LABEL
    try:
        if fetch is None:
            cfg = weather.load_config(env)  # logs a warning for unusable coordinates
            label = cfg.label
            report = weather.fetch_report(cfg)
        else:
            report = fetch()
            label = report.label
        if not report.ok:
            weather.log.warning("브리핑의 날씨를 가져오지 못했습니다: %s", scrub(report.error or "응답 없음"))
        return weather.report_line(report, slack=slack)
    except Exception as exc:  # noqa: BLE001 - the briefing goes out anyway
        weather.log.warning("브리핑의 날씨를 가져오지 못했습니다: %s", type(exc).__name__)
        return weather.failed_line(label, slack=slack)


# ---------------------------------------------------------------- 고뭉치's greeting


def date_fields(day: date | datetime) -> dict[str, str]:
    """``date`` "2026년 10월 8일(목)", ``short`` "10월 8일(목)", ``md`` "10월 8일", ``wd`` "목요일"."""
    weekday = WEEKDAYS_KO[day.weekday()]
    md = f"{day.month}월 {day.day}일"
    return {"date": f"{day.year}년 {md}({weekday})", "short": f"{md}({weekday})", "md": md, "wd": f"{weekday}요일"}


GREETING_SYSTEM_PROMPT = (
    "너는 한 연구자의 비서실장 '고뭉치'다. 아침 브리핑을 여는 인사만 쓴다. 날씨 줄과 Chat KHU 크레딧은 프로그램이, "
    "Dropbox 소식과 오늘 일정은 팀원 업뎃과 일정이 따로 전한다.\n\n"
    "## 말투\n" + VOICES[MUNGCHI] + "\n\n"
    "## 규칙\n"
    "- 인사만 1~2문장으로 짧게 쓴다(80자 안팎). 따옴표, 목록, 제목, 설명은 붙이지 않는다.\n"
    '- 사용자 메시지에 있는 오늘 날짜를 "M월 D일(요일)" 꼴로 꼭 넣는다. 다른 날짜, 연도, 요일은 쓰지 않는다.\n'
    "- 숫자는 날짜에만 쓴다. 기온, 강수확률 같은 숫자는 쓰지 않는다.\n"
    "- 날씨는 주어진 요약에 맞을 때만 가볍게 한마디 해도 된다. 요약에 없는 날씨는 말하지 않는다.\n"
    "- 일정, 파일, 크레딧 이야기는 하지 않고, 아무도 멘션하지 않는다.\n"
    "- 이전 인사가 주어지면 그것과 다른 말로 시작하고 같은 표현을 되풀이하지 않는다.\n"
    f"- {ACCURACY_RULE}\n"
)


def weather_words(line: str) -> str:
    """The weather line without its label and without any number: ``대체로 맑음 · 미세먼지 보통``."""
    text = re.sub(r"[*_]", "", line or "")
    if ":" in text:
        text = text.split(":", 1)[1]
    parts = [part.strip() for part in text.split("·")]
    return " · ".join(part for part in parts if part and not re.search(r"\d", part) and weather.FAILED_NOTE not in part)


def greeting_prompt(now: datetime, *, weather_text: str = "", previous: str | None = None) -> str:
    """The greeting's user prompt: today's date (from code), a weekday note, the weather in words, the last greeting."""
    fields = date_fields(now)
    if now.weekday() >= 5:
        day_note = f"오늘은 주말({fields['wd']})이에요."
    elif now.weekday() == 0:
        day_note = "오늘은 한 주를 시작하는 월요일이에요."
    else:
        day_note = "오늘은 평일이에요."
    lines = [
        "오늘 아침 브리핑을 여는 인사를 1~2문장으로 써 줘.",
        f"- 오늘 날짜: {now.year}년 {fields['md']} {fields['wd']} (짧게 쓰면 {fields['short']})",
        f"- {day_note}",
        f"- 날씨 요약: {weather_words(weather_text) or '없음'}",
    ]
    if previous:
        lines.append(f'- 이전 인사: "{previous}" (이 인사와 다르게 시작하고, 같은 표현은 쓰지 마)')
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


def valid_greeting(text: str, now: datetime) -> bool:
    """True when ``text`` is a short greeting with today's date and nothing that could be wrong.

    Today's month and day must appear ("10월 8일", or "10/8"); any other date,
    year or weekday, any other number, Slack markup or mention, more than two
    lines or more than ``MAX_GREETING_CHARS`` characters makes it invalid.
    """
    if not text or len(text) > MAX_GREETING_CHARS or text.count("\n") > 1:
        return False
    if any(char in text for char in "<>@*#`|[]_"):
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


def greeting_templates(now: date | datetime) -> list[str]:
    """The fallback templates for ``now``'s day (weekend and Monday ones added on those days)."""
    pool = list(phrases.GREETING_TEMPLATES)
    if now.weekday() >= 5:
        pool += phrases.WEEKEND_GREETING_TEMPLATES
    if now.weekday() == 0:
        pool += phrases.MONDAY_GREETING_TEMPLATES
    return pool


def fallback_greeting(
    now: datetime, *, rng: random.Random | None = None, previous: tuple[str, str] | None = None
) -> str:
    """A template greeting for ``now`` (the date filled in by code); not the template used last time when possible."""
    pool = greeting_templates(now)
    avoid = None
    if previous is not None:
        try:
            last_day = date.fromisoformat(previous[0])
        except ValueError:
            last_day = None
        if last_day is not None:
            last_fields = date_fields(last_day)
            avoid = next((t for t in greeting_templates(last_day) if t.format(**last_fields) == previous[1]), None)
    return phrases.pick(pool, rng, avoid=avoid).format(**date_fields(now))


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
    now: datetime,
    *,
    weather_text: str = "",
    env: Mapping[str, str] | None = None,
    store: StateStore | None = None,
    generate: GreetingGenerate | None = None,
    rng: random.Random | None = None,
    timeout: float = GREETING_TIMEOUT_SECONDS,
) -> Greeting:
    """고뭉치's greeting for ``now``: the model's when it is valid and new, else a template. Never raises.

    The last greeting (state file) goes into the prompt; the one used is
    stored for tomorrow.
    """
    store = store or StateStore(config.get_state_path(env))
    try:
        previous = store.last_greeting()
    except OSError:
        previous = None
    prompt = greeting_prompt(now, weather_text=weather_text, previous=previous[1] if previous else None)
    greeting: Greeting | None = None
    try:
        call = generate(prompt) if generate is not None else generate_greeting(prompt, env)
        text = clean_greeting(await asyncio.wait_for(call, timeout))
        if valid_greeting(text, now) and (previous is None or text != previous[1]):
            greeting = Greeting(text, "llm")
        else:
            log.info("아침 인사가 형식에 맞지 않아(날짜 확인 등) 준비된 인사를 씁니다.")
    except Exception as exc:  # noqa: BLE001 - a template greeting is always fine
        reason = crash_kind(exc) if isinstance(exc, asyncio.TimeoutError) else safe_error(exc)
        log.warning("아침 인사를 만들지 못해 준비된 인사를 씁니다: %s", reason)
    if greeting is None:
        greeting = Greeting(fallback_greeting(now, rng=rng, previous=previous), "template")
    try:
        store.mark_greeting(now.date().isoformat(), greeting.text)
    except OSError as exc:
        log.warning("아침 인사를 상태 파일에 기록하지 못했습니다: %s", safe_error(exc))
    return greeting


# ---------------------------------------------------------------- 업뎃's and 일정's reports


REPORT_PROMPTS = {
    UPDATE: (
        "아침 브리핑에서 네가 맡은 부분을 보고할 차례야. 고뭉치가 인사와 날씨, Chat KHU 크레딧을 이미 전했고, "
        "오늘 일정은 일정이 따로 보고해.\n"
        "- check_dropbox_updates를 since_hours 없이(0) 한 번만 불러 공저자 업데이트를 확인해. "
        "이번 실행은 아침 브리핑이라 도구가 지난 브리핑 이후를 본다. 기간은 결과의 since_basis대로 써.\n"
        '- 업뎃다운 짧은 아침 인사 한 줄(예: "업뎃 보고드립니다!")로 시작하고, 그다음은 네 형식(하위 폴더, 링크, '
        "사람별 파일과 수정 시각)과 규칙을 그대로 따라. 변경이 없으면 그 한 줄이면 돼.\n"
        "- 날씨, 크레딧, 일정은 쓰지 말고, 다른 팀원을 부르거나 멘션하지 마. 짧게."
    ),
    SCHEDULE: (
        "아침 브리핑에서 네가 맡은 부분을 보고할 차례야. 고뭉치가 인사와 날씨, Chat KHU 크레딧을 이미 전했고, "
        "Dropbox 소식은 업뎃이 따로 보고해.\n"
        '- get_schedule을 date="{iso}", days=1로 한 번만 불러 오늘({korean}) 하루치 일정만 확인해. get_weather는 부르지 마.\n'
        "- 일정다운 밝은 아침 한마디로 시작하고, 지금 / 바로 다음 일정을 맨 앞에, 이어서 오늘 일정을 네 형식대로 시간 순으로 써. "
        '겹침과 쓸모 있는 빈 시간(1~3개)도 형식대로. 오늘 일정이 없으면 "오늘 일정 없음"이라고 써.\n'
        "- 날씨, 크레딧, Dropbox는 쓰지 말고, 다른 팀원을 부르거나 멘션하지 마. 짧게."
    ),
}


def report_prompt(persona: str, now: datetime) -> str:
    """업뎃's or 일정's user prompt for the morning report (the date from code; no weather, no credits)."""
    korean = f"{now.date().isoformat()} ({WEEKDAYS_KO[now.weekday()]}요일)"
    return REPORT_PROMPTS[persona].format(iso=now.date().isoformat(), korean=korean)


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
    now: datetime,
    slack: bool,
    run_timeout: float | None = None,
    on_status: Callable[[str], Any] | None = None,
    rng: random.Random | None = None,
) -> Report:
    """One direct 업뎃 / 일정 run for the morning briefing. Never raises (except when cancelled).

    업뎃 runs in briefing mode (its Dropbox checkpoint moves); 일정 looks at today only.
    """
    label = PERSONA_LABELS[persona]
    result: TurnResult | None = None
    crash: BaseException | None = None
    try:
        turn = run(
            report_prompt(persona, now),
            on_status=on_status,
            extra_system_prompt=SLACK_FORMAT_PROMPT if slack else "",
            persona=persona,
            briefing=persona == UPDATE,
        )
        result = await (asyncio.wait_for(turn, run_timeout) if run_timeout else turn)
    except Exception as exc:  # noqa: BLE001 - reported as a short apology, details in the log
        crash = exc
        log.error("%s의 아침 보고를 만들지 못했습니다: %s", label, crash_kind(exc) if isinstance(exc, asyncio.TimeoutError) else safe_error(exc))
    else:
        if result.failed:
            log.warning("%s의 아침 보고가 완전하지 않습니다: %s", label, scrub(result.error or ""))
    return Report(persona, report_text(persona, result, crash, rng=rng), result, crash)


# ---------------------------------------------------------------- the relay


@dataclass
class Head:
    """고뭉치's part before its closing lines: the greeting, the weather line ("" when off) and the credits."""

    greeting: Greeting
    weather: str
    credits: str

    def text(self, closing: Sequence[str] = ()) -> str:
        return compose_head(self.greeting.text, self.weather, self.credits, closing)


def compose_head(greeting: str, weather_text: str, credit_text: str, closing: Sequence[str] = ()) -> str:
    """Greeting, then the weather and credit lines together, then the closing lines (notes, hand-off)."""
    data = "\n".join(part.strip() for part in (weather_text, credit_text) if part and part.strip())
    tail = "\n".join(line.strip() for line in closing if line and line.strip())
    return "\n\n".join(part for part in (greeting.strip(), data, tail) if part)


async def _build_head(
    now: datetime,
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
    credit_task = asyncio.ensure_future(asyncio.to_thread(credit_section, env, fetch=credit_fetch, now=now, slack=slack))
    try:
        weather_text = (
            await asyncio.to_thread(weather_section, env, fetch=weather_fetch, slack=slack)
            if config.get_brief_weather(env)
            else ""
        )
        greeting = await make_greeting(
            now,
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
    return Head(greeting, weather_text, credit_text)


class Relay:
    """The running parts of one relay briefing, awaited in posting order: ``head()``, then ``report(persona)``.

    Use it as ``async with start_relay(...) as relay``: leaving the block
    cancels whatever is still running (e.g. when the bots stop).
    """

    def __init__(self, head: asyncio.Future[Head], reports: dict[str, asyncio.Future[Report]], now: datetime):
        self._head = head
        self._reports = reports
        self.now = now

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
    up is left out by the caller). ``slack`` picks Slack formatting.
    ``run_timeout`` (seconds) bounds each report run.
    """
    tz = config.get_timezone(env)
    now = (now or datetime.now(tz)).astimezone(tz)
    run = run or run_turn
    rng = rng or random.Random()
    reports = {
        persona: asyncio.ensure_future(
            run_report(persona, run=run, now=now, slack=slack, run_timeout=run_timeout, on_status=on_status, rng=rng)
        )
        for persona in personas
    }
    head = asyncio.ensure_future(
        _build_head(
            now,
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
    return Relay(head, reports, now)


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

    Progress and errors go to stderr. Exits 1 when 업뎃's or 일정's part could not be made.
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
            handoff = phrases.pick(phrases.HANDOFF_TEMPLATES, rng).format(bots=phrases.teammate_names(REPORTERS))
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
