"""Today's briefing, built the same way for every way it is delivered.

``python -m mungchi --brief`` (terminal), ``--brief --slack`` and the
scheduled morning briefing of the running Slack bots (``BRIEF_TIME``) all go
through ``build_briefing``, so they share one structure:

    ☀️ 오늘의 브리핑 (10/08 목)
    🌤️ 서울 날씨: 대체로 맑음 · ...           ← added by code (Open-Meteo), no LLM; BRIEF_WEATHER=off drops it
    ① 오늘의 일정 · ② Dropbox 업데이트      ← 고뭉치's answer (one briefing run)
    💳 Chat KHU 크레딧: ...                 ← appended by code, no LLM

The weather and the credits are fetched in worker threads while the agent
runs; the model never sees them. The agent run is a briefing run
(``briefing=True``): its Dropbox check looks at the time since the last
briefing and moves that checkpoint. If the run fails, the header, the
weather line, a short Korean failure line and the credits still go out.

``brief_due`` is the pure "is the morning briefing due now?" check used by
the scheduler in ``slack_bot``.
"""

from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, TextIO

from . import config, credits, weather
from .main import TurnResult, briefing_prompt, run_turn
from .personas import MUNGCHI
from .slack_format import SLACK_FORMAT_PROMPT, brief_header
from .tools.common import safe_error, scrub

RunTurn = Callable[..., Awaitable[TurnResult]]
CreditFetch = Callable[[], credits.CreditReport]
WeatherFetch = Callable[[], weather.WeatherReport]

BRIEF_CRASH_TEXT = "⚠️ 오늘 브리핑을 만들지 못했어요 ({kind}). 실행 로그를 확인해 주세요."
BRIEF_FAILED_TEXT = "⚠️ 고뭉치가 브리핑을 끝내지 못했어요."
BRIEF_EMPTY_TEXT = "⚠️ 고뭉치가 빈 브리핑을 보냈어요."
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


# ---------------------------------------------------------------- what


@dataclass
class Briefing:
    """One briefing: the header, the weather line, 고뭉치's part (or a failure line) and the credit section."""

    header: str
    body: str
    credits: str
    result: TurnResult | None = None
    crash: BaseException | None = None
    weather: str = ""  # empty with BRIEF_WEATHER=off

    @property
    def failed(self) -> bool:
        return self.result is None or self.result.failed

    @property
    def session_id(self) -> str | None:
        return self.result.session_id if self.result is not None else None

    @property
    def text(self) -> str:
        return compose_briefing(self.header, self.body, self.credits, self.weather)


def compose_briefing(header: str, body: str, credit_text: str, weather_text: str = "") -> str:
    """Header (with the weather line right under it), body and credit section, separated by blank lines."""
    head = "\n".join(part.strip() for part in (header, weather_text) if part and part.strip())
    return "\n\n".join(part.strip() for part in (head, body, credit_text) if part and part.strip())


def crash_kind(exc: BaseException) -> str:
    return "시간 초과" if isinstance(exc, asyncio.TimeoutError) else type(exc).__name__


def briefing_body(result: TurnResult | None, crash: BaseException | None = None) -> str:
    """고뭉치's answer, plus a short scrubbed Korean note when the run failed; a failure line when it crashed."""
    if result is None:
        return BRIEF_CRASH_TEXT.format(kind=crash_kind(crash) if crash is not None else "결과 없음")
    text = (result.text or "").strip()
    if result.failed:
        reason = scrub(result.error or "")[:MAX_ERROR_CHARS]
        note = f"⚠️ {reason}" if reason else BRIEF_FAILED_TEXT
        return f"{text}\n\n{note}" if text else note
    return text or BRIEF_EMPTY_TEXT


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


async def _no_weather() -> str:
    return ""


async def build_briefing(
    *,
    run: RunTurn | None = None,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    slack: bool = False,
    on_status: Callable[[str], Any] | None = None,
    credit_fetch: CreditFetch | None = None,
    weather_fetch: WeatherFetch | None = None,
    run_timeout: float | None = None,
) -> Briefing:
    """Run today's briefing, with the weather under the header and the credits at the end.

    Never raises for a failed run, weather or credit check. The weather
    (unless ``BRIEF_WEATHER=off``) and the credits are fetched by code in
    worker threads while the agent runs; they never go through the model.
    ``slack`` picks Slack formatting (the Slack prompt rules, a bold header,
    mrkdwn credits and weather label). ``run_timeout`` (seconds) bounds the agent run.
    """
    tz = config.get_timezone(env)
    now = (now or datetime.now(tz)).astimezone(tz)
    run = run or run_turn
    result: TurnResult | None = None
    crash: BaseException | None = None
    extras = asyncio.gather(
        asyncio.to_thread(credit_section, env, fetch=credit_fetch, now=now, slack=slack),
        (
            asyncio.to_thread(weather_section, env, fetch=weather_fetch, slack=slack)
            if config.get_brief_weather(env)
            else _no_weather()
        ),
    )
    try:
        try:
            turn = run(
                briefing_prompt(now, env),
                on_status=on_status,
                extra_system_prompt=SLACK_FORMAT_PROMPT if slack else "",
                persona=MUNGCHI,
                briefing=True,
            )
            result = await (asyncio.wait_for(turn, run_timeout) if run_timeout else turn)
        except Exception as exc:  # noqa: BLE001 - reported in the briefing without details
            crash = exc
        credit_text, weather_text = await extras
    finally:
        extras.cancel()  # only matters when the run itself was cancelled; a no-op once done
    return Briefing(
        header=brief_header(now, slack=slack),
        body=briefing_body(result, crash),
        credits=credit_text,
        result=result,
        crash=crash,
        weather=weather_text,
    )


# ---------------------------------------------------------------- terminal


def run_brief_cli(
    env: Mapping[str, str] | None = None,
    *,
    run: RunTurn | None = None,
    now: datetime | None = None,
    credit_fetch: CreditFetch | None = None,
    weather_fetch: WeatherFetch | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """``python -m mungchi --brief``: the briefing on stdout, progress and errors on stderr."""
    out = out or sys.stdout
    err = err or sys.stderr

    def status(line: str) -> None:
        print(line, file=err, flush=True)

    briefing = asyncio.run(
        build_briefing(
            run=run,
            now=now,
            env=env,
            slack=False,
            on_status=status,
            credit_fetch=credit_fetch,
            weather_fetch=weather_fetch,
        )
    )
    if briefing.crash is not None:
        print(f"[오류] 고뭉치를 실행하지 못했습니다: {safe_error(briefing.crash)}", file=err)
    print(briefing.text, file=out, flush=True)
    return 1 if briefing.failed else 0
