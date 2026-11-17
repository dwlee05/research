"""Korean run-error messages (terminal and Slack) and the Claude env clean-up."""

from __future__ import annotations

import io
import os

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from mungchi import config
from mungchi import main as main_module
from mungchi.main import (
    ERROR_MESSAGES,
    Renderer,
    build_parser,
    describe_assistant_error,
    describe_result_error,
    drop_empty_claude_env,
    main,
)
from mungchi.slack_bot import compose_reply

MODEL_TEXT = (
    "There's an issue with the selected model (claude-opus-5-5). It may not exist or you may not have "
    "access to it. Run /model to pick a different model."
)
AUTH_TEXT = (
    'Failed to authenticate. API Error: 401 {"type":"error","error":{"type":"authentication_error",'
    '"message":"invalid x-api-key"}}'
)


def result_message(**fields) -> ResultMessage:
    defaults = dict(subtype="success", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=1, session_id="s")
    return ResultMessage(**{**defaults, **fields})


def api_error(text: str, kind: str = "unknown") -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="<synthetic>", error=kind)


# ---------------------------------------------------------------- the reported case


def test_model_issue_is_reported_once_with_hint_and_never_as_success():
    out, status = io.StringIO(), io.StringIO()
    renderer = Renderer(out=out, status=status)
    renderer.handle(api_error(MODEL_TEXT))
    # The CLI closes an API error with subtype "success", is_error=True and the same text.
    renderer.handle(result_message(result=MODEL_TEXT))
    shown = status.getvalue()
    assert "success" not in shown
    assert "알 수 없는 오류" not in shown
    assert shown.count("issue with the selected model") == 1
    assert shown.splitlines() == [
        "[오류] 모델 설정에 문제가 있습니다.",
        " ".join(MODEL_TEXT.split()),
        config.MODEL_HINT,
    ]
    assert out.getvalue() == ""  # the API error is not printed as an answer
    result = renderer.result()
    assert result.failed and result.text == ""
    assert result.error.startswith("모델 설정에 문제가 있습니다.") and result.error.endswith(config.MODEL_HINT)
    assert "--list-models" in config.MODEL_HINT


def test_result_error_never_shows_the_bare_subtype():
    renderer = Renderer(echo=False)
    renderer.handle(result_message())
    assert renderer.error == "응답을 마치지 못했습니다."
    assert describe_result_error("success") == "응답을 마치지 못했습니다."
    assert describe_result_error("success", "", 529) == "응답을 마치지 못했습니다(HTTP 529)."
    assert describe_result_error("error_max_turns") == "응답을 마치지 못했습니다(최대 턴 수 도달)."
    assert describe_result_error("error_during_execution", "boom") == "응답을 마치지 못했습니다(실행 중 오류).\nboom"
    for subtype in ("success", "error_during_execution", "error_max_turns", "error_max_budget_usd"):
        assert subtype not in describe_result_error(subtype, "API Error: 500")


def test_result_only_error_uses_its_text_and_status():
    renderer = Renderer(echo=False)
    text = 'API Error: 404 {"type":"error","error":{"type":"not_found_error","message":"model: claude-x"}}'
    renderer.handle(result_message(result=text, api_error_status=404))
    lines = renderer.error.splitlines()
    assert lines[0] == "응답을 마치지 못했습니다(HTTP 404)."
    assert lines[1].startswith("API Error: 404")
    assert lines[-1] == config.MODEL_HINT


def test_a_new_result_detail_is_still_reported():
    renderer = Renderer(echo=False)
    renderer.handle(api_error(MODEL_TEXT))
    renderer.handle(result_message(subtype="error_during_execution", errors=["tool crashed"]))
    assert len(renderer.status_lines) == 2
    assert renderer.status_lines[1] == "[오류] 응답을 마치지 못했습니다(실행 중 오류).\ntool crashed"
    assert renderer.error.startswith("모델 설정에 문제가 있습니다.")  # the first error is the turn's error


# ---------------------------------------------------------------- hints


def test_model_hint_for_404_and_model_not_found_texts():
    for text in (
        MODEL_TEXT,
        'API Error: 404 {"type":"error","error":{"type":"not_found_error","message":"model: x"}}',
        "The model claude-x does not exist",
        "Unknown model: claude-x",
    ):
        message = describe_assistant_error("unknown", text)
        assert message.startswith("모델 설정에 문제가 있습니다."), text
        assert message.endswith(config.MODEL_HINT), text
    # invalid_request keeps its own reason but still points at MUNGCHI_MODEL.
    assert describe_assistant_error("invalid_request") == f"잘못된 요청입니다.\n{config.MODEL_HINT}"


def test_auth_hint_for_401_texts_and_authentication_failures():
    renderer = Renderer(echo=False)
    renderer.handle(api_error(AUTH_TEXT, kind="authentication_failed"))
    renderer.handle(result_message(result=AUTH_TEXT, api_error_status=401))
    assert len(renderer.status_lines) == 1
    assert renderer.error.startswith("인증에 실패했습니다.")
    assert renderer.error.endswith(config.AUTH_HINT)
    assert "ANTHROPIC_AUTH_TOKEN" in config.AUTH_HINT and "게이트웨이를 쓸 땐 ANTHROPIC_API_KEY를 비우세요" in config.AUTH_HINT
    for text in ("API Error: 401 Unauthorized", "Invalid API key · Fix external API key", "invalid bearer token"):
        message = describe_assistant_error("unknown", text)
        assert message.startswith("인증에 실패했습니다.") and message.endswith(config.AUTH_HINT), text
    assert describe_assistant_error("authentication_failed") == f"인증에 실패했습니다.\n{config.AUTH_HINT}"


def test_other_errors_get_no_setting_hint():
    message = describe_assistant_error("rate_limit", 'API Error: 429 {"type":"error"}')
    assert message.startswith("요청 한도에 걸렸습니다.")
    assert config.MODEL_HINT not in message and config.AUTH_HINT not in message
    assert describe_assistant_error("server_error") == ERROR_MESSAGES["server_error"]
    assert describe_assistant_error("some_new_kind") == ERROR_MESSAGES["unknown"]


def test_messages_no_longer_mention_a_claude_login():
    for text in (*ERROR_MESSAGES.values(), config.AUTH_HINT, config.MODEL_HINT):
        assert "로그인" not in text


# ---------------------------------------------------------------- secrets and length


def test_error_messages_are_scrubbed(monkeypatch):
    gateway_token = "gateway-secret-token-0123456789"
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", gateway_token)
    api_key = "-".join(["sk", "ant", "api03", "Q" * 30])  # assembled so no key-shaped literal is committed
    text = f"API Error: 401 invalid bearer token {gateway_token} (also tried {api_key})"
    renderer = Renderer(echo=False)
    renderer.handle(api_error(text))
    renderer.handle(result_message(result=f"failed with {gateway_token}", api_error_status=401))
    shown = "\n".join(renderer.status_lines) + (renderer.error or "")
    assert gateway_token not in shown and api_key not in shown
    assert "***" in shown and config.AUTH_HINT in shown


def test_long_error_text_is_cut_after_scrubbing_and_keeps_the_hint(monkeypatch):
    secret = "gateway-secret-token-0123456789"
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", secret)
    text = MODEL_TEXT + " " + "x" * 400 + secret
    message = describe_assistant_error("unknown", text)
    reason, excerpt, hint = message.split("\n")
    assert len(excerpt) <= main_module.MAX_ERROR_DETAIL_CHARS and excerpt.endswith("…")
    assert secret[:12] not in message
    assert hint == config.MODEL_HINT


def test_slack_reply_shows_the_same_message_with_its_hint():
    renderer = Renderer(echo=False)
    renderer.handle(api_error(MODEL_TEXT + " " + "y" * 500))
    reply = compose_reply(renderer.result())
    assert reply.startswith("⚠️ 모델 설정에 문제가 있습니다.")
    assert reply.endswith(config.MODEL_HINT)
    assert "success" not in reply


# ---------------------------------------------------------------- empty Claude settings


def test_drop_empty_claude_env_removes_only_empty_claude_values():
    env = {
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_BASE_URL": "  ",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "",
        "ANTHROPIC_AUTH_TOKEN": "gateway-token",
        "OTHER": "",
    }
    drop_empty_claude_env(env)
    assert env == {"ANTHROPIC_AUTH_TOKEN": "gateway-token", "OTHER": ""}


class EnvRecordingClient:
    """Stands in for ClaudeSDKClient and records the environment the CLI would inherit."""

    seen: list[dict[str, str]] = []

    def __init__(self, options=None, transport=None):
        EnvRecordingClient.seen.append(dict(os.environ))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt, session_id="default"):
        pass

    async def receive_response(self):
        yield result_message(is_error=False)


def test_empty_api_key_is_removed_before_the_sdk_client_starts(monkeypatch, capsys):
    EnvRecordingClient.seen = []
    monkeypatch.setattr(main_module, "ClaudeSDKClient", EnvRecordingClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "gateway-token-value")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example/v1/gateway/claude")
    assert main(["질문"]) == 0
    [env] = EnvRecordingClient.seen
    assert "ANTHROPIC_API_KEY" not in env
    assert env["ANTHROPIC_AUTH_TOKEN"] == "gateway-token-value"
    assert env["ANTHROPIC_BASE_URL"] == "https://gateway.example/v1/gateway/claude"


# ---------------------------------------------------------------- --list-models in the CLI


def test_help_lists_list_models_and_rejects_combinations(capsys):
    help_text = build_parser().format_help()
    assert "--list-models" in help_text and "python -m mungchi --list-models" in help_text
    for argv in (["--list-models", "질문"], ["--list-models", "--brief"], ["--list-models", "--agent", "update"], ["slack", "--list-models"]):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2
    assert "--list-models는 질문이나 다른 옵션" in capsys.readouterr().err


def test_cli_list_models_runs_after_env_clean_up(monkeypatch):
    from mungchi import model_list

    calls: list[dict[str, str]] = []

    def fake_list_models(env=None, **kwargs):
        calls.append(dict(os.environ))
        return 0

    monkeypatch.setattr(model_list, "list_models", fake_list_models)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert main(["--list-models"]) == 0
    [env] = calls
    assert "ANTHROPIC_API_KEY" not in env
