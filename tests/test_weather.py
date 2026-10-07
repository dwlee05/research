"""Today's weather line (Open-Meteo, no key, no LLM) against a mocked httpx transport (no network)."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import unicodedata

import httpx
import pytest

from mungchi import config, weather
from mungchi.main import build_parser, main
from mungchi.weather import (
    AirQuality,
    Forecast,
    WeatherReport,
    describe_code,
    dust_grade,
    fetch_report,
    format_line,
    is_weather_query,
    parse_air_quality,
    parse_forecast,
    pm10_grade,
    pm25_grade,
    report_line,
    run_weather_cli,
    slack_weather_text,
    weather_line,
)

# The shape Open-Meteo answers with for the request this module sends.
FORECAST_JSON = {
    "latitude": 37.55,
    "longitude": 127.0,
    "generationtime_ms": 0.05,
    "utc_offset_seconds": 32400,
    "timezone": "Asia/Seoul",
    "timezone_abbreviation": "GMT+9",
    "elevation": 38.0,
    "current_units": {"time": "iso8601", "interval": "seconds", "temperature_2m": "°C", "weather_code": "wmo code"},
    "current": {"time": "2026-10-08T07:00", "interval": 900, "temperature_2m": 14.2, "weather_code": 1},
    "daily_units": {
        "time": "iso8601",
        "weather_code": "wmo code",
        "temperature_2m_max": "°C",
        "temperature_2m_min": "°C",
        "precipitation_probability_max": "%",
    },
    "daily": {
        "time": ["2026-10-08"],
        "weather_code": [1],
        "temperature_2m_max": [22.6],
        "temperature_2m_min": [11.5],
        "precipitation_probability_max": [10],
    },
}
AIR_JSON = {
    "latitude": 37.6,
    "longitude": 127.0,
    "timezone": "Asia/Seoul",
    "current_units": {"time": "iso8601", "interval": "seconds", "pm10": "μg/m³", "pm2_5": "μg/m³"},
    "current": {"time": "2026-10-08T07:00", "interval": 3600, "pm10": 42.3, "pm2_5": 12.0},
}
LINE = "🌤️ 서울 날씨: 대체로 맑음 · 최저 12° / 최고 23° · 강수확률 10% · 미세먼지 보통"
SLACK_LINE = "🌤️ *서울 날씨*: 대체로 맑음 · 최저 12° / 최고 23° · 강수확률 10% · 미세먼지 보통"
FAILED = "🌤️ 서울 날씨: 가져오지 못했어요"
FORECAST_HOST = "api.open-meteo.com"
AIR_HOST = "air-quality-api.open-meteo.com"


def routes(forecast=None, air=None):
    """A transport answering Open-Meteo's two hosts, recording every request.

    ``forecast`` / ``air`` may be a Response, a callable taking the request,
    or None for the sample JSON.
    """
    requests: list[httpx.Request] = []

    def answer(spec, default, request):
        if spec is None:
            return httpx.Response(200, json=default)
        return spec(request) if callable(spec) else spec

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == FORECAST_HOST:
            return answer(forecast, FORECAST_JSON, request)
        if request.url.host == AIR_HOST:
            return answer(air, AIR_JSON, request)
        return httpx.Response(404, json={"error": True, "reason": "unknown host"})

    return httpx.MockTransport(handle), requests


def daily(**values):
    """The sample forecast with some daily values replaced (None removes the field)."""
    days = {**FORECAST_JSON["daily"]}
    for key, value in values.items():
        if value is None:
            days.pop(key, None)
        else:
            days[key] = [value]
    return {**FORECAST_JSON, "daily": days}


def line(payload=FORECAST_JSON, air=AIR_JSON, **kwargs):
    return format_line("서울", parse_forecast(payload), parse_air_quality(air) if air is not None else None, **kwargs)


def broken(request):
    raise httpx.ConnectError("connection refused", request=request)


# ---------------------------------------------------------------- formatting


def test_the_sample_response_becomes_the_agreed_line():
    assert line() == LINE
    assert line(slack=True) == SLACK_LINE  # the label is bold in Slack, like the credit line


@pytest.mark.parametrize(
    "code,description,emoji",
    [
        (0, "맑음", "☀️"),
        (1, "대체로 맑음", "🌤️"),
        (2, "구름 많음", "⛅"),
        (3, "흐림", "☁️"),
        (45, "안개", "🌫️"),
        (48, "안개", "🌫️"),
        (51, "이슬비", "🌦️"),
        (61, "약한 비", "🌧️"),
        (63, "비", "🌧️"),
        (65, "강한 비", "🌧️"),
        (71, "약한 눈", "🌨️"),
        (73, "눈", "🌨️"),
        (80, "소나기", "🌦️"),
        (95, "뇌우", "⛈️"),
        (99, "우박 동반 뇌우", "⛈️"),
    ],
)
def test_weather_codes_become_short_korean_descriptions(code, description, emoji):
    assert describe_code(code) == (description, emoji)
    assert line(daily(weather_code=code)).startswith(f"{emoji} 서울 날씨: {description} · 최저")


def test_every_wmo_code_open_meteo_uses_is_mapped_and_unknown_ones_say_so():
    for code in (0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99):
        assert code in weather.WEATHER_CODES
    assert describe_code(4) == ("날씨 정보", "🌡️")
    assert describe_code(None) == ("날씨 정보", "🌡️")
    assert line(daily(weather_code=4)).startswith("🌡️ 서울 날씨: 날씨 정보 · 최저 12°")


@pytest.mark.parametrize(
    "low,high,text",
    [
        (11.5, 22.6, "최저 12° / 최고 23°"),  # half up, not banker's rounding
        (12.4, 22.5, "최저 12° / 최고 23°"),
        (-0.4, 0.4, "최저 0° / 최고 0°"),  # never "-0°"
        (-3.5, -0.6, "최저 -4° / 최고 -1°"),
        (12, 23, "최저 12° / 최고 23°"),
    ],
)
def test_temperatures_are_rounded_to_whole_degrees(low, high, text):
    assert f" · {text} · " in line(daily(temperature_2m_min=low, temperature_2m_max=high))


@pytest.mark.parametrize(
    "chance,umbrella",
    [(0, False), (59, False), (59.4, False), (60, True), (85, True), (100, True)],
)
def test_umbrella_reminder_from_a_60_percent_chance_of_rain(chance, umbrella):
    text = line(daily(precipitation_probability_max=chance))
    assert text.endswith(" · ☔ 우산 챙기세요") is umbrella
    assert f"강수확률 {weather.round_half_up(chance)}%" in text


def test_umbrella_comes_after_the_dust_grade():
    assert line(daily(weather_code=63, precipitation_probability_max=80)) == (
        "🌧️ 서울 날씨: 비 · 최저 12° / 최고 23° · 강수확률 80% · 미세먼지 보통 · ☔ 우산 챙기세요"
    )


@pytest.mark.parametrize(
    "value,grade",
    [(0, "좋음"), (30, "좋음"), (30.1, "보통"), (31, "보통"), (80, "보통"), (81, "나쁨"), (150, "나쁨"), (151, "매우나쁨"), (420, "매우나쁨")],
)
def test_pm10_grades_follow_the_korean_standard(value, grade):
    assert pm10_grade(value) == grade


@pytest.mark.parametrize(
    "value,grade",
    [(0, "좋음"), (15, "좋음"), (15.1, "보통"), (16, "보통"), (35, "보통"), (36, "나쁨"), (75, "나쁨"), (76, "매우나쁨")],
)
def test_pm25_grades_follow_the_korean_standard(value, grade):
    assert pm25_grade(value) == grade


@pytest.mark.parametrize(
    "pm10,pm2_5,grade",
    [
        (20, 10, "좋음"),
        (20, 20, "보통"),  # PM2.5 is the worse one
        (90, 10, "나쁨"),  # PM10 is the worse one
        (160, 40, "매우나쁨"),
        (None, 40, "나쁨"),  # one alone is enough
        (90, None, "나쁨"),
        (None, None, None),
    ],
)
def test_the_dust_grade_is_the_worse_of_the_two(pm10, pm2_5, grade):
    assert dust_grade(AirQuality(pm10=pm10, pm2_5=pm2_5)) == grade
    text = format_line("서울", parse_forecast(FORECAST_JSON), AirQuality(pm10=pm10, pm2_5=pm2_5))
    assert (f"미세먼지 {grade}" in text) if grade else ("미세먼지" not in text)


def test_missing_fields_are_left_out():
    # No air quality at all.
    assert line(air=None) == "🌤️ 서울 날씨: 대체로 맑음 · 최저 12° / 최고 23° · 강수확률 10%"
    # Only a lowest temperature, no chance of rain.
    assert line(daily(temperature_2m_max=None, precipitation_probability_max=None), air=None) == (
        "🌤️ 서울 날씨: 대체로 맑음 · 최저 12°"
    )
    # No daily part: the current weather code and temperature stand in.
    current_only = {k: v for k, v in FORECAST_JSON.items() if k != "daily"}
    assert line(current_only, air=None) == "🌤️ 서울 날씨: 대체로 맑음 · 지금 14°"
    # No weather code anywhere: the description is left out.
    no_code = {**daily(weather_code=None), "current": {"temperature_2m": 14.2}}
    assert line(no_code) == "🌤️ 서울 날씨: 최저 12° / 최고 23° · 강수확률 10% · 미세먼지 보통"
    # Nulls inside the arrays (Open-Meteo sends null for missing hours/days).
    nulls = {**FORECAST_JSON, "daily": {**FORECAST_JSON["daily"], "temperature_2m_max": [None], "precipitation_probability_max": []}}
    assert line(nulls, air=None) == "🌤️ 서울 날씨: 대체로 맑음 · 최저 12°"


@pytest.mark.parametrize("payload", [{}, None, [], "not json", {"daily": None, "current": None}, {"daily": {"weather_code": ["x"]}}])
def test_odd_payloads_never_crash_and_give_the_failure_note(payload):
    forecast = parse_forecast(payload)
    assert forecast.empty
    assert format_line("서울", forecast) == FAILED
    assert parse_air_quality(payload).empty


def test_strings_and_negative_dust_values_are_handled():
    forecast = parse_forecast({"daily": {"weather_code": ["2"], "temperature_2m_min": ["9.5"], "temperature_2m_max": [True]}})
    assert forecast == Forecast(code=2, low=9.5)
    assert parse_air_quality({"current": {"pm10": -1, "pm2_5": "8"}}) == AirQuality(pm10=None, pm2_5=8.0)


def test_report_line_and_failure_note_use_the_label():
    report = WeatherReport(label="부산", forecast=parse_forecast(FORECAST_JSON))
    assert report_line(report) == "🌤️ 부산 날씨: 대체로 맑음 · 최저 12° / 최고 23° · 강수확률 10%"
    assert report_line(WeatherReport(label="부산", error="HTTP 500")) == "🌤️ 부산 날씨: 가져오지 못했어요"
    assert report_line(WeatherReport(error="HTTP 500"), slack=True) == "🌤️ *서울 날씨*: 가져오지 못했어요"


# ---------------------------------------------------------------- fetching


def test_fetch_sends_the_documented_requests_and_reads_both_answers():
    transport, requests = routes()
    report = fetch_report(env={}, transport=transport)
    assert report.ok and report.error is None and report.air_error is None
    assert report_line(report) == LINE
    forecast, air = requests
    assert (forecast.method, forecast.url.host, forecast.url.path) == ("GET", FORECAST_HOST, "/v1/forecast")
    assert dict(forecast.url.params) == {
        "latitude": "37.5665",
        "longitude": "126.9780",
        "current": "temperature_2m,weather_code",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "Asia/Seoul",
        "forecast_days": "1",
    }
    assert (air.url.host, air.url.path) == (AIR_HOST, "/v1/air-quality")
    assert dict(air.url.params) == {"latitude": "37.5665", "longitude": "126.9780", "current": "pm10,pm2_5", "timezone": "Asia/Seoul"}
    # No key or credential of any kind travels with the requests.
    for request in requests:
        assert "authorization" not in request.headers and "x-api-key" not in request.headers


def test_the_configured_place_and_timezone_are_used():
    transport, requests = routes()
    env = {"WEATHER_LABEL": "부산", "WEATHER_LAT": "35.1796", "WEATHER_LON": "129.0756", "TIMEZONE": "Asia/Seoul"}
    report = fetch_report(env=env, transport=transport)
    assert report_line(report).startswith("🌤️ 부산 날씨: ")
    assert requests[0].url.params["latitude"] == "35.1796" and requests[0].url.params["longitude"] == "129.0756"
    transport, requests = routes()
    fetch_report(env={"TIMEZONE": "Europe/Berlin"}, transport=transport)
    assert {r.url.params["timezone"] for r in requests} == {"Europe/Berlin"}
    transport, requests = routes()
    fetch_report(env={"TIMEZONE": "Mars/Olympus"}, transport=transport)  # invalid: the default zone
    assert {r.url.params["timezone"] for r in requests} == {"Asia/Seoul"}


@pytest.mark.parametrize(
    "air",
    [
        httpx.Response(500, text="oops"),
        httpx.Response(200, text="<html>maintenance</html>"),
        httpx.Response(200, json={"current": {}}),
        httpx.Response(400, json={"error": True, "reason": "Cannot initialize WeatherVariable from invalid String value pm1"}),
        broken,
    ],
)
def test_an_air_quality_failure_only_drops_the_dust_part(air):
    transport, _ = routes(air=air)
    report = fetch_report(env={}, transport=transport)
    assert report.ok and report.air is None and report.air_error
    assert report_line(report) == "🌤️ 서울 날씨: 대체로 맑음 · 최저 12° / 최고 23° · 강수확률 10%"


@pytest.mark.parametrize(
    "forecast,reason",
    [
        (httpx.Response(500, text="oops"), "HTTP 500"),
        (httpx.Response(400, json={"error": True, "reason": "Latitude must be in range of -90 to 90°."}), "HTTP 400: Latitude must be"),
        (httpx.Response(200, text="<html>login</html>"), "JSON 응답이 아님"),
        (httpx.Response(200, json={"error": True, "reason": "boom"}), "boom"),
        (httpx.Response(200, json={"daily": {}, "current": {}}), "응답에 날씨 값이 없음"),
        (broken, "연결 실패: ConnectError"),
    ],
)
def test_a_forecast_failure_is_the_short_note_and_skips_the_air_request(forecast, reason):
    transport, requests = routes(forecast=forecast)
    report = fetch_report(env={}, transport=transport)
    assert not report.ok and reason in report.error
    assert report_line(report) == FAILED
    assert [r.url.host for r in requests] == [FORECAST_HOST]


def test_a_timeout_is_reported_as_such():
    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    transport, _ = routes(forecast=slow)
    assert fetch_report(env={}, transport=transport).error == "시간 초과"


def test_the_client_has_a_short_timeout_and_nothing_raises_even_if_httpx_breaks(monkeypatch):
    timeouts = []
    real_client = httpx.Client
    transport, _ = routes()

    def client(*, transport=None, timeout=None):
        timeouts.append(timeout)
        return real_client(transport=routes_transport, timeout=timeout)

    routes_transport = transport
    monkeypatch.setattr(weather.httpx, "Client", client)
    assert weather_line({}) == LINE and timeouts == [10.0]

    def exploding_client(**kwargs):
        raise RuntimeError("httpx is broken")

    monkeypatch.setattr(weather.httpx, "Client", exploding_client)
    report = fetch_report(env={})
    assert not report.ok and "RuntimeError" in report.error
    assert weather_line({}) == FAILED
    assert slack_weather_text({}) == "🌤️ *서울 날씨*: 가져오지 못했어요"


def test_without_a_mock_the_tests_are_offline():
    # conftest makes real requests fail like a dropped connection: the line is the failure note.
    assert weather_line({}) == FAILED


# ---------------------------------------------------------------- settings


def test_weather_settings_default_to_seoul():
    cfg = config.load_weather_config({})
    assert (cfg.label, cfg.latitude, cfg.longitude, cfg.timezone_name, cfg.warnings) == ("서울", 37.5665, 126.978, "Asia/Seoul", ())
    custom = config.load_weather_config({"WEATHER_LABEL": "  우리   동네 ", "WEATHER_LAT": "35.1796", "WEATHER_LON": "129.0756"})
    assert (custom.label, custom.latitude, custom.longitude, custom.warnings) == ("우리 동네", 35.1796, 129.0756, ())
    assert config.load_weather_config({"WEATHER_LABEL": "", "WEATHER_LAT": " ", "WEATHER_LON": ""}).label == "서울"


@pytest.mark.parametrize(
    "lat,lon",
    [("abc", "126.9"), ("37.5", "동경"), ("91", "126.9"), ("37.5", "181"), ("nan", "126.9"), ("35.1", ""), ("", "129.0")],
)
def test_unusable_coordinates_fall_back_to_seoul_with_a_korean_warning(lat, lon, caplog):
    env = {"WEATHER_LABEL": "부산", "WEATHER_LAT": lat, "WEATHER_LON": lon}
    cfg = config.load_weather_config(env)
    assert (cfg.label, cfg.latitude, cfg.longitude) == ("서울", 37.5665, 126.978)
    [warning] = cfg.warnings
    assert "WEATHER_LAT/WEATHER_LON" in warning and "서울 날씨를 보여 줍니다" in warning
    transport, requests = routes()
    with caplog.at_level(logging.WARNING, logger="mungchi.weather"):
        assert weather_line(env, transport=transport) == LINE
    assert "날씨 설정: WEATHER_LAT/WEATHER_LON" in caplog.text
    assert requests[0].url.params["latitude"] == "37.5665"


@pytest.mark.parametrize(
    "value,on",
    [(None, True), ("", True), ("on", True), ("1", True), ("true", True), ("off", False), ("OFF", False), ("0", False), ("false", False), (" False ", False)],
)
def test_brief_weather_is_on_unless_turned_off(value, on):
    env = {} if value is None else {"BRIEF_WEATHER": value}
    assert config.get_brief_weather(env) is on


# ---------------------------------------------------------------- the shortcut's question


@pytest.mark.parametrize(
    "text",
    [
        "날씨",
        "날씨?",
        "날씨？",
        "날씨!",
        "오늘 날씨",
        "오늘날씨",
        "서울 날씨",
        "오늘 서울 날씨",
        "오늘 서울날씨 어때?",
        "날씨 어때",
        "날씨 어때요?",
        "날씨어때?",
        "날씨 알려줘",
        "날씨 알려 줘.",
        "오늘 날씨 확인",
        "날씨 확인해 줘",
        "날씨 확인해줘",
        "날씨 좀",
        "날씨좀",
        "  날씨  ",
        # The agreed examples.
        "날씨는?",
        "오늘의 날씨",
        "지금 날씨 어때?",
        "서울 날씨 좀 알려줘",
        "날씨 알려줄래?",
        "오늘 서울 날씨 어떄",
        "날씨 알려주세요!",
        "날씨 🙏",
        # More of the same shape.
        "현재 날씨",
        "지금 서울의 날씨는 어때요?",
        "여기 날씨 어때",
        "서울의 날씨",
        "날씨가 어떠니",
        "날씨 어떤가요?",
        "날씨 알려 줘",
        "날씨 알려줘요",
        "날씨 확인해줘",
        "날씨 보여줘",
        "날씨 궁금해",
        "날씨 좀 알려줘~",
        "날씨은?",
        "날씨...?!",
        "날씨 ☀️☔",
        "날씨 👍🏽",
        "날씨\t알려줘\n",
        "날씨 :pray:",  # Slack sends emoji as :shortcodes:
        "날씨 알려줘 :pray::skin-tone-2:",
    ],
)
def test_short_weather_questions_match(text):
    assert is_weather_query(text)
    assert is_weather_query(text, "서울") and is_weather_query(text, "부산")


def test_decomposed_hangul_is_normalized_before_matching():
    assert is_weather_query(unicodedata.normalize("NFD", "오늘 서울 날씨 어때?"))
    assert is_weather_query(unicodedata.normalize("NFD", "지금 날씨 어떄"))


def test_the_configured_label_is_a_place_too():
    assert not is_weather_query("부산 날씨")
    assert is_weather_query("부산 날씨", "부산")
    assert is_weather_query("오늘 부산의 날씨 알려줘", "부산")
    assert is_weather_query("서울 날씨", "부산")  # 서울 and 여기 always work
    assert is_weather_query("여기 날씨", "부산")
    assert not is_weather_query("대구 날씨", "부산")
    # Labels are matched like the text: spaces optional, case ignored, regex characters literal.
    assert is_weather_query("우리 동네 날씨", "우리 동네") and is_weather_query("우리동네 날씨", "우리 동네")
    assert is_weather_query("seoul 날씨 어때?", "Seoul")
    assert is_weather_query("a.b 날씨", "a.b") and not is_weather_query("axb 날씨", "a.b")
    assert weather.configured_label({"WEATHER_LABEL": " 부산 "}) == "부산"
    assert weather.configured_label({}) == "서울"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "내일 비 오면 일정 바꿔야 할까?",
        "내일 날씨 어때?",
        "부산 날씨",
        "날씨 좋으면 산책 갈까?",
        "이번 주 날씨 알려줘",
        "오늘 일정 알려줘",
        "날씨 어때 그리고 일정도",
        "크레딧",
        "<@U123> 날씨",
        # The agreed examples: these need the agent.
        "날씨 좋은 날 야외 미팅 잡아줘",
        "이번 주말 날씨에 맞춰 일정 정리해줘",
        "내일 날씨",
        "오늘 내일 날씨",
        "날씨 예보",
        "날씨 어때 내일은?",
        "🙏",
        "?!",
        ":pray:",
    ],
)
def test_longer_or_other_questions_do_not_match(text):
    assert not is_weather_query(text)
    assert not is_weather_query(text, "대구")


@pytest.mark.parametrize(
    "text",
    ["크레딧", "크레딧?", "남은 크레딧 얼마나 남았어?", "잔액", "사용량 보여줘", "credits", "Credit 확인해줘"],
)
def test_the_credit_question_is_unaffected(text):
    from mungchi import credits

    assert credits.is_credit_query(text)
    assert not is_weather_query(text)


@pytest.mark.parametrize("text", ["날씨", "날씨 알려줄래?", "서울 날씨 좀 알려줘", "날씨 🙏", "크레딧 아끼려면 어떻게 해?"])
def test_weather_questions_are_not_credit_questions(text):
    from mungchi import credits

    assert not credits.is_credit_query(text)


# ---------------------------------------------------------------- the get_weather tool (일정)


# What Open-Meteo answers with forecast_days=2: today, then tomorrow (rainy).
FORECAST_2D = {
    **FORECAST_JSON,
    "daily": {
        "time": ["2026-10-08", "2026-10-09"],
        "weather_code": [1, 63],
        "temperature_2m_max": [22.6, 17.4],
        "temperature_2m_min": [11.5, 13.2],
        "precipitation_probability_max": [10, 80],
    },
}


def use_transport(monkeypatch, transport):
    """Make the tool's own ``httpx.Client`` talk to ``transport`` (the tool takes no transport argument)."""
    real_client = httpx.Client
    monkeypatch.setattr(weather.httpx, "Client", lambda **kw: real_client(transport=transport, timeout=kw.get("timeout")))


def call_get_weather() -> dict:
    from mungchi.tools.weather_tool import get_weather

    result = asyncio.run(get_weather.handler({}))
    [content] = result["content"]
    assert content["type"] == "text"
    return json.loads(content["text"])


def test_get_weather_tool_returns_compact_json_for_today_and_tomorrow(monkeypatch):
    transport, requests = routes(forecast=httpx.Response(200, json=FORECAST_2D))
    use_transport(monkeypatch, transport)
    assert call_get_weather() == {
        "configured": True,
        "ok": True,
        "label": "서울",
        # Today's line, exactly the briefing's: tomorrow's rain does not change it.
        "summary": LINE,
        "today": {"description": "대체로 맑음", "min": 12, "max": 23, "precipitation_probability": 10},
        "tomorrow": {"description": "비", "min": 13, "max": 17, "precipitation_probability": 80},
        "current": {"temp": 14, "description": "대체로 맑음"},
        "fine_dust": "보통",
    }
    forecast, air = requests
    assert forecast.url.params["forecast_days"] == "2"
    assert (forecast.url.host, air.url.host) == (FORECAST_HOST, AIR_HOST)
    assert dict(air.url.params) == {"latitude": "37.5665", "longitude": "126.9780", "current": "pm10,pm2_5", "timezone": "Asia/Seoul"}


def test_get_weather_tool_without_tomorrow_dust_or_current_leaves_those_parts_out(monkeypatch):
    payload = {k: v for k, v in FORECAST_JSON.items() if k != "current"}  # one day, no "current"
    transport, _ = routes(forecast=httpx.Response(200, json=payload), air=httpx.Response(503, text="busy"))
    use_transport(monkeypatch, transport)
    data = call_get_weather()
    assert "tomorrow" not in data
    assert data["current"] == {"temp": None, "description": None}
    assert data["fine_dust"] is None and data["fine_dust_error"] == "HTTP 503"
    assert data["summary"] == "🌤️ 서울 날씨: 대체로 맑음 · 최저 12° / 최고 23° · 강수확률 10%"


def test_get_weather_tool_uses_the_configured_place_and_passes_on_warnings(monkeypatch):
    transport, requests = routes(forecast=httpx.Response(200, json=FORECAST_2D))
    use_transport(monkeypatch, transport)
    monkeypatch.setenv("WEATHER_LABEL", "부산")
    monkeypatch.setenv("WEATHER_LAT", "35.1796")
    monkeypatch.setenv("WEATHER_LON", "129.0756")
    data = call_get_weather()
    assert data["label"] == "부산" and data["summary"].startswith("🌤️ 부산 날씨: ")
    assert requests[0].url.params["latitude"] == "35.1796" and "warnings" not in data
    monkeypatch.setenv("WEATHER_LAT", "north")
    data = call_get_weather()
    assert data["label"] == "서울"
    [warning] = data["warnings"]
    assert warning.startswith("WEATHER_LAT/WEATHER_LON 값('north', '129.0756')을 쓸 수 없어")


@pytest.mark.parametrize(
    "forecast,error",
    [
        (broken, "연결 실패: ConnectError"),
        (httpx.Response(500, text="oops"), "HTTP 500"),
        (httpx.Response(200, json={"daily": {}, "current": {}}), "응답에 날씨 값이 없음"),
    ],
)
def test_get_weather_tool_failure_shape(monkeypatch, forecast, error):
    transport, requests = routes(forecast=forecast)
    use_transport(monkeypatch, transport)
    assert call_get_weather() == {"configured": True, "ok": False, "label": "서울", "error": error, "summary": FAILED}
    assert [r.url.host for r in requests] == [FORECAST_HOST]  # no air-quality request after a failed forecast


def test_get_weather_tool_offline_and_crashing_never_raise(monkeypatch):
    from mungchi.tools import weather_tool

    # conftest: a real request fails like a dropped connection.
    assert call_get_weather() == {
        "configured": True,
        "ok": False,
        "label": "서울",
        "error": "연결 실패: ConnectError",
        "summary": FAILED,
    }
    token = "-".join(["xoxb", "123456789012", "123456789012", "abcdefghijklmnopqrstuvwx"])
    monkeypatch.setenv("SLACK_BOT_TOKEN", token)

    def crash():
        raise RuntimeError(f"boom {token}")

    monkeypatch.setattr(weather_tool, "run_weather", crash)
    assert call_get_weather() == {"configured": True, "ok": False, "error": "RuntimeError: boom ***"}


def test_the_briefing_line_still_uses_today_only():
    # Even with two days in the answer, the line (briefing, --weather, Slack shortcut) is today's.
    assert parse_forecast(FORECAST_2D) == parse_forecast(FORECAST_JSON)
    assert format_line("서울", parse_forecast(FORECAST_2D), parse_air_quality(AIR_JSON)) == LINE
    assert parse_forecast(FORECAST_2D, day=1) == Forecast(code=63, low=13.2, high=17.4, rain_chance=80)
    assert parse_forecast(FORECAST_JSON, day=1).empty
    # Only the tool asks for tomorrow; the line's request stays at one day.
    transport, requests = routes(forecast=httpx.Response(200, json=FORECAST_2D))
    assert weather_line({}, transport=transport) == LINE
    assert requests[0].url.params["forecast_days"] == "1"
    report = fetch_report(env={}, transport=routes()[0])
    assert report.tomorrow is None


# ---------------------------------------------------------------- --weather


def test_run_weather_cli_prints_the_line():
    transport, _ = routes()
    out, err = io.StringIO(), io.StringIO()
    assert run_weather_cli({}, transport=transport, out=out, err=err) == 0
    assert out.getvalue() == LINE + "\n"
    assert err.getvalue() == ""


def test_run_weather_cli_without_dust_says_why_on_stderr():
    transport, _ = routes(air=httpx.Response(503, text="busy"))
    out, err = io.StringIO(), io.StringIO()
    assert run_weather_cli({}, transport=transport, out=out, err=err) == 0
    assert out.getvalue() == "🌤️ 서울 날씨: 대체로 맑음 · 최저 12° / 최고 23° · 강수확률 10%\n"
    assert err.getvalue() == "(미세먼지 정보는 받지 못했습니다: HTTP 503)\n"


def test_run_weather_cli_errors_go_to_stderr_with_a_hint():
    transport, _ = routes(forecast=broken)
    out, err = io.StringIO(), io.StringIO()
    assert run_weather_cli({"WEATHER_LAT": "north"}, transport=transport, out=out, err=err) == 1
    assert out.getvalue() == ""
    warning, error, hint = err.getvalue().splitlines()
    assert warning.startswith("[경고] WEATHER_LAT/WEATHER_LON 값('north', '')을 쓸 수 없어")
    assert error.startswith("[오류] 날씨를 가져오지 못했습니다 (연결 실패: ConnectError — ConnectError: connection refused)")
    assert hint == weather.CONNECT_HINT


def test_cli_weather_flag_makes_no_llm_call(monkeypatch, capsys):
    from mungchi import main as main_module

    def no_llm(*args, **kwargs):  # pragma: no cover - the test fails if called
        raise AssertionError("--weather must not start an agent")

    monkeypatch.setattr(main_module, "ClaudeSDKClient", no_llm)
    monkeypatch.setenv("WEATHER_LABEL", "서울")
    transport, requests = routes()
    real_client = httpx.Client
    monkeypatch.setattr(weather.httpx, "Client", lambda **kw: real_client(transport=transport, timeout=kw.get("timeout")))
    assert main(["--weather"]) == 0
    out, err = capsys.readouterr()
    assert out == LINE + "\n" and err == ""
    assert len(requests) == 2


def test_cli_weather_help_and_conflicts(capsys):
    help_text = build_parser().format_help()
    assert "--weather" in help_text and "python -m mungchi --weather" in help_text and "Open-Meteo" in help_text
    for argv in (["--weather", "질문"], ["--weather", "--brief"], ["--weather", "--credits"], ["--weather", "--list-models"], ["slack", "--weather"]):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2
    assert "--weather는 질문이나 다른 옵션" in capsys.readouterr().err
