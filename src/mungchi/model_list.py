"""``python -m mungchi --list-models``: the model ids the configured Claude API or gateway offers.

Only ``GET {ANTHROPIC_BASE_URL}/v1/models`` is called (no agent run, no model
usage). The key goes in request headers only and is never printed.
"""

from __future__ import annotations

import sys
from typing import Any, Mapping, TextIO

import httpx

from . import config
from .tools.common import safe_error, scrub

ANTHROPIC_VERSION = "2023-06-01"
TIMEOUT_SECONDS = 20.0
MAX_PAGES = 10
MAX_BODY_CHARS = 300

NO_KEY_TEXT = (
    "[오류] Claude 인증 키가 없습니다. .env에 ANTHROPIC_API_KEY(Anthropic 키) 또는 "
    "ANTHROPIC_AUTH_TOKEN(게이트웨이 키)을 넣으세요."
)
BOTH_KEYS_TEXT = (
    "[경고] ANTHROPIC_AUTH_TOKEN과 ANTHROPIC_API_KEY가 둘 다 있습니다. "
    "게이트웨이를 쓸 땐 ANTHROPIC_API_KEY를 비우세요(에이전트 실행이 게이트웨이 인증에 실패할 수 있습니다)."
)
STATUS_HINTS = {
    401: config.AUTH_HINT,
    403: "→ 이 키로는 모델 목록을 볼 수 없습니다. 키의 권한이나 게이트웨이 이용 범위를 확인하세요.",
    404: (
        "→ ANTHROPIC_BASE_URL의 경로를 확인하세요 (Chat KHU는 끝이 /v1/gateway/claude). "
        "게이트웨이가 모델 목록(/v1/models)을 지원하지 않을 수도 있습니다."
    ),
}
OTHER_STATUS_HINT = "→ 잠시 후 다시 시도하고, 계속되면 ANTHROPIC_BASE_URL과 키 설정을 확인하세요."
NOT_JSON_HINT = "→ ANTHROPIC_BASE_URL이 웹 화면 주소가 아니라 API 주소인지 확인하세요."
CONNECT_HINT = "→ ANTHROPIC_BASE_URL 주소(https://로 시작)와 인터넷 연결을 확인하세요."
SSL_HINT = (
    '→ macOS에서 python.org의 Python을 쓴다면 "/Applications/Python 3.x/Install Certificates.command"를 '
    "한 번 실행하세요 (README의 '문제 해결' 참고)."
)


def request_headers(env: Mapping[str, str] | None = None) -> tuple[dict[str, str], str | None]:
    """Headers for the models request and the name of the env var whose key they carry.

    ``ANTHROPIC_AUTH_TOKEN`` (gateway) wins and is sent both as a Bearer token
    and as ``x-api-key``; otherwise ``ANTHROPIC_API_KEY`` goes in ``x-api-key``.
    The name is ``None`` when neither is set.
    """
    headers = {"anthropic-version": ANTHROPIC_VERSION, "accept": "application/json"}
    token = config.get_anthropic_auth_token(env)
    if token:
        headers["authorization"] = f"Bearer {token}"
        headers["x-api-key"] = token
        return headers, "ANTHROPIC_AUTH_TOKEN"
    key = config.get_anthropic_api_key(env)
    if key:
        headers["x-api-key"] = key
        return headers, "ANTHROPIC_API_KEY"
    return headers, None


def model_ids(payload: Any) -> list[str]:
    """Model ids from an Anthropic (``{"data": [{"id": ...}]}``) or OpenAI-style list."""
    items = payload.get("data") if isinstance(payload, dict) else payload
    ids: list[str] = []
    for item in items if isinstance(items, list) else []:
        model_id = item.get("id") if isinstance(item, dict) else item
        model_id = model_id.strip() if isinstance(model_id, str) else ""
        if model_id and model_id not in ids:
            ids.append(model_id)
    return ids


def _body_excerpt(response: httpx.Response, secrets: list[str]) -> str:
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - undecodable body
        return ""
    flat = scrub(" ".join(text.split()), secrets)
    return flat if len(flat) <= MAX_BODY_CHARS else flat[: MAX_BODY_CHARS - 1].rstrip() + "…"


def fetch_model_ids(client: httpx.Client, url: str, headers: Mapping[str, str]) -> tuple[httpx.Response, list[str]]:
    """GET ``url`` (following ``has_more`` pages). Stops at the first non-200 response.

    Raises ``ValueError`` when a 200 response is not JSON.
    """
    ids: list[str] = []
    params: dict[str, str] | None = None
    for _ in range(MAX_PAGES):
        response = client.get(url, headers=headers, params=params)
        if response.status_code != 200:
            break
        payload = response.json()
        ids += [model_id for model_id in model_ids(payload) if model_id not in ids]
        last_id = payload.get("last_id") if isinstance(payload, dict) else None
        if not (isinstance(payload, dict) and payload.get("has_more") and isinstance(last_id, str) and last_id):
            break
        params = {"after_id": last_id}
    return response, ids


def list_models(
    env: Mapping[str, str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Print the status and the model ids one per line, marking ``MUNGCHI_MODEL`` with ``*``."""
    out = out or sys.stdout
    err = err or sys.stderr
    secrets = config.secret_values(env)
    url = f"{config.get_anthropic_base_url(env)}/v1/models"
    current = config.get_model(env)
    headers, key_name = request_headers(env)
    if key_name is None:
        print(NO_KEY_TEXT, file=err)
        return 1
    print(f"모델 목록 확인: GET {scrub(url, secrets)} (인증: {key_name})", file=out)
    if key_name == "ANTHROPIC_AUTH_TOKEN" and config.get_anthropic_api_key(env):
        print(BOTH_KEYS_TEXT, file=err)

    try:
        with httpx.Client(transport=transport, timeout=TIMEOUT_SECONDS) as client:
            response, ids = fetch_model_ids(client, url, headers)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        message = safe_error(exc, secrets)
        print(f"[오류] 모델 목록을 받지 못했습니다(연결 실패): {message}", file=err)
        print(SSL_HINT if "CERTIFICATE_VERIFY_FAILED" in message else CONNECT_HINT, file=err)
        return 1
    except ValueError:
        print("[오류] 응답이 JSON이 아니라서 모델 목록을 읽지 못했습니다(HTTP 200).", file=err)
        print(NOT_JSON_HINT, file=err)
        return 1

    status = response.status_code
    if status != 200:
        print(f"[오류] 모델 목록을 받지 못했습니다(HTTP {status}).", file=err)
        excerpt = _body_excerpt(response, secrets)
        if excerpt:
            print(f"응답: {excerpt}", file=err)
        print(STATUS_HINTS.get(status, OTHER_STATUS_HINT), file=err)
        return 1

    print(f"HTTP {status}: 모델 {len(ids)}개", file=out)
    for model_id in ids:
        mark = "*" if model_id == current else " "
        suffix = "   ← 지금 MUNGCHI_MODEL" if model_id == current else ""
        print(f"{mark} {scrub(model_id, secrets)}{suffix}", file=out)
    if not ids:
        print("모델 목록이 비어 있습니다. 게이트웨이에서 쓸 수 있는 모델을 확인하세요.", file=err)
    elif current not in ids:
        print(
            f"지금 MUNGCHI_MODEL 값({current})이 목록에 없습니다. 위 ID 가운데 하나를 .env의 MUNGCHI_MODEL에 넣으세요.",
            file=out,
        )
    return 0
