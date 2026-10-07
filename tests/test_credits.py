"""Chat KHU credits (``--credits``, parsing, formatting) against a mocked httpx transport (no network)."""

from __future__ import annotations

import io
import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from mungchi import config, credits
from mungchi.credits import (
    UNSUPPORTED_TEXT,
    Balance,
    Bucket,
    approx,
    billing_period,
    fetch_report,
    fmt_number,
    format_summary,
    gateway_root,
    is_credit_query,
    parse_balance,
    parse_usage,
    projection,
    run_credits_cli,
    slack_credit_text,
)
from mungchi.main import build_parser, main

SEOUL = ZoneInfo("Asia/Seoul")
GATEWAY = "https://factchat-cloud.mindlogic.ai/v1/gateway/claude"
ROOT = "https://factchat-cloud.mindlogic.ai/v1/gateway"
TOKEN = "gw-credit-test-token-0123456789abcdef"
# Assembled at runtime so no key-shaped literal is committed.
API_KEY = "-".join(["sk", "ant", "api03", "Q" * 32])

# Exactly what the gateway returned on the user's Mac.
CREDITS_JSON = {
    "object": "credit_balance",
    "monthly_allocated": {
        "quota": 10000.0,
        "used": 949.488154,
        "remaining": 9050.51,
        "renewal_date": "2026-11-01T00:00:00+09:00",
    },
    "purchased": {"quota": 0, "used": 0, "remaining": 0},
    "org_granted": {"quota": 0, "used": 0, "remaining": 0},
    "total": {"quota": 10000.0, "used": 949.49, "remaining": 9050.51},
}
USAGE_JSON = {
    "object": "usage_summary",
    "start_date": "2026-10-01",
    "end_date": "2026-10-07",
    "timezone": "Asia/Seoul",
    "group_by": "model",
    "scope": "key",
    "total": {"call_count": 94, "credits": 893.9},
    "rows": [
        {"key": "claude-sonnet-5", "call_count": 71, "credits": 536.7},
        {"key": "claude-opus-5-5", "call_count": 23, "credits": 357.2},
    ],
}
# Exactly 7 days into the cycle that started 2026-10-01 00:00 (Seoul).
SEVEN_DAYS_IN = datetime(2026, 10, 8, 0, 0, tzinfo=SEOUL)
EXPECTED_SUMMARY = "\n".join(
    [
        "💳 Chat KHU 크레딧: 9,050.5 남음 / 10,000 (90.5%) · 11/01 갱신",
        "이번 달 사용: 949.5 (10/01–10/07, 94회)",
        "· claude-sonnet-5: 71회 · 536.7",
        "· claude-opus-5-5: 23회 · 357.2",
        "이 속도면 이번 달 약 4,200 사용 예상 (한도의 42%)",
    ]
)


def gateway_env(**extra):
    return {"ANTHROPIC_BASE_URL": GATEWAY, "ANTHROPIC_AUTH_TOKEN": TOKEN, **extra}


def routes(credits_response=None, usage_response=None):
    """A transport answering /credits/ and /usage/, recording every request."""
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/credits/"):
            return credits_response or httpx.Response(200, json=CREDITS_JSON)
        if request.url.path.endswith("/usage/"):
            return usage_response or httpx.Response(200, json=USAGE_JSON)
        return httpx.Response(404, json={"error": "unknown path"})

    return httpx.MockTransport(handle), requests


def summary(balance_json=CREDITS_JSON, usage_json=USAGE_JSON, *, now=SEVEN_DAYS_IN, slack=False):
    usage = parse_usage(usage_json) if usage_json is not None else None
    return format_summary(parse_balance(balance_json), usage, now=now, tz=SEOUL, slack=slack)


# ---------------------------------------------------------------- parsing


def test_parses_the_exact_gateway_json():
    balance = parse_balance(CREDITS_JSON)
    assert balance.total == Bucket(quota=10000.0, used=949.49, remaining=9050.51)
    assert balance.monthly == Bucket(quota=10000.0, used=949.488154, remaining=9050.51)
    assert balance.purchased == Bucket(quota=0.0, used=0.0, remaining=0.0)
    assert not balance.purchased.granted and not balance.org_granted.granted
    assert balance.renewal == datetime(2026, 11, 1, tzinfo=SEOUL)

    usage = parse_usage(USAGE_JSON)
    assert (usage.start, usage.end) == (date(2026, 10, 1), date(2026, 10, 7))
    assert (usage.calls, usage.credits) == (94, 893.9)
    assert [(r.key, r.calls, r.credits) for r in usage.rows] == [
        ("claude-sonnet-5", 71, 536.7),
        ("claude-opus-5-5", 23, 357.2),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        None,
        [],
        "not json at all",
        {"total": None, "monthly_allocated": None},
        {"total": "lots", "monthly_allocated": {"quota": None, "renewal_date": 20261101}},
        {"total": {"quota": True, "used": [], "remaining": {}}, "monthly_allocated": {"renewal_date": "next month"}},
    ],
)
def test_missing_or_odd_balance_fields_never_crash(payload):
    balance = parse_balance(payload)
    assert balance.total == Bucket()
    assert balance.renewal is None
    text = format_summary(balance, parse_usage(payload), now=SEVEN_DAYS_IN, tz=SEOUL)
    assert text == "💳 Chat KHU 크레딧: 남은 양을 알 수 없음"


def test_strings_and_partial_fields_are_understood():
    balance = parse_balance(
        {
            "monthly_allocated": {"quota": "10,000", "used": "949.49", "renewal_date": "2026-11-01T00:00:00Z"},
            "purchased": {"quota": "1000", "remaining": "800"},
            # no "total": summed from the sources
        }
    )
    assert balance.monthly == Bucket(quota=10000.0, used=949.49, remaining=10000.0 - 949.49)
    assert balance.purchased == Bucket(quota=1000.0, used=200.0, remaining=800.0)
    assert balance.total.quota == 11000.0 and balance.total.used == pytest.approx(1149.49)
    assert balance.renewal == datetime(2026, 11, 1, tzinfo=timezone.utc)

    usage = parse_usage(
        {
            "start_date": "2026-10-01",
            "end_date": None,
            "total": {"call_count": "94"},
            "rows": [
                {"key": "claude-sonnet-5", "call_count": "71", "credits": "536.7"},
                {"call_count": None, "credits": "x"},
                "garbage",
                {"key": "  claude\n opus  ", "call_count": -3, "credits": 1.25},
            ],
        }
    )
    assert usage.calls == 94 and usage.end is None
    assert usage.credits == pytest.approx(537.95)  # summed from the rows
    assert [(r.key, r.calls, r.credits) for r in usage.rows] == [
        ("claude-sonnet-5", 71, 536.7),
        ("(이름 없음)", None, None),
        ("(이름 없음)", None, None),
        ("claude opus", None, 1.25),
    ]


# ---------------------------------------------------------------- formatting


def test_formatter_matches_the_agreed_korean_summary():
    assert summary() == EXPECTED_SUMMARY


def test_slack_variant_uses_mrkdwn_bold_and_bullets():
    text = summary(slack=True)
    assert text.splitlines()[0] == "💳 *Chat KHU 크레딧*: 9,050.5 남음 / 10,000 (90.5%) · 11/01 갱신"
    assert "• claude-sonnet-5: 71회 · 536.7" in text and "· claude-sonnet-5" not in text


@pytest.mark.parametrize(
    "value,text",
    [
        (9050.51, "9,050.5"),
        (10000.0, "10,000"),
        (949.488154, "949.5"),
        (0, "0"),
        (0.04, "0"),
        (0.05, "0.1"),
        (1234567.25, "1,234,567.3"),
        (999.96, "1,000"),
        (-12.0, "-12"),
    ],
)
def test_numbers_have_separators_one_decimal_and_no_trailing_zero(value, text):
    assert fmt_number(value) == text


def test_approx_rounds_estimates_to_a_sensible_step():
    assert approx(4204.88) == 4200
    assert approx(523.4) == 520
    assert approx(57.4) == 57


def test_projection_needs_a_full_day_and_uses_the_renewal_cycle():
    balance, usage = parse_balance(CREDITS_JSON), parse_usage(USAGE_JSON)
    assert billing_period(balance, usage, SEOUL) == (datetime(2026, 10, 1, tzinfo=SEOUL), datetime(2026, 11, 1, tzinfo=SEOUL))
    projected, share = projection(balance, usage, SEVEN_DAYS_IN, SEOUL)
    assert projected == pytest.approx(949.49 / 7 * 31)
    assert share == pytest.approx(949.49 / 7 * 31 / 100)

    # Less than one full day into the cycle: no projection line at all.
    early = datetime(2026, 10, 1, 23, 0, tzinfo=SEOUL)
    assert projection(balance, usage, early, SEOUL) is None
    assert "사용 예상" not in summary(now=early)
    # Exactly one day: shown. After the renewal date (stale data): not shown.
    assert "사용 예상" in summary(now=datetime(2026, 10, 2, 0, 0, tzinfo=SEOUL))
    assert "사용 예상" not in summary(now=datetime(2026, 11, 1, 0, 0, tzinfo=SEOUL))
    # Nothing used yet: nothing to project.
    unused = {**CREDITS_JSON, "total": {"quota": 10000.0, "used": 0, "remaining": 10000.0}}
    assert projection(parse_balance(unused), usage, SEVEN_DAYS_IN, SEOUL) is None


def test_projection_falls_back_to_the_usage_start_date_without_a_renewal_date():
    no_renewal = {**CREDITS_JSON, "monthly_allocated": {"quota": 10000.0, "used": 949.488154, "remaining": 9050.51}}
    balance, usage = parse_balance(no_renewal), parse_usage(USAGE_JSON)
    assert billing_period(balance, usage, SEOUL) == (datetime(2026, 10, 1, tzinfo=SEOUL), datetime(2026, 11, 1, tzinfo=SEOUL))
    text = summary(no_renewal)
    assert text.splitlines()[0] == "💳 Chat KHU 크레딧: 9,050.5 남음 / 10,000 (90.5%)"  # no "갱신"
    assert text.endswith("이 속도면 이번 달 약 4,200 사용 예상 (한도의 42%)")
    assert billing_period(balance, None, SEOUL) is None


def test_month_end_renewal_dates_are_clamped():
    balance = parse_balance({**CREDITS_JSON, "monthly_allocated": {**CREDITS_JSON["monthly_allocated"], "renewal_date": "2026-03-31T00:00:00+09:00"}})
    start, end = billing_period(balance, None, SEOUL)
    assert start == datetime(2026, 2, 28, tzinfo=SEOUL) and end == datetime(2026, 3, 31, tzinfo=SEOUL)


def test_zero_sections_are_hidden_and_granted_ones_are_shown():
    assert "구매" not in summary() and "기관" not in summary() and "월 기본" not in summary()
    extra = {
        **CREDITS_JSON,
        "purchased": {"quota": 1000, "used": 200, "remaining": 800},
        "org_granted": {"quota": 0, "used": 0, "remaining": 0},
        "total": {"quota": 11000.0, "used": 1149.49, "remaining": 9850.51},
    }
    lines = summary(extra).splitlines()
    assert lines[0] == "💳 Chat KHU 크레딧: 9,850.5 남음 / 11,000 (89.6%) · 11/01 갱신"
    assert lines[1:3] == ["월 기본 크레딧: 9,050.5 남음 / 10,000", "구매 크레딧: 800 남음 / 1,000"]
    assert not any("기관" in line for line in lines)


def test_top_five_models_by_credits_then_a_count_of_the_rest():
    rows = [{"key": f"model-{i}", "call_count": i, "credits": float(i * 10)} for i in range(1, 8)]
    rows.append({"key": "no-credits", "call_count": 999})
    text = summary(usage_json={**USAGE_JSON, "rows": rows})
    listed = [line for line in text.splitlines() if line.startswith("· ")]
    assert listed == [
        "· model-7: 7회 · 70",
        "· model-6: 6회 · 60",
        "· model-5: 5회 · 50",
        "· model-4: 4회 · 40",
        "· model-3: 3회 · 30",
        "· 외 3개 모델",
    ]


def test_without_usage_the_balance_still_reads_well():
    text = summary(usage_json=None)
    assert text.splitlines()[:2] == [
        "💳 Chat KHU 크레딧: 9,050.5 남음 / 10,000 (90.5%) · 11/01 갱신",
        "이번 달 사용: 949.5",
    ]


# ---------------------------------------------------------------- gateway root


@pytest.mark.parametrize(
    "base",
    [GATEWAY, GATEWAY + "/", ROOT, ROOT + "/", "https://FACTCHAT-cloud.Mindlogic.ai/v1/gateway/Claude/"],
)
def test_gateway_root_is_derived_from_the_claude_base_url(base):
    assert gateway_root({"ANTHROPIC_BASE_URL": base}).lower() == ROOT.lower()


def test_credits_api_base_wins_and_other_hosts_are_not_supported():
    assert gateway_root({"ANTHROPIC_BASE_URL": GATEWAY, "CREDITS_API_BASE": "https://credits.example.org/api/"}) == (
        "https://credits.example.org/api"
    )
    assert gateway_root({"CREDITS_API_BASE": "https://credits.example.org/api"}) == "https://credits.example.org/api"
    for base in ("", "https://api.anthropic.com", "https://evil-mindlogic.ai.example.com/claude", "https://notmindlogic.ai/claude"):
        assert gateway_root({"ANTHROPIC_BASE_URL": base}) is None, base


def test_unsupported_gateway_says_so_in_korean_without_any_request():
    transport, requests = routes()
    report = fetch_report({"ANTHROPIC_BASE_URL": "https://api.anthropic.com", "ANTHROPIC_API_KEY": API_KEY}, transport=transport)
    assert not report.ok and not report.supported
    assert report.error == UNSUPPORTED_TEXT == "크레딧 조회는 Chat KHU(Mindlogic) 게이트웨이에서만 됩니다."
    assert requests == []


# ---------------------------------------------------------------- fetching


def test_fetch_sends_the_key_as_bearer_and_x_api_key_to_both_endpoints():
    transport, requests = routes()
    report = fetch_report(gateway_env(), transport=transport)
    assert report.ok and report.usage_error is None
    assert [str(r.url) for r in requests] == [ROOT + "/credits/", ROOT + "/usage/"]
    for request in requests:
        assert request.method == "GET"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["x-api-key"] == TOKEN


def test_api_key_is_the_fallback_credential():
    transport, requests = routes()
    env = {"ANTHROPIC_BASE_URL": GATEWAY, "ANTHROPIC_API_KEY": API_KEY}
    assert fetch_report(env, transport=transport).ok
    assert requests[0].headers["authorization"] == f"Bearer {API_KEY}"


def test_no_key_is_a_korean_message_and_no_request():
    transport, requests = routes()
    report = fetch_report({"ANTHROPIC_BASE_URL": GATEWAY}, transport=transport)
    assert not report.ok and report.supported
    assert report.error.splitlines() == ["크레딧을 확인할 키가 없습니다.", credits.KEY_HINT]
    assert requests == []


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_name_the_status_and_point_to_the_env_key(status):
    body = {"error": {"message": f"invalid key {TOKEN}"}}
    transport, _ = routes(credits_response=httpx.Response(status, json=body))
    report = fetch_report(gateway_env(), transport=transport)
    assert not report.ok
    assert report.error.splitlines() == [
        f"크레딧을 확인하지 못했습니다 (HTTP {status}).",
        "→ .env의 ANTHROPIC_AUTH_TOKEN(게이트웨이 키)을 확인하세요.",
    ]
    assert TOKEN not in report.error


def test_network_errors_are_short_scrubbed_korean_messages():
    def broken(request):
        raise httpx.ConnectError(f"connection refused while sending Bearer {TOKEN}", request=request)

    report = fetch_report(gateway_env(), transport=httpx.MockTransport(broken))
    assert not report.ok
    first, hint = report.error.splitlines()
    assert first.startswith("크레딧을 확인하지 못했습니다 (연결 실패: ConnectError")
    assert hint == credits.CONNECT_HINT
    assert TOKEN not in report.error


def test_a_failed_usage_request_still_shows_the_balance():
    transport, _ = routes(usage_response=httpx.Response(500, text="oops"))
    report = fetch_report(gateway_env(), transport=transport)
    assert report.ok and report.usage is None and report.usage_error == "HTTP 500"
    text = credits.summary_text(report, now=SEVEN_DAYS_IN)
    assert text.startswith("💳 Chat KHU 크레딧: 9,050.5 남음")
    assert "(사용 내역은 받지 못했습니다: HTTP 500)" in text


def test_non_json_balance_is_explained():
    transport, _ = routes(credits_response=httpx.Response(200, text="<html>login</html>"))
    report = fetch_report(gateway_env(), transport=transport)
    assert report.error.splitlines()[0] == "크레딧을 확인하지 못했습니다 (HTTP 200이지만 JSON 응답이 아님)."


def test_slack_text_is_the_mrkdwn_summary_or_a_warning():
    transport, _ = routes()
    assert slack_credit_text(gateway_env(), transport=transport, now=SEVEN_DAYS_IN) == summary(slack=True)
    transport, _ = routes(credits_response=httpx.Response(401, json={}))
    assert slack_credit_text(gateway_env(), transport=transport).startswith("⚠️ 크레딧을 확인하지 못했습니다 (HTTP 401).")


# ---------------------------------------------------------------- the shortcut's question


@pytest.mark.parametrize(
    "text",
    [
        "크레딧",
        "크레딧?",
        "크레딧 확인",
        "크레딧 확인해 줘",
        "크레딧 알려줘",
        "크레딧 알려 줘!",
        "크레딧 얼마",
        "크레딧 얼마나 남았어?",
        "크레딧얼마남았나",
        "남은 크레딧",
        "남은 크레딧 보여줘",
        "잔액",
        "잔액？",
        "잔여 크레딧",
        "잔여크레딧 확인",
        "사용량",
        "사용량 보여줘.",
        "credits",
        "Credit?",
        "CREDITS 알려줘",
        "  크레딧  ",
        # The user calls the credits "토큰".
        "토큰",
        "토큰?",
        "남은 토큰",
        "토큰 얼마",
        "토큰 얼마나 남았어",
        "토큰 얼마나 남았어?",
        "토큰 사용량",
        "토큰 확인",
        "잔여 토큰",
        "잔여토큰 알려줘",
        "토큰 🙏",
        "토큰 :pray:",
    ],
)
def test_short_credit_questions_match(text):
    assert is_credit_query(text)


def test_decomposed_hangul_is_normalized_before_matching():
    import unicodedata

    assert is_credit_query(unicodedata.normalize("NFD", "남은 크레딧 얼마나 남았어?"))


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "크레딧 아끼려면 어떻게 해?",
        "크레딧이 뭐야?",
        "크레딧 많이 쓰는 모델이 뭐야",
        "어제 공저자들이 뭐 고쳤어?",
        "사용량 줄이는 법",
        "오늘 일정 알려줘",
        "credits please explain how billing works",
        "<@U123> 크레딧",
        "토큰 아끼려면 어떻게 해?",
        "토큰이 뭐야?",
        "토큰 가격 알려줘",
        "날씨랑 토큰",  # both at once: the combined shortcut (quick_info), not this one
        None,
    ],
)
def test_longer_questions_do_not_match(text):
    assert not is_credit_query(text)


# ---------------------------------------------------------------- 고뭉치's get_credits tool


def use_transport(monkeypatch, transport):
    """Make the tool's own ``httpx.Client`` talk to ``transport`` (the tool takes no transport argument)."""
    real_client = httpx.Client
    monkeypatch.setattr(credits.httpx, "Client", lambda **kw: real_client(transport=transport, timeout=kw.get("timeout")))


def call_get_credits() -> dict:
    import asyncio

    from mungchi.tools.credits_tool import get_credits

    result = asyncio.run(get_credits.handler({}))
    [content] = result["content"]
    assert content["type"] == "text"
    return json.loads(content["text"])


def test_get_credits_tool_returns_compact_json_with_the_summary(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", GATEWAY)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(credits, "datetime", _Pinned)
    transport, requests = routes()
    use_transport(monkeypatch, transport)
    data = call_get_credits()
    assert data == {
        "configured": True,
        "ok": True,
        "summary": EXPECTED_SUMMARY,  # the same Korean text as --credits
        "total": {"quota": 10000.0, "used": 949.5, "remaining": 9050.5, "remaining_percent": 90.5},
        "monthly": {"quota": 10000.0, "used": 949.5, "remaining": 9050.5},
        "renewal_date": "2026-11-01",
        "usage": {
            "start": "2026-10-01",
            "end": "2026-10-07",
            "calls": 94,
            "credits": 893.9,
            "models": [
                {"model": "claude-sonnet-5", "calls": 71, "credits": 536.7},
                {"model": "claude-opus-5-5", "calls": 23, "credits": 357.2},
            ],
            "other_models": 0,
        },
        "projection": {"credits": 4200, "percent_of_quota": 42},
    }
    # Only the two read-only credit endpoints; the key only in headers, never in the result.
    assert [r.url.path for r in requests] == ["/v1/gateway/credits/", "/v1/gateway/usage/"]
    assert TOKEN not in json.dumps(data, ensure_ascii=False)


def test_get_credits_tool_shows_other_sources_and_a_missing_usage_part(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", GATEWAY)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", TOKEN)
    balance = {**CREDITS_JSON, "purchased": {"quota": 500, "used": 100, "remaining": 400}}
    transport, _ = routes(credits_response=httpx.Response(200, json=balance), usage_response=httpx.Response(500, text="oops"))
    use_transport(monkeypatch, transport)
    data = call_get_credits()
    assert data["ok"] is True and data["purchased"] == {"quota": 500.0, "used": 100.0, "remaining": 400.0}
    assert "org_granted" not in data and "usage" not in data
    assert data["usage_error"] == "HTTP 500"
    assert "(사용 내역은 받지 못했습니다: HTTP 500)" in data["summary"]


@pytest.mark.parametrize(
    "env,expected",
    [
        (
            {"ANTHROPIC_API_KEY": API_KEY},
            {"configured": False, "ok": False, "error": UNSUPPORTED_TEXT, "hint": credits.UNSUPPORTED_HINT},
        ),
        (
            {"ANTHROPIC_BASE_URL": GATEWAY},
            {
                "configured": False,
                "ok": False,
                "missing": ["ANTHROPIC_AUTH_TOKEN"],
                "error": credits.NO_KEY_TEXT,
                "hint": credits.KEY_HINT,
            },
        ),
    ],
)
def test_get_credits_tool_without_a_gateway_or_key_says_what_to_set(monkeypatch, env, expected):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(credits.httpx, "Client", lambda **kw: pytest.fail("no request without a gateway and key"))
    data = call_get_credits()
    assert data == expected
    assert API_KEY not in json.dumps(data, ensure_ascii=False)


@pytest.mark.parametrize(
    "response,first_line",
    [
        (httpx.Response(401, json={"detail": TOKEN}), "크레딧을 확인하지 못했습니다 (HTTP 401)."),
        (httpx.Response(200, text="<html>login</html>"), "크레딧을 확인하지 못했습니다 (HTTP 200이지만 JSON 응답이 아님)."),
    ],
)
def test_get_credits_tool_failure_is_a_short_korean_error(monkeypatch, response, first_line):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", GATEWAY)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", TOKEN)
    transport, requests = routes(credits_response=response)
    use_transport(monkeypatch, transport)
    data = call_get_credits()
    assert set(data) == {"configured", "ok", "error"}
    assert data["configured"] is True and data["ok"] is False
    assert data["error"].splitlines()[0] == first_line
    assert TOKEN not in data["error"]
    assert [r.url.path for r in requests] == ["/v1/gateway/credits/"]  # no usage request after a failed balance


def test_get_credits_tool_offline_and_crashing_never_raise(monkeypatch):
    from mungchi.tools import credits_tool

    monkeypatch.setenv("ANTHROPIC_BASE_URL", GATEWAY)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", TOKEN)
    # conftest: a real request fails like a dropped connection.
    data = call_get_credits()
    assert data["ok"] is False and data["error"].startswith("크레딧을 확인하지 못했습니다 (연결 실패: ConnectError")

    def crash():
        raise RuntimeError(f"boom {TOKEN}")

    monkeypatch.setattr(credits_tool, "run_credits", crash)
    assert call_get_credits() == {"configured": True, "ok": False, "error": "RuntimeError: boom ***"}


# ---------------------------------------------------------------- --credits


class _Pinned(datetime):
    @classmethod
    def now(cls, tz=None):
        return SEVEN_DAYS_IN.astimezone(tz) if tz is not None else SEVEN_DAYS_IN


def test_run_credits_cli_prints_the_summary_and_never_the_key():
    transport, _ = routes()
    out, err = io.StringIO(), io.StringIO()
    assert run_credits_cli(gateway_env(), transport=transport, now=SEVEN_DAYS_IN, out=out, err=err) == 0
    assert out.getvalue() == EXPECTED_SUMMARY + "\n"
    assert err.getvalue() == ""


def test_run_credits_cli_errors_go_to_stderr_with_a_hint():
    transport, _ = routes(credits_response=httpx.Response(401, json={"detail": TOKEN}))
    out, err = io.StringIO(), io.StringIO()
    assert run_credits_cli(gateway_env(), transport=transport, out=out, err=err) == 1
    assert out.getvalue() == ""
    assert err.getvalue().splitlines() == [
        "[오류] 크레딧을 확인하지 못했습니다 (HTTP 401).",
        "→ .env의 ANTHROPIC_AUTH_TOKEN(게이트웨이 키)을 확인하세요.",
    ]
    assert TOKEN not in err.getvalue()

    out, err = io.StringIO(), io.StringIO()
    assert run_credits_cli({"ANTHROPIC_API_KEY": API_KEY}, out=out, err=err) == 1
    assert err.getvalue().splitlines() == ["[오류] " + UNSUPPORTED_TEXT, credits.UNSUPPORTED_HINT]
    assert API_KEY not in err.getvalue()


def test_cli_credits_flag_uses_dotenv_settings_and_makes_no_llm_call(monkeypatch, capsys):
    from mungchi import main as main_module

    def no_llm(*args, **kwargs):  # pragma: no cover - the test fails if called
        raise AssertionError("--credits must not start an agent")

    monkeypatch.setattr(main_module, "ClaudeSDKClient", no_llm)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", GATEWAY)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(credits, "datetime", _Pinned)
    transport, requests = routes()
    real_client = httpx.Client
    timeouts = []

    def client_with_mock_transport(*, transport=None, timeout=None):
        timeouts.append(timeout)
        return real_client(transport=routes_transport, timeout=timeout)

    routes_transport = transport
    monkeypatch.setattr(credits.httpx, "Client", client_with_mock_transport)

    assert main(["--credits"]) == 0
    out, err = capsys.readouterr()
    assert out == EXPECTED_SUMMARY + "\n"
    assert TOKEN not in out + err
    assert len(requests) == 2 and timeouts == [15.0]


def test_cli_credits_help_and_conflicts(capsys):
    help_text = build_parser().format_help()
    assert "--credits" in help_text and "python -m mungchi --credits" in help_text
    for argv in (["--credits", "질문"], ["--credits", "--brief"], ["--credits", "--list-models"], ["--credits", "--dropbox-check"]):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2
    assert "--credits는 질문이나 다른 옵션" in capsys.readouterr().err


# ---------------------------------------------------------------- alert settings


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, 10.0),
        ({"CREDIT_ALERT_PERCENT": "15"}, 15.0),
        ({"CREDIT_ALERT_PERCENT": " 7.5% "}, 7.5),
        ({"CREDIT_ALERT_PERCENT": ""}, 0.0),
        ({"CREDIT_ALERT_PERCENT": "0"}, 0.0),
        ({"CREDIT_ALERT_PERCENT": "-3"}, 0.0),
        ({"CREDIT_ALERT_PERCENT": "250"}, 100.0),
        ({"CREDIT_ALERT_PERCENT": "열"}, 10.0),
    ],
)
def test_credit_alert_percent_setting(env, expected):
    assert config.get_credit_alert_percent(env) == expected


def test_alert_text_is_a_korean_warning_then_the_summary():
    report = credits.CreditReport(
        balance=parse_balance({**CREDITS_JSON, "total": {"quota": 10000.0, "used": 9150.0, "remaining": 850.0}}),
        usage=parse_usage(USAGE_JSON),
    )
    text = credits.alert_text(report, 10.0, now=SEVEN_DAYS_IN)
    first, second, blank, header, *_ = text.splitlines()
    assert first == "⚠️ *Chat KHU 크레딧이 얼마 남지 않았어요* (남은 비율 8.5%, 알림 기준 10%)"
    assert "한 번만" in second and blank == ""
    assert header == "💳 *Chat KHU 크레딧*: 850 남음 / 10,000 (8.5%) · 11/01 갱신"
    assert credits.alert_period(report.balance, SEVEN_DAYS_IN) == "2026-11-01T00:00:00+09:00"
    assert credits.alert_period(Balance(total=Bucket()), SEVEN_DAYS_IN) == "month:2026-10"


def test_state_keeps_the_credit_alert_next_to_the_dropbox_checkpoint(tmp_path):
    from mungchi.state import StateStore

    store = StateStore(tmp_path / "state.json")
    assert store.credit_alert_period() is None
    store.mark_checked("dropbox", SEVEN_DAYS_IN)
    store.mark_credit_alert("2026-11-01T00:00:00+09:00", SEVEN_DAYS_IN)
    assert store.credit_alert_period() == "2026-11-01T00:00:00+09:00"
    assert store.last_checked("dropbox") == SEVEN_DAYS_IN
    data = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert set(data) == {"last_checked", "credit_alert"}
