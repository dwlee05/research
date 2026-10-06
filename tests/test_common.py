from __future__ import annotations

import base64
import json

from mungchi.tools.common import int_arg, safe_error, scrub, tool_result


def test_int_arg_coerces_and_clamps():
    assert int_arg("24", 0, 0, 100) == 24
    assert int_arg(None, 7, 0, 100) == 7
    assert int_arg("abc", 7, 0, 100) == 7
    assert int_arg(-5, 0, 0, 100) == 0
    assert int_arg(10_000, 0, 0, 100) == 100


def test_scrub_removes_configured_secrets_and_known_shapes():
    token = "olp_abcdefghijklmnopqrstuvwxyz0123"
    basic = base64.b64encode(f"git:{token}".encode()).decode()
    message = (
        f"fatal: auth failed for https://git:{token}@git.overleaf.com/abc "
        f"(Authorization: Basic {basic}) bearer sl.ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    )
    cleaned = scrub(message, secrets=[token, basic])
    assert token not in cleaned
    assert basic not in cleaned
    assert "sl.ABCDEFGHIJ" not in cleaned
    assert "***" in cleaned


def test_scrub_uses_environment_secrets_by_default(monkeypatch):
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "my-very-secret-dropbox-token")
    assert "my-very-secret-dropbox-token" not in scrub("token=my-very-secret-dropbox-token")


def test_scrub_can_redact_urls():
    url = "https://calendar.google.com/calendar/ical/me%40gmail.com/private-0123abcd/basic.ics"
    assert url not in scrub(f"404 for {url}", secrets=[], redact_urls=True)


def test_safe_error_scrubs_exception_text(monkeypatch):
    monkeypatch.setenv("OVERLEAF_GIT_TOKEN", "olp_supersecrettoken123")
    err = RuntimeError("bad credentials olp_supersecrettoken123")
    text = safe_error(err)
    assert "olp_supersecrettoken123" not in text
    assert text.startswith("RuntimeError")


def test_tool_result_is_scrubbed_json_text(monkeypatch):
    monkeypatch.setenv("DROPBOX_REFRESH_TOKEN", "refresh-token-value-123")
    result = tool_result({"error": "oops refresh-token-value-123", "한글": "그대로"})
    text = result["content"][0]["text"]
    assert "refresh-token-value-123" not in text
    assert json.loads(text)["한글"] == "그대로"


def test_tool_result_scrubbing_never_breaks_json():
    payload = {
        "subject": "see https://example.com/page",
        "email": "kim@uni.ac.kr",
        "diff": "+Authorization: Basic abcdef123456\n+the bearer of news",
    }
    data = json.loads(tool_result(payload, secrets=[])["content"][0]["text"])
    assert data["email"] == "kim@uni.ac.kr"
    assert data["subject"] == "see https://example.com/page"
    assert "abcdef123456" not in data["diff"]
    assert "the bearer of news" in data["diff"]


def test_scrub_removes_slack_tokens_by_shape_and_by_env(monkeypatch):
    # Fake tokens are assembled at runtime so no token-shaped literal is committed.
    bot = "-".join(["xoxb", "123456789012", "123456789012", "AbCdEfGhIjKlMnOpQrStUvWx"])
    app = "-".join(["xapp", "1", "A0123456789", "1234567890123", "abcdef0123456789"])
    cleaned = scrub(f"auth failed: {bot} / {app}", secrets=[])
    assert bot not in cleaned and app not in cleaned
    monkeypatch.setenv("SLACK_BOT_TOKEN", "custom-slack-secret-value")
    assert "custom-slack-secret-value" not in scrub("token=custom-slack-secret-value")
