"""``python -m mungchi --list-models`` against a mocked httpx transport (no network)."""

from __future__ import annotations

import io

import httpx

from mungchi import config
from mungchi.model_list import NOT_JSON_HINT, STATUS_HINTS, list_models, request_headers

GATEWAY = "https://factchat-cloud.mindlogic.ai/v1/gateway/claude"
TOKEN = "gw-test-token-0123456789abcdef"
# Assembled at runtime so no key-shaped literal is committed.
API_KEY = "-".join(["sk", "ant", "api03", "K" * 32])

MODELS = {
    "data": [
        {"type": "model", "id": "claude-opus-4-1", "display_name": "Claude Opus 4.1"},
        {"type": "model", "id": "claude-sonnet-4-5", "display_name": "Claude Sonnet 4.5"},
    ],
    "has_more": False,
    "first_id": "claude-opus-4-1",
    "last_id": "claude-sonnet-4-5",
}


def run(env, handler):
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    out, err = io.StringIO(), io.StringIO()
    code = list_models(env, transport=httpx.MockTransport(record), out=out, err=err)
    return code, out.getvalue(), err.getvalue(), requests


def test_lists_ids_one_per_line_and_marks_the_current_model():
    env = {"ANTHROPIC_BASE_URL": GATEWAY + "/", "ANTHROPIC_AUTH_TOKEN": TOKEN, "MUNGCHI_MODEL": "claude-sonnet-4-5"}
    code, out, err, [request] = run(env, lambda r: httpx.Response(200, json=MODELS))
    assert code == 0
    assert str(request.url) == GATEWAY + "/v1/models"
    lines = out.splitlines()
    assert lines[0] == f"모델 목록 확인: GET {GATEWAY}/v1/models (인증: ANTHROPIC_AUTH_TOKEN)"
    assert lines[1] == "HTTP 200: 모델 2개"
    assert lines[2] == "  claude-opus-4-1"
    assert lines[3] == "* claude-sonnet-4-5   ← 지금 MUNGCHI_MODEL"
    assert len(lines) == 4
    assert err == ""
    assert TOKEN not in out + err


def test_auth_token_is_sent_as_bearer_and_x_api_key():
    env = {"ANTHROPIC_BASE_URL": GATEWAY, "ANTHROPIC_AUTH_TOKEN": TOKEN}
    code, out, err, [request] = run(env, lambda r: httpx.Response(200, json=MODELS))
    assert code == 0
    assert request.method == "GET"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["x-api-key"] == TOKEN
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert TOKEN not in out + err


def test_api_key_goes_in_x_api_key_on_the_default_url_and_missing_model_is_noted():
    code, out, err, [request] = run({"ANTHROPIC_API_KEY": API_KEY}, lambda r: httpx.Response(200, json=MODELS))
    assert code == 0
    assert str(request.url) == "https://api.anthropic.com/v1/models"
    assert request.headers["x-api-key"] == API_KEY
    assert "authorization" not in request.headers
    assert "(인증: ANTHROPIC_API_KEY)" in out
    assert "*" not in out  # default claude-opus-5-5 is not offered here
    assert "지금 MUNGCHI_MODEL 값(claude-opus-5-5)이 목록에 없습니다" in out
    assert API_KEY not in out + err


def test_both_keys_set_warns_to_empty_the_api_key():
    env = {"ANTHROPIC_BASE_URL": GATEWAY, "ANTHROPIC_AUTH_TOKEN": TOKEN, "ANTHROPIC_API_KEY": API_KEY}
    code, out, err, [request] = run(env, lambda r: httpx.Response(200, json=MODELS))
    assert code == 0
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert "ANTHROPIC_API_KEY를 비우세요" in err
    assert TOKEN not in out + err and API_KEY not in out + err


def test_404_prints_a_korean_hint_and_no_key():
    body = {"type": "error", "error": {"type": "not_found_error", "message": f"no route for {GATEWAY}/v1/models key={TOKEN}"}}
    code, out, err, _ = run({"ANTHROPIC_BASE_URL": GATEWAY, "ANTHROPIC_AUTH_TOKEN": TOKEN}, lambda r: httpx.Response(404, json=body))
    assert code == 1
    assert "[오류] 모델 목록을 받지 못했습니다(HTTP 404)." in err
    assert "응답: " in err and "not_found_error" in err
    assert STATUS_HINTS[404] in err and "ANTHROPIC_BASE_URL의 경로" in err
    assert TOKEN not in out + err
    assert "***" in err


def test_401_prints_the_auth_hint_and_no_key():
    body = {"type": "error", "error": {"type": "authentication_error", "message": f"invalid x-api-key {API_KEY}"}}
    code, out, err, _ = run({"ANTHROPIC_API_KEY": API_KEY}, lambda r: httpx.Response(401, json=body))
    assert code == 1
    assert "(HTTP 401)" in err
    assert config.AUTH_HINT in err
    assert API_KEY not in out + err


def test_no_key_stops_before_any_request():
    code, out, err, requests = run({"ANTHROPIC_BASE_URL": GATEWAY}, lambda r: httpx.Response(200, json=MODELS))
    assert code == 1 and requests == []
    assert "ANTHROPIC_API_KEY" in err and "ANTHROPIC_AUTH_TOKEN" in err


def test_web_page_instead_of_api_is_explained():
    code, out, err, _ = run(
        {"ANTHROPIC_BASE_URL": GATEWAY, "ANTHROPIC_AUTH_TOKEN": TOKEN},
        lambda r: httpx.Response(200, text="<html>Chat KHU</html>"),
    )
    assert code == 1
    assert "JSON이 아니라서" in err and NOT_JSON_HINT in err


def test_pages_are_followed_with_after_id():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("after_id") == "m1":
            return httpx.Response(200, json={"data": [{"id": "m2"}], "has_more": False})
        return httpx.Response(200, json={"data": [{"id": "m1"}], "has_more": True, "last_id": "m1"})

    code, out, err, requests = run({"ANTHROPIC_API_KEY": API_KEY, "MUNGCHI_MODEL": "m2"}, handler)
    assert code == 0 and len(requests) == 2
    assert "  m1" in out.splitlines() and "* m2   ← 지금 MUNGCHI_MODEL" in out.splitlines()


def test_certificate_errors_point_to_install_certificates():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate",
            request=request,
        )

    code, out, err, _ = run({"ANTHROPIC_BASE_URL": GATEWAY, "ANTHROPIC_AUTH_TOKEN": TOKEN}, handler)
    assert code == 1
    assert "연결 실패" in err and "Install Certificates.command" in err
    assert TOKEN not in out + err


def test_request_headers_never_include_an_empty_key():
    headers, name = request_headers({})
    assert name is None and "x-api-key" not in headers and "authorization" not in headers
