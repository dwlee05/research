"""Today's weather in one Korean line, without any LLM call or API key.

The briefing (``--brief``, ``--brief --slack`` and the scheduled morning
briefing), ``python -m mungchi --weather`` and the Slack shortcut ("@고뭉치
날씨") all go through here, and so does the ``get_weather`` tool of 일정 and 고뭉치
(``weather_payload``: today and tomorrow as compact JSON). The data comes
from Open-Meteo (free, no key):

- ``GET https://api.open-meteo.com/v1/forecast``: today's weather code,
  lowest / highest temperature and highest chance of rain (plus the current
  temperature and weather code as a fallback); the tool also asks for tomorrow;
- ``GET https://air-quality-api.open-meteo.com/v1/air-quality``: the current
  PM10 / PM2.5, graded by the Korean standard (optional: left out when it fails).

    🌤️ 서울 날씨: 대체로 맑음 · 최저 12° / 최고 23° · 강수확률 10% · 미세먼지 보통

Nothing here raises into a caller: a failed forecast becomes the short note
``🌤️ 서울 날씨: 가져오지 못했어요``.
"""

from __future__ import annotations

import functools
import logging
import re
import sys
import unicodedata
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping, TextIO

import httpx

from . import config
from .model_list import SSL_HINT
from .quick_info import query_text
from .tools.common import safe_error, scrub

log = logging.getLogger("mungchi.weather")

TIMEOUT_SECONDS = 10.0
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
# From this chance of rain (%) on, the line ends with an umbrella reminder.
UMBRELLA_PERCENT = 60.0

DEFAULT_EMOJI = "🌤️"
FAILED_NOTE = "가져오지 못했어요"
UMBRELLA_NOTE = "☔ 우산 챙기세요"
UNKNOWN_WEATHER = ("날씨 정보", "🌡️")
# Used in the CLI's error lines.
ERROR_TEXT = "날씨를 가져오지 못했습니다 ({reason})."
CONNECT_HINT = "→ 인터넷 연결을 확인하세요. 학교·회사 네트워크가 api.open-meteo.com을 막고 있을 수도 있어요."
AIR_MISSING_TEXT = "(미세먼지 정보는 받지 못했습니다: {reason})"

# WMO weather codes as Open-Meteo reports them -> (short Korean description, emoji).
WEATHER_CODES: dict[int, tuple[str, str]] = {
    0: ("맑음", "☀️"),
    1: ("대체로 맑음", "🌤️"),
    2: ("구름 많음", "⛅"),
    3: ("흐림", "☁️"),
    45: ("안개", "🌫️"),
    48: ("안개", "🌫️"),  # depositing rime fog
    51: ("이슬비", "🌦️"),
    53: ("이슬비", "🌦️"),
    55: ("강한 이슬비", "🌦️"),
    56: ("어는 이슬비", "🌧️"),
    57: ("어는 이슬비", "🌧️"),
    61: ("약한 비", "🌧️"),
    63: ("비", "🌧️"),
    65: ("강한 비", "🌧️"),
    66: ("어는 비", "🌧️"),
    67: ("어는 비", "🌧️"),
    71: ("약한 눈", "🌨️"),
    73: ("눈", "🌨️"),
    75: ("많은 눈", "🌨️"),
    77: ("싸락눈", "🌨️"),
    80: ("소나기", "🌦️"),
    81: ("소나기", "🌦️"),
    82: ("강한 소나기", "🌧️"),
    85: ("눈 소나기", "🌨️"),
    86: ("강한 눈 소나기", "🌨️"),
    95: ("뇌우", "⛈️"),
    96: ("우박 동반 뇌우", "⛈️"),
    99: ("우박 동반 뇌우", "⛈️"),
}

# Korean fine-dust standard (µg/m³): the upper bound of each grade; above the last is 매우나쁨.
DUST_GRADES = ("좋음", "보통", "나쁨", "매우나쁨")
PM10_LIMITS = (30.0, 80.0, 150.0)
PM25_LIMITS = (15.0, 35.0, 75.0)

# ---------------------------------------------------------------- the Slack shortcut's question
#
# A short message that only asks for today's weather, matched after the bot's
# mention is removed:
#
#   [오늘 | 오늘의 | 지금 | 현재] [서울 | WEATHER_LABEL | 여기][의] 날씨[는 | 은 | 가 | 좀] [어때 | 알려줘 | ...]
#
# with or without spaces, any trailing punctuation and emoji ("날씨 🙏",
# "날씨 :pray:" as Slack sends it). Anything longer ("내일 비 오면 일정 바꿔야
# 할까?", "날씨 좋은 날 야외 미팅 잡아줘") does not match and goes to the agent.

WEATHER_TIME_RE = r"(?:오늘의?|지금|현재)"
WEATHER_PARTICLE_RE = r"(?:는|은|가|좀)"
WEATHER_TAIL_RE = (
    r"(?:어때요?|어떄|어떠니|어떤가요"  # 어떄: a common typo of 어때
    r"|(?:좀 ?)?알려 ?(?:줘요?|줄래|주세요)"
    r"|확인(?: ?해 ?줘)?|보여 ?줘|궁금해)"
)
WEATHER_PLACES = ("서울", "여기")


@functools.lru_cache(maxsize=16)
def _weather_query_re(label: str) -> re.Pattern[str]:
    places = list(WEATHER_PLACES)
    label = " ".join(unicodedata.normalize("NFC", label).lower().split())
    if label and label not in places:
        places.append(label)
    place = "(?:" + "|".join(" ?".join(re.escape(part) for part in name.split()) for name in places) + ")"
    return re.compile(
        rf"^(?:{WEATHER_TIME_RE} ?)?(?:{place}(?: ?의)? ?)?날씨(?: ?{WEATHER_PARTICLE_RE})?(?: ?{WEATHER_TAIL_RE})?$"
    )


def is_weather_query(text: str | None, label: str | None = None) -> bool:
    """True when ``text`` (mention already removed) only asks for today's weather.

    ``label`` is the configured ``WEATHER_LABEL`` (e.g. 부산); 서울 and 여기 are
    always accepted as the place. Pure: no LLM call, no I/O.
    """
    normalized = query_text(text)
    return bool(normalized) and _weather_query_re(label or "").match(normalized) is not None


def configured_label(env: Mapping[str, str] | None = None) -> str:
    """``WEATHER_LABEL`` as the weather line shows it (서울 when unset or unusable). Never raises."""
    try:
        return config.load_weather_config(env).label
    except Exception:  # noqa: BLE001 - the shortcut still knows 서울
        return config.DEFAULT_WEATHER_LABEL


# ---------------------------------------------------------------- parsing (defensive)


@dataclass(frozen=True)
class Forecast:
    """One day's forecast; every field is None when the response did not have it.

    ``current_temp`` / ``current_code`` (the weather right now) are only set for today.
    """

    code: int | None = None  # the day's weather code (today: the current one when the daily one is missing)
    low: float | None = None
    high: float | None = None
    rain_chance: float | None = None  # precipitation_probability_max, %
    current_temp: float | None = None
    current_code: int | None = None

    @property
    def empty(self) -> bool:
        return all(value is None for value in (self.code, self.low, self.high, self.rain_chance, self.current_temp))


@dataclass(frozen=True)
class AirQuality:
    pm10: float | None = None
    pm2_5: float | None = None

    @property
    def empty(self) -> bool:
        return self.pm10 is None and self.pm2_5 is None


@dataclass
class WeatherReport:
    """What one fetch found. ``error`` / ``air_error`` (short Korean) say why a part is missing."""

    label: str = config.DEFAULT_WEATHER_LABEL
    forecast: Forecast | None = None
    # Only when asked for (``fetch_report(days=2)``, the get_weather tool); the line never uses it.
    tomorrow: Forecast | None = None
    air: AirQuality | None = None
    error: str | None = None
    air_error: str | None = None
    # The forecast request's exception text (scrubbed), for the CLI's error line only.
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.forecast is not None and not self.forecast.empty


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    """A finite number from an int, float or numeric string; None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if number == number and abs(number) != float("inf") else None


def _nth(value: Any, day: int) -> Any:
    """Entry ``day`` of a daily array (0: today, 1: tomorrow); a bare value counts as today's."""
    if isinstance(value, list):
        return value[day] if 0 <= day < len(value) else None
    return value if day == 0 else None


def _code(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number == int(number) and number >= 0 else None


def parse_forecast(payload: Any, day: int = 0) -> Forecast:
    """Open-Meteo forecast JSON -> ``Forecast`` for ``day`` (0: today, 1: tomorrow).

    Missing or odd fields become None, never an error. Only today falls back
    to the current weather code and carries the current temperature.
    """
    data = _mapping(payload)
    daily = _mapping(data.get("daily"))
    current = _mapping(data.get("current")) if day == 0 else {}
    current_code = _code(current.get("weather_code"))
    code = _code(_nth(daily.get("weather_code"), day))
    if code is None:
        code = current_code
    return Forecast(
        code=code,
        low=_number(_nth(daily.get("temperature_2m_min"), day)),
        high=_number(_nth(daily.get("temperature_2m_max"), day)),
        rain_chance=_number(_nth(daily.get("precipitation_probability_max"), day)),
        current_temp=_number(current.get("temperature_2m")),
        current_code=current_code,
    )


def parse_air_quality(payload: Any) -> AirQuality:
    """Open-Meteo air-quality JSON -> ``AirQuality`` (negative values are dropped)."""
    current = _mapping(_mapping(payload).get("current"))
    values = [_number(current.get(name)) for name in ("pm10", "pm2_5")]
    pm10, pm2_5 = (value if value is not None and value >= 0 else None for value in values)
    return AirQuality(pm10=pm10, pm2_5=pm2_5)


# ---------------------------------------------------------------- formatting (pure)


def describe_code(code: int | None) -> tuple[str, str]:
    """``(Korean description, emoji)`` for a WMO weather code; ``("날씨 정보", "🌡️")`` when unknown."""
    return WEATHER_CODES.get(code, UNKNOWN_WEATHER) if code is not None else UNKNOWN_WEATHER


def round_half_up(value: float) -> int:
    """12.5 -> 13, -0.5 -> -1, -0.4 -> 0 (never "-0")."""
    try:
        return int(Decimal(repr(float(value))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, OverflowError):
        return int(round(value))


def _grade(value: float | None, limits: tuple[float, float, float]) -> int | None:
    if value is None:
        return None
    for index, limit in enumerate(limits):
        if value <= limit:
            return index
    return len(limits)


def pm10_grade(value: float | None) -> str | None:
    """PM10 (µg/m³): ≤30 좋음, ≤80 보통, ≤150 나쁨, above 매우나쁨."""
    index = _grade(value, PM10_LIMITS)
    return DUST_GRADES[index] if index is not None else None


def pm25_grade(value: float | None) -> str | None:
    """PM2.5 (µg/m³): ≤15 좋음, ≤35 보통, ≤75 나쁨, above 매우나쁨."""
    index = _grade(value, PM25_LIMITS)
    return DUST_GRADES[index] if index is not None else None


def dust_grade(air: AirQuality | None) -> str | None:
    """The worse of the PM10 and PM2.5 grades (either alone when the other is missing)."""
    if air is None:
        return None
    grades = [g for g in (_grade(air.pm10, PM10_LIMITS), _grade(air.pm2_5, PM25_LIMITS)) if g is not None]
    return DUST_GRADES[max(grades)] if grades else None


def _name(label: str, slack: bool) -> str:
    return f"*{label} 날씨*" if slack else f"{label} 날씨"


def failed_line(label: str = config.DEFAULT_WEATHER_LABEL, *, slack: bool = False) -> str:
    """``🌤️ 서울 날씨: 가져오지 못했어요`` (the label bold in Slack)."""
    return f"{DEFAULT_EMOJI} {_name(label, slack)}: {FAILED_NOTE}"


def format_line(
    label: str, forecast: Forecast | None, air: AirQuality | None = None, *, slack: bool = False
) -> str:
    """One Korean line; parts without data are left out.

    ``🌤️ 서울 날씨: 대체로 맑음 · 최저 12° / 최고 23° · 강수확률 10% · 미세먼지 보통``, with
    `` · ☔ 우산 챙기세요`` at the end from a 60% chance of rain. The current
    temperature is shown only when today's lowest and highest are missing.
    """
    if forecast is None or forecast.empty:
        return failed_line(label, slack=slack)
    parts: list[str] = []
    if forecast.code is not None:
        description, emoji = describe_code(forecast.code)
        parts.append(description)
    else:
        emoji = DEFAULT_EMOJI
    temps = [
        f"{name} {round_half_up(value)}°"
        for name, value in (("최저", forecast.low), ("최고", forecast.high))
        if value is not None
    ]
    if temps:
        parts.append(" / ".join(temps))
    elif forecast.current_temp is not None:
        parts.append(f"지금 {round_half_up(forecast.current_temp)}°")
    if forecast.rain_chance is not None:
        parts.append(f"강수확률 {round_half_up(forecast.rain_chance)}%")
    dust = dust_grade(air)
    if dust:
        parts.append(f"미세먼지 {dust}")
    if forecast.rain_chance is not None and forecast.rain_chance >= UMBRELLA_PERCENT:
        parts.append(UMBRELLA_NOTE)
    return f"{emoji} {_name(label, slack)}: " + " · ".join(parts)


def report_line(report: WeatherReport, *, slack: bool = False) -> str:
    """The line for a fetched report: the weather, or the short failure note."""
    if not report.ok:
        return failed_line(report.label, slack=slack)
    return format_line(report.label, report.forecast, report.air, slack=slack)


# ---------------------------------------------------------------- fetching


class _FetchError(Exception):
    """A failed request: ``short`` (e.g. "HTTP 500") and ``detail`` (the exception text, scrubbed)."""

    def __init__(self, short: str, detail: str = ""):
        super().__init__(short)
        self.short = short
        self.detail = detail


def forecast_params(cfg: config.WeatherConfig, days: int = 1) -> dict[str, str]:
    """``days`` 1 (today: the line) or 2 (today and tomorrow: the get_weather tool)."""
    return {
        "latitude": f"{cfg.latitude:.4f}",
        "longitude": f"{cfg.longitude:.4f}",
        "current": "temperature_2m,weather_code",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": cfg.timezone_name,
        "forecast_days": str(days),
    }


def air_quality_params(cfg: config.WeatherConfig) -> dict[str, str]:
    return {
        "latitude": f"{cfg.latitude:.4f}",
        "longitude": f"{cfg.longitude:.4f}",
        "current": "pm10,pm2_5",
        "timezone": cfg.timezone_name,
    }


def _reason(response: httpx.Response) -> str:
    """Open-Meteo's ``{"error": true, "reason": "..."}``, flattened and cut short ("" when absent)."""
    try:
        reason = _mapping(response.json()).get("reason")
    except ValueError:
        return ""
    return " ".join(str(reason).split())[:120] if isinstance(reason, str) else ""


def _get_json(client: httpx.Client, url: str, params: Mapping[str, str]) -> Any:
    try:
        response = client.get(url, params=params)
    except httpx.TimeoutException as exc:
        raise _FetchError("시간 초과", safe_error(exc)) from None
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise _FetchError(f"연결 실패: {type(exc).__name__}", safe_error(exc)) from None
    if response.status_code != 200:
        reason = _reason(response)
        raise _FetchError(f"HTTP {response.status_code}" + (f": {reason}" if reason else ""))
    try:
        payload = response.json()
    except ValueError:
        raise _FetchError("HTTP 200이지만 JSON 응답이 아님") from None
    if isinstance(payload, Mapping) and payload.get("error"):
        raise _FetchError(_reason(response) or "오류 응답")
    return payload


def fetch_report(
    cfg: config.WeatherConfig | None = None,
    *,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: float = TIMEOUT_SECONDS,
    days: int = 1,
) -> WeatherReport:
    """Fetch today's forecast (with ``days=2`` tomorrow's too), then the fine dust. Never raises.

    A failed forecast leaves ``forecast`` None with ``error`` set (the air
    quality is then not asked for); a failed air-quality request only sets
    ``air_error``. A missing tomorrow only leaves ``tomorrow`` None.
    """
    cfg = cfg or config.load_weather_config(env)
    days = 2 if days >= 2 else 1
    report = WeatherReport(label=cfg.label)
    try:
        with httpx.Client(transport=transport, timeout=timeout) as client:
            try:
                payload = _get_json(client, FORECAST_URL, forecast_params(cfg, days))
            except _FetchError as exc:
                report.error, report.detail = exc.short, exc.detail or None
                return report
            forecast = parse_forecast(payload)
            if forecast.empty:
                report.error = "응답에 날씨 값이 없음"
                return report
            report.forecast = forecast
            if days == 2:
                tomorrow = parse_forecast(payload, day=1)
                report.tomorrow = None if tomorrow.empty else tomorrow
            try:
                air = parse_air_quality(_get_json(client, AIR_QUALITY_URL, air_quality_params(cfg)))
            except _FetchError as exc:
                report.air_error = exc.short
            except Exception as exc:  # noqa: BLE001 - the dust part is optional
                report.air_error = type(exc).__name__
            else:
                if air.empty:
                    report.air_error = "응답에 미세먼지 값이 없음"
                else:
                    report.air = air
    except Exception as exc:  # noqa: BLE001 - a short Korean reason, never a traceback
        if report.forecast is None:
            report.error = report.error or f"응답을 읽지 못함: {type(exc).__name__}"
        else:
            report.air_error = report.air_error or type(exc).__name__
    return report


def load_config(env: Mapping[str, str] | None = None) -> config.WeatherConfig:
    """``config.load_weather_config`` with its warnings logged (scrubbed by the bots' log filter)."""
    cfg = config.load_weather_config(env)
    for warning in cfg.warnings:
        log.warning("날씨 설정: %s", warning)
    return cfg


def weather_line(
    env: Mapping[str, str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    slack: bool = False,
) -> str:
    """Fetch and format today's line, or the short failure note. Never raises. Blocking."""
    label = config.DEFAULT_WEATHER_LABEL
    try:
        cfg = load_config(env)
        label = cfg.label
        report = fetch_report(cfg, transport=transport)
        if not report.ok:
            log.warning("날씨를 가져오지 못했습니다: %s", scrub(report.error or "응답 없음"))
        return report_line(report, slack=slack)
    except Exception as exc:  # noqa: BLE001 - the caller always gets a line
        log.warning("날씨를 가져오지 못했습니다: %s", type(exc).__name__)
        return failed_line(label, slack=slack)


def slack_weather_text(
    env: Mapping[str, str] | None = None, *, transport: httpx.BaseTransport | None = None
) -> str:
    """What the Slack shortcut posts: the line in mrkdwn (bold label), or the short failure note."""
    return weather_line(env, transport=transport, slack=True)


# ---------------------------------------------------------------- the get_weather tool (일정, 고뭉치)


def _degrees(value: float | None) -> int | None:
    return round_half_up(value) if value is not None else None


def _description(code: int | None) -> str | None:
    return describe_code(code)[0] if code is not None else None


def _day(forecast: Forecast) -> dict[str, Any]:
    return {
        "description": _description(forecast.code),
        "min": _degrees(forecast.low),
        "max": _degrees(forecast.high),
        "precipitation_probability": _degrees(forecast.rain_chance),
    }


def report_payload(report: WeatherReport, warnings: tuple[str, ...] | list[str] = ()) -> dict[str, Any]:
    """Compact JSON for the get_weather tool. Temperatures in whole °C, chance of rain in %.

    ``summary`` is today's Korean line (the same as the briefing's). On a failed
    forecast: ``ok: false`` with a short Korean ``error`` and the failure note.
    """
    payload: dict[str, Any] = {"configured": True, "ok": report.ok, "label": report.label}
    if not report.ok or report.forecast is None:
        payload["error"] = report.error or "응답 없음"
        payload["summary"] = failed_line(report.label)
    else:
        payload["summary"] = report_line(report)
        payload["today"] = _day(report.forecast)
        if report.tomorrow is not None:
            payload["tomorrow"] = _day(report.tomorrow)
        payload["current"] = {
            "temp": _degrees(report.forecast.current_temp),
            "description": _description(report.forecast.current_code),
        }
        payload["fine_dust"] = dust_grade(report.air)
        if report.air_error:
            payload["fine_dust_error"] = report.air_error
    if warnings:
        payload["warnings"] = list(warnings)
    return payload


def weather_payload(
    env: Mapping[str, str] | None = None, *, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    """What the get_weather tool returns: today and tomorrow (``report_payload``). Never raises. Blocking."""
    label = config.DEFAULT_WEATHER_LABEL
    try:
        cfg = load_config(env)
        label = cfg.label
        report = fetch_report(cfg, transport=transport, days=2)
        if not report.ok:
            log.warning("날씨를 가져오지 못했습니다: %s", scrub(report.error or "응답 없음"))
        return report_payload(report, cfg.warnings)
    except Exception as exc:  # noqa: BLE001 - the agent always gets a short Korean reason
        log.warning("날씨를 가져오지 못했습니다: %s", type(exc).__name__)
        return report_payload(WeatherReport(label=label, error=f"응답을 읽지 못함: {type(exc).__name__}"))


# ---------------------------------------------------------------- CLI


def run_weather_cli(
    env: Mapping[str, str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """``python -m mungchi --weather``: the line on stdout; warnings and errors on stderr."""
    out = out or sys.stdout
    err = err or sys.stderr
    cfg = config.load_weather_config(env)
    for warning in cfg.warnings:
        print(f"[경고] {warning}", file=err)
    report = fetch_report(cfg, transport=transport)
    if not report.ok:
        reason = report.error or "응답 없음"
        if report.detail:
            reason = f"{reason} — {report.detail[:200]}"
        print(scrub("[오류] " + ERROR_TEXT.format(reason=reason)), file=err)
        print(SSL_HINT if "CERTIFICATE_VERIFY_FAILED" in (report.detail or "") else CONNECT_HINT, file=err)
        return 1
    print(report_line(report), file=out)
    if report.air_error:
        print(AIR_MISSING_TEXT.format(reason=report.air_error), file=err)
    return 0
