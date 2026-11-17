"""Chat KHU (Mindlogic gateway) credits, without any LLM call.

``python -m mungchi --credits``, the Slack shortcut ("@고뭉치 크레딧", "토큰"),
고뭉치's ``get_credits`` tool (``credit_payload``: compact JSON) and the
running bots' low-credit alert all go through here. The briefing, the
Slack shortcut and the alert show the short summary (two lines: the
balance, then this month's use and the projection); ``--credits`` and a
detailed Slack request ("크레딧 자세히", "토큰 내역") the detailed one, with
the call count, the dates and the models. Only two read-only
gateway endpoints are called, ``GET {root}/credits/`` and ``GET {root}/usage/``;
no model is used, so checking costs nothing. The key travels only in request
headers, and every message that leaves this module is scrubbed.

``{root}`` is ``CREDITS_API_BASE`` when set, otherwise ``ANTHROPIC_BASE_URL``
without its trailing ``/claude`` (``https://factchat-cloud.mindlogic.ai/v1/gateway``),
and only for a ``*.mindlogic.ai`` host: other gateways (and Anthropic's own
API) have no such endpoints.
"""

from __future__ import annotations

import calendar
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, tzinfo
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping, TextIO
from urllib.parse import urlsplit

import httpx

from . import config
from .model_list import SSL_HINT
from .quick_info import query_text
from .tools.common import safe_error, scrub

TIMEOUT_SECONDS = 15.0
MINDLOGIC_DOMAIN = "mindlogic.ai"
CLAUDE_PATH_SUFFIX = "/claude"
CREDITS_PATH = "/credits/"
USAGE_PATH = "/usage/"
# Models listed by name under the usage line (by credits, highest first).
MAX_MODEL_ROWS = 5
MAX_MODEL_KEY_CHARS = 60
# The projection is shown only once this much of the cycle has passed.
MIN_PROJECTION_DAYS = 1.0
SECONDS_PER_DAY = 86_400

UNSUPPORTED_TEXT = "크레딧 조회는 Chat KHU(Mindlogic) 게이트웨이에서만 됩니다."
UNSUPPORTED_HINT = (
    "→ .env의 ANTHROPIC_BASE_URL이 Chat KHU 주소(https://factchat-cloud.mindlogic.ai/v1/gateway/claude)인지 "
    "확인하세요. 다른 주소의 크레딧 API를 쓰려면 CREDITS_API_BASE에 적으세요."
)
NO_KEY_TEXT = "크레딧을 확인할 키가 없습니다."
KEY_HINT = "→ .env의 ANTHROPIC_AUTH_TOKEN(게이트웨이 키)을 확인하세요."
HTTP_ERROR_TEXT = "크레딧을 확인하지 못했습니다 (HTTP {status})."
CONNECT_ERROR_TEXT = "크레딧을 확인하지 못했습니다 (연결 실패: {detail})."
CONNECT_HINT = "→ 인터넷 연결과 ANTHROPIC_BASE_URL(또는 CREDITS_API_BASE) 주소를 확인하세요."
NOT_JSON_TEXT = "크레딧을 확인하지 못했습니다 (HTTP 200이지만 JSON 응답이 아님)."
ADDRESS_HINT = "→ 크레딧 API 주소를 확인하세요 (CREDITS_API_BASE, 또는 ANTHROPIC_BASE_URL에서 끝의 /claude를 뺀 주소)."
READ_ERROR_TEXT = "크레딧 응답을 읽지 못했습니다 ({kind})."
STATUS_HINTS = {401: KEY_HINT, 403: KEY_HINT, 404: ADDRESS_HINT}
USAGE_MISSING_TEXT = "(사용 내역은 받지 못했습니다: {reason})"

# A short message that only asks for the credits ("크레딧", "남은 크레딧 얼마나 남았어?",
# "credits"), matched after the bot's mention is removed and the text is
# normalized like the weather question (``quick_info.query_text``: NFC, lower
# case, single spaces, trailing punctuation and emoji removed). The user calls
# the credits "토큰", so "남은 토큰", "토큰 얼마나 남았어", "토큰 사용량" match too.
# Longer questions ("크레딧 아끼려면 어떻게 해?", "토큰이 뭐야?") do not match
# and go to the agent as before.
#
#   [남은 | 잔여] (크레딧 | 토큰 | 잔액 | 사용량 | credit(s)) [사용량 | 잔액] [확인(해 줘) | 알려 줘 | 얼마(나) (남았어) | 보여 줘]
CREDIT_QUERY_RE = re.compile(
    r"^(?:(?:남은|잔여) ?)?(?:크레딧|토큰|잔액|사용량|credits?)(?: ?(?:사용량|잔액))?"
    r"(?: ?(?:확인(?: ?해 ?줘)?|알려 ?줘|얼마(?:나|야)?(?: ?남았(?:어|나|니|지))?|보여 ?줘))?$"
)


def is_credit_query(text: str | None) -> bool:
    """True when ``text`` (mention already removed) is just a short credit question. Pure."""
    normalized = query_text(text)
    return bool(normalized) and CREDIT_QUERY_RE.match(normalized) is not None


# ---------------------------------------------------------------- gateway root


def _is_mindlogic(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host == MINDLOGIC_DOMAIN or host.endswith("." + MINDLOGIC_DOMAIN)


def gateway_root(env: Mapping[str, str] | None = None) -> str | None:
    """The gateway API root the credit endpoints hang off, or None when not supported.

    ``CREDITS_API_BASE`` wins. Otherwise ``ANTHROPIC_BASE_URL`` with a
    trailing ``/claude`` (and slashes) removed, but only for a
    ``*.mindlogic.ai`` host.
    """
    explicit = config.get_credits_api_base(env)
    if explicit:
        return explicit
    base = config.get_anthropic_base_url(env)  # Anthropic's own API when unset: not supported
    if not _is_mindlogic(base):
        return None
    if base.lower().endswith(CLAUDE_PATH_SUFFIX):
        base = base[: -len(CLAUDE_PATH_SUFFIX)]
    return base.rstrip("/") or None


def credentials(env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """``(key, env var name)``: ``ANTHROPIC_AUTH_TOKEN``, else ``ANTHROPIC_API_KEY``; ``("", "")`` if neither."""
    token = config.get_anthropic_auth_token(env)
    if token:
        return token, "ANTHROPIC_AUTH_TOKEN"
    key = config.get_anthropic_api_key(env)
    if key:
        return key, "ANTHROPIC_API_KEY"
    return "", ""


def request_headers(key: str) -> dict[str, str]:
    """The key as both ``Authorization: Bearer`` and ``x-api-key`` (what the gateway accepts)."""
    return {"authorization": f"Bearer {key}", "x-api-key": key, "accept": "application/json"}


# ---------------------------------------------------------------- parsing (defensive)


@dataclass(frozen=True)
class Bucket:
    """One credit source: ``quota`` granted, ``used`` and ``remaining`` (None when unknown)."""

    quota: float | None = None
    used: float | None = None
    remaining: float | None = None

    @property
    def granted(self) -> bool:
        return self.quota is not None and self.quota > 0


@dataclass(frozen=True)
class Balance:
    total: Bucket
    monthly: Bucket = field(default_factory=Bucket)
    purchased: Bucket = field(default_factory=Bucket)
    org_granted: Bucket = field(default_factory=Bucket)
    # When the monthly allocation renews (aware when the API gave an offset).
    renewal: datetime | None = None


@dataclass(frozen=True)
class ModelUsage:
    key: str
    calls: int | None = None
    credits: float | None = None


@dataclass(frozen=True)
class Usage:
    start: date | None = None
    end: date | None = None
    calls: int | None = None
    credits: float | None = None
    rows: tuple[ModelUsage, ...] = ()


@dataclass
class CreditReport:
    """What one fetch found. ``error`` (Korean, scrubbed) is set when the balance could not be read."""

    balance: Balance | None = None
    usage: Usage | None = None
    error: str | None = None
    # Why the usage part is missing (e.g. "HTTP 500"); the balance is still shown.
    usage_error: str | None = None
    supported: bool = True

    @property
    def ok(self) -> bool:
        return self.balance is not None and self.error is None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    """A finite number from an int, float or numeric string ("1,234.5"); None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    return number if number == number and abs(number) != float("inf") else None


def _count(value: Any) -> int | None:
    number = _number(value)
    return int(round(number)) if number is not None and number >= 0 else None


def _sum(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def parse_bucket(raw: Any) -> Bucket:
    data = _mapping(raw)
    quota, used, remaining = (_number(data.get(name)) for name in ("quota", "used", "remaining"))
    if remaining is None and quota is not None and used is not None:
        remaining = quota - used
    if used is None and quota is not None and remaining is not None:
        used = quota - remaining
    return Bucket(quota=quota, used=used, remaining=remaining)


def parse_datetime(value: Any) -> datetime | None:
    """ISO 8601 date-time (or plain date) -> datetime; None when missing or malformed."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    parsed = parse_date(text)
    return datetime.combine(parsed, time()) if parsed else None


def parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def parse_balance(payload: Any) -> Balance:
    """``GET /credits/`` -> ``Balance``. Missing or odd fields become None, never an error."""
    data = _mapping(payload)
    monthly_raw = _mapping(data.get("monthly_allocated"))
    monthly = parse_bucket(monthly_raw)
    purchased = parse_bucket(data.get("purchased"))
    org_granted = parse_bucket(data.get("org_granted"))
    total = parse_bucket(data.get("total"))
    parts = (monthly, purchased, org_granted)
    # A missing total field is the sum of the sources that report it.
    total = Bucket(
        quota=total.quota if total.quota is not None else _sum([b.quota for b in parts]),
        used=total.used if total.used is not None else _sum([b.used for b in parts]),
        remaining=total.remaining if total.remaining is not None else _sum([b.remaining for b in parts]),
    )
    renewal = parse_datetime(monthly_raw.get("renewal_date")) or parse_datetime(data.get("renewal_date"))
    return Balance(total=total, monthly=monthly, purchased=purchased, org_granted=org_granted, renewal=renewal)


def parse_usage(payload: Any) -> Usage:
    """``GET /usage/`` -> ``Usage`` (rows grouped by model). Defensive like ``parse_balance``."""
    data = _mapping(payload)
    total = _mapping(data.get("total"))
    raw_rows = data.get("rows")
    rows: list[ModelUsage] = []
    for raw in raw_rows if isinstance(raw_rows, list) else []:
        item = _mapping(raw)
        key = item.get("key")
        key = " ".join(str(key).split()) if isinstance(key, (str, int, float)) and not isinstance(key, bool) else ""
        if len(key) > MAX_MODEL_KEY_CHARS:
            key = key[: MAX_MODEL_KEY_CHARS - 1] + "…"
        rows.append(ModelUsage(key=key or "(이름 없음)", calls=_count(item.get("call_count")), credits=_number(item.get("credits"))))
    calls = _count(total.get("call_count"))
    if calls is None and any(row.calls is not None for row in rows):
        calls = sum(row.calls or 0 for row in rows)
    credits = _number(total.get("credits"))
    if credits is None:
        credits = _sum([row.credits for row in rows])
    return Usage(
        start=parse_date(data.get("start_date")),
        end=parse_date(data.get("end_date")),
        calls=calls,
        credits=credits,
        rows=tuple(rows),
    )


# ---------------------------------------------------------------- formatting (pure)


def fmt_number(value: float) -> str:
    """Thousands separators, one decimal place, no trailing ".0": 9050.51 -> "9,050.5", 10000.0 -> "10,000"."""
    try:
        rounded = Decimal(repr(float(value))).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return f"{value:,.0f}"
    if rounded == rounded.to_integral_value():
        return f"{int(rounded):,}"
    return f"{rounded:,.1f}"


def percent_of(part: float | None, whole: float | None) -> float | None:
    if part is None or whole is None or whole <= 0:
        return None
    return part / whole * 100


def remaining_percent(balance: Balance) -> float | None:
    """``total.remaining / total.quota * 100`` (None when either is unknown)."""
    return percent_of(balance.total.remaining, balance.total.quota)


def approx(value: float) -> float:
    """A round figure for an estimate: to 100 from 1,000, to 10 from 100, else to 1."""
    step = 100 if value >= 1_000 else 10 if value >= 100 else 1
    return round(value / step) * step


def _local(dt: datetime, tz: tzinfo) -> datetime:
    return dt.astimezone(tz) if dt.tzinfo is not None else dt


def _shift_month(dt: datetime, months: int) -> datetime:
    index = dt.year * 12 + dt.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    return dt.replace(year=year, month=month, day=min(dt.day, calendar.monthrange(year, month)[1]))


def billing_period(balance: Balance, usage: Usage | None, tz: tzinfo) -> tuple[datetime, datetime] | None:
    """``(start, end)`` of the current monthly cycle, or None when it cannot be told.

    The monthly allocation renews at ``renewal_date``, so the cycle in progress
    is the month before it: ``[renewal_date - 1 month, renewal_date)``. That is
    the window the quota and ``used`` refer to. The usage endpoint's
    ``start_date`` is consistent with it for Chat KHU (2026-10-01 for a
    2026-11-01 renewal), but it describes the usage summary's own window, which
    the API does not promise to align with the cycle; so it is only the
    fallback when there is no renewal date, with the cycle assumed to last one
    month from that day.
    """
    if balance.renewal is not None:
        end = balance.renewal if balance.renewal.tzinfo is not None else balance.renewal.replace(tzinfo=tz)
        return _shift_month(end, -1), end
    if usage is not None and usage.start is not None:
        start = datetime.combine(usage.start, time(), tzinfo=tz)
        return start, _shift_month(start, 1)
    return None


def _used(balance: Balance) -> float | None:
    return balance.total.used if balance.total.used is not None else balance.monthly.used


def projection(balance: Balance, usage: Usage | None, now: datetime, tz: tzinfo) -> tuple[float, float | None] | None:
    """``(credits expected by the end of the cycle, share of the quota in %)`` at the current rate.

    ``used / days_elapsed * days_in_period``; None until a full day of the
    cycle has passed, when nothing was used, or when the cycle is unknown or over.
    """
    period = billing_period(balance, usage, tz)
    used = _used(balance)
    if period is None or used is None or used <= 0:
        return None
    start, end = period
    now = now if now.tzinfo is not None else now.replace(tzinfo=tz)
    elapsed = (now - start).total_seconds() / SECONDS_PER_DAY
    length = (end - start).total_seconds() / SECONDS_PER_DAY
    if elapsed < MIN_PROJECTION_DAYS or length <= 0 or now >= end:
        return None
    projected = used / elapsed * length
    return projected, percent_of(projected, balance.total.quota)


def _bucket_line(label: str, bucket: Bucket) -> str:
    left = fmt_number(bucket.remaining) if bucket.remaining is not None else "?"
    quota = fmt_number(bucket.quota) if bucket.quota is not None else "?"
    return f"{label}: {left} 남음 / {quota}"


def _projection_text(balance: Balance, usage: Usage | None, now: datetime, tz: tzinfo, *, verb: str) -> str | None:
    """``이 속도면 이번 달 약 4,200 {verb} (한도의 42%)``, or None before there is a projection."""
    expected = projection(balance, usage, now, tz)
    if expected is None:
        return None
    projected, share = expected
    text = f"이 속도면 이번 달 약 {fmt_number(approx(projected))} {verb}"
    if share is not None:
        text += f" (한도의 {round(share):,}%)"
    return text


def format_summary(
    balance: Balance,
    usage: Usage | None = None,
    *,
    now: datetime,
    tz: tzinfo,
    slack: bool = False,
    usage_error: str | None = None,
    detailed: bool = True,
) -> str:
    """The Korean credit summary (Slack mrkdwn with ``slack=True``).

    ``💳 Chat KHU 크레딧: 9,050.5 남음 / 10,000 (90.5%) · 11/01 갱신``, then the
    other credit sources (only when granted), then:

    * ``detailed`` (``--credits``, "크레딧 자세히"): this month's use with the
      dates and the call count, the top models and a projection (once a
      full day of the cycle has passed), each on its own line;
    * short (the briefing, the Slack shortcut, the low-credit alert): one line,
      ``이번 달 사용 949.5 · 이 속도면 이번 달 약 4,200 예상 (한도의 42%)``, the
      projection left out while there is none.

    A missing usage part is noted on its own line in both.
    """
    total = balance.total
    label = "*Chat KHU 크레딧*" if slack else "Chat KHU 크레딧"
    if total.remaining is None:
        head = "남은 양을 알 수 없음"
    else:
        head = f"{fmt_number(total.remaining)} 남음"
        if total.quota is not None:
            head += f" / {fmt_number(total.quota)}"
        left_share = remaining_percent(balance)
        if left_share is not None:
            head += f" ({fmt_number(left_share)}%)"
    if balance.renewal is not None:
        head += f" · {_local(balance.renewal, tz):%m/%d} 갱신"
    lines = [f"💳 {label}: {head}"]

    # Purchased and organisation-granted credits only when there are any.
    extras = [
        (name, bucket)
        for name, bucket in (("구매 크레딧", balance.purchased), ("기관 지원 크레딧", balance.org_granted))
        if bucket.granted
    ]
    if extras:
        lines.append(_bucket_line("월 기본 크레딧", balance.monthly))
        lines.extend(_bucket_line(name, bucket) for name, bucket in extras)

    used = _used(balance)
    if used is None and usage is not None:
        used = usage.credits
    if not detailed:
        parts = [f"이번 달 사용 {fmt_number(used)}"] if used is not None else []
        expected = _projection_text(balance, usage, now, tz, verb="예상")
        if expected:
            parts.append(expected)
        if parts:
            lines.append(" · ".join(parts))
        if usage_error:
            lines.append(USAGE_MISSING_TEXT.format(reason=usage_error))
        return "\n".join(lines)
    if used is not None:
        details = []
        if usage is not None and usage.start is not None and usage.end is not None:
            details.append(f"{usage.start:%m/%d}–{usage.end:%m/%d}")
        if usage is not None and usage.calls is not None:
            details.append(f"{usage.calls:,}회")
        lines.append(f"이번 달 사용: {fmt_number(used)}" + (f" ({', '.join(details)})" if details else ""))

    bullet = "• " if slack else "· "
    if usage is not None and usage.rows:
        ranked = sorted(
            usage.rows,
            key=lambda row: (row.credits is None, -(row.credits or 0.0), -(row.calls or 0), row.key),
        )
        for row in ranked[:MAX_MODEL_ROWS]:
            parts = ([f"{row.calls:,}회"] if row.calls is not None else []) + (
                [fmt_number(row.credits)] if row.credits is not None else []
            )
            lines.append(f"{bullet}{row.key}" + (f": {' · '.join(parts)}" if parts else ""))
        if len(ranked) > MAX_MODEL_ROWS:
            lines.append(f"{bullet}외 {len(ranked) - MAX_MODEL_ROWS}개 모델")
    if usage_error:
        lines.append(USAGE_MISSING_TEXT.format(reason=usage_error))

    expected = _projection_text(balance, usage, now, tz, verb="사용 예상")
    if expected:
        lines.append(expected)
    return "\n".join(lines)


# ---------------------------------------------------------------- fetching


class _FetchError(Exception):
    """A failed request: ``message`` (Korean, with a hint line) and ``short`` (e.g. "HTTP 500")."""

    def __init__(self, message: str, short: str):
        super().__init__(short)
        self.message = message
        self.short = short


def _get_json(client: httpx.Client, url: str, headers: Mapping[str, str], secrets: list[str]) -> Any:
    try:
        response = client.get(url, headers=headers)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        detail = safe_error(exc, secrets)[:160]
        hint = SSL_HINT if "CERTIFICATE_VERIFY_FAILED" in detail else CONNECT_HINT
        raise _FetchError(f"{CONNECT_ERROR_TEXT.format(detail=detail)}\n{hint}", "연결 실패") from None
    status = response.status_code
    if status != 200:
        hint = STATUS_HINTS.get(status)
        message = HTTP_ERROR_TEXT.format(status=status) + (f"\n{hint}" if hint else "")
        raise _FetchError(message, f"HTTP {status}")
    try:
        return response.json()
    except ValueError:
        raise _FetchError(f"{NOT_JSON_TEXT}\n{ADDRESS_HINT}", "JSON 응답이 아님") from None


def fetch_report(
    env: Mapping[str, str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> CreditReport:
    """Fetch and parse the balance and this month's usage. Never raises; never contains the key.

    A failed usage request still returns the balance (``usage_error`` says why).
    """
    root = gateway_root(env)
    if root is None:
        return CreditReport(error=UNSUPPORTED_TEXT, supported=False)
    key, _name = credentials(env)
    if not key:
        return CreditReport(error=f"{NO_KEY_TEXT}\n{KEY_HINT}")
    secrets = [*config.secret_values(env), key]
    headers = request_headers(key)
    report = CreditReport()
    try:
        with httpx.Client(transport=transport, timeout=timeout) as client:
            report.balance = parse_balance(_get_json(client, root + CREDITS_PATH, headers, secrets))
            try:
                report.usage = parse_usage(_get_json(client, root + USAGE_PATH, headers, secrets))
            except _FetchError as exc:
                report.usage_error = exc.short
    except _FetchError as exc:
        return CreditReport(error=scrub(exc.message, secrets))
    except Exception as exc:  # noqa: BLE001 - a short Korean message, never a traceback or a key
        return CreditReport(error=scrub(READ_ERROR_TEXT.format(kind=type(exc).__name__), secrets))
    return report


def summary_text(
    report: CreditReport,
    *,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
    slack: bool = False,
    detailed: bool = True,
) -> str:
    """The summary (``format_summary``, detailed or short) for a successful report, else its error (with ``⚠️`` in Slack)."""
    if not report.ok or report.balance is None:
        error = report.error or READ_ERROR_TEXT.format(kind="응답 없음")
        return ("⚠️ " if slack else "") + error
    tz = config.get_timezone(env)
    now = now or datetime.now(tz)
    return format_summary(
        report.balance, report.usage, now=now, tz=tz, slack=slack, usage_error=report.usage_error, detailed=detailed
    )


def slack_credit_text(
    env: Mapping[str, str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
    detailed: bool = False,
) -> str:
    """What the Slack shortcut posts: the short summary in mrkdwn (``detailed``: the detailed one), or a short Korean error."""
    return summary_text(fetch_report(env, transport=transport), env=env, now=now, slack=True, detailed=detailed)


def slack_credit_detail_text(
    env: Mapping[str, str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
) -> str:
    """The Slack shortcut for a detailed request ("크레딧 자세히", "토큰 내역"): the detailed summary, with the models."""
    return slack_credit_text(env, transport=transport, now=now, detailed=True)


# ---------------------------------------------------------------- the get_credits tool (고뭉치)


def _amount(value: float | None) -> float | None:
    """One decimal place, like the summary (9050.51 -> 9050.5); None stays None."""
    return float(Decimal(repr(float(value))).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)) if value is not None else None


def _bucket_payload(bucket: Bucket) -> dict[str, float | None]:
    return {"quota": _amount(bucket.quota), "used": _amount(bucket.used), "remaining": _amount(bucket.remaining)}


def report_payload(
    report: CreditReport,
    *,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compact JSON for 고뭉치's get_credits tool: the same data as the summary.

    ``short_summary`` is the short Korean summary (the briefing's, the one to
    pass on unless details are asked for), ``summary`` the detailed one
    (``--credits``' text, with the models); ``total`` and
    ``monthly`` (quota / used / remaining), ``renewal_date`` (local
    YYYY-MM-DD), ``usage`` (this cycle, with the top models) and
    ``projection`` (credits expected by the end of the cycle at the current
    rate, None until a full day has passed). On failure: ``ok: false`` with
    the short Korean ``error`` (``configured: false`` when the gateway has no
    credit endpoints).
    """
    if not report.ok or report.balance is None:
        payload: dict[str, Any] = {
            "configured": report.supported,
            "ok": False,
            "error": report.error or READ_ERROR_TEXT.format(kind="응답 없음"),
        }
        if not report.supported:
            payload["hint"] = UNSUPPORTED_HINT
        return payload
    tz = config.get_timezone(env)
    now = now or datetime.now(tz)
    balance, usage = report.balance, report.usage
    total = _bucket_payload(balance.total)
    left = remaining_percent(balance)
    total["remaining_percent"] = _amount(left)
    payload = {
        "configured": True,
        "ok": True,
        "short_summary": format_summary(balance, usage, now=now, tz=tz, usage_error=report.usage_error, detailed=False),
        "summary": format_summary(balance, usage, now=now, tz=tz, usage_error=report.usage_error),
        "total": total,
        "monthly": _bucket_payload(balance.monthly),
        "renewal_date": f"{_local(balance.renewal, tz):%Y-%m-%d}" if balance.renewal is not None else None,
    }
    for name, bucket in (("purchased", balance.purchased), ("org_granted", balance.org_granted)):
        if bucket.granted:
            payload[name] = _bucket_payload(bucket)
    if usage is not None:
        ranked = sorted(
            usage.rows,
            key=lambda row: (row.credits is None, -(row.credits or 0.0), -(row.calls or 0), row.key),
        )
        payload["usage"] = {
            "start": usage.start.isoformat() if usage.start else None,
            "end": usage.end.isoformat() if usage.end else None,
            "calls": usage.calls,
            "credits": _amount(usage.credits),
            "models": [
                {"model": row.key, "calls": row.calls, "credits": _amount(row.credits)}
                for row in ranked[:MAX_MODEL_ROWS]
            ],
            "other_models": max(0, len(ranked) - MAX_MODEL_ROWS),
        }
    if report.usage_error:
        payload["usage_error"] = report.usage_error
    expected = projection(balance, usage, now, tz)
    payload["projection"] = (
        {
            "credits": approx(expected[0]),
            "percent_of_quota": round(expected[1]) if expected[1] is not None else None,
        }
        if expected is not None
        else None
    )
    return payload


def credit_payload(
    env: Mapping[str, str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """What the get_credits tool returns (``report_payload``). Never raises, never contains the key. Blocking."""
    if gateway_root(env) is None:
        return {"configured": False, "ok": False, "error": UNSUPPORTED_TEXT, "hint": UNSUPPORTED_HINT}
    if not credentials(env)[0]:
        return {
            "configured": False,
            "ok": False,
            "missing": ["ANTHROPIC_AUTH_TOKEN"],
            "error": NO_KEY_TEXT,
            "hint": KEY_HINT,
        }
    return report_payload(fetch_report(env, transport=transport), env=env, now=now)


# ---------------------------------------------------------------- low-credit alert (pure parts)


def alert_period(balance: Balance, now: datetime) -> str:
    """The key the alert is sent once for: the renewal date, else the calendar month."""
    if balance.renewal is not None:
        return balance.renewal.isoformat()
    return f"month:{now:%Y-%m}"


def alert_text(report: CreditReport, threshold: float, *, env: Mapping[str, str] | None = None, now: datetime | None = None) -> str:
    """The DM sent when the credits run low: a warning line, then the short summary."""
    share = remaining_percent(report.balance) if report.balance is not None else None
    left = f"{fmt_number(share)}%" if share is not None else "알 수 없음"
    return (
        f"⚠️ *Chat KHU 크레딧이 얼마 남지 않았어요* (남은 비율 {left}, 알림 기준 {fmt_number(threshold)}%)\n"
        "이번 갱신 주기에는 이 알림을 한 번만 보내요. 봇에게 '크레딧'이라고 보내면 언제든 다시 확인할 수 있어요.\n\n"
        + summary_text(report, env=env, now=now, slack=True, detailed=False)
    )


# ---------------------------------------------------------------- CLI


def run_credits_cli(
    env: Mapping[str, str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """``python -m mungchi --credits``: print the detailed summary (stdout) or a Korean error (stderr).

    A terminal diagnostic, so always the detailed form (dates, call count, models).
    """
    out = out or sys.stdout
    err = err or sys.stderr
    secrets = config.secret_values(env)
    report = fetch_report(env, transport=transport)
    if not report.ok:
        print(scrub("[오류] " + (report.error or READ_ERROR_TEXT.format(kind="응답 없음")), secrets), file=err)
        if not report.supported:
            print(UNSUPPORTED_HINT, file=err)
        return 1
    print(scrub(summary_text(report, env=env, now=now, detailed=True), secrets), file=out)
    return 0
