from __future__ import annotations

import base64
import json

from mungchi.tools.common import (
    MAX_DIFF_LINES,
    OutputBudget,
    dumps,
    is_text_path,
    safe_error,
    scrub,
    shrink_to_limit,
    tool_result,
    truncate_diff,
)


def test_text_extensions_are_case_insensitive():
    for path in ["a.tex", "refs.BIB", "x/notes.md", "run.R", "f.m", "t.csv", "s.sty", "c.cls", "p.py", "r.txt"]:
        assert is_text_path(path), path
    for path in ["fig.png", "paper.pdf", "data.xlsx", "Makefile"]:
        assert not is_text_path(path), path


def test_truncate_diff_limits_lines_and_reports_omission():
    text = "\n".join(f"+line {i}" for i in range(200))
    clipped, omitted, truncated = truncate_diff(text, max_lines=MAX_DIFF_LINES)
    lines = clipped.splitlines()
    assert truncated
    assert omitted == 200 - MAX_DIFF_LINES
    assert len(lines) == MAX_DIFF_LINES + 1
    assert lines[-1] == f"… ({omitted}줄 생략)"


def test_truncate_diff_keeps_short_diffs_intact():
    text = "@@ -1 +1 @@\n-old\n+new"
    clipped, omitted, truncated = truncate_diff(text)
    assert (clipped, omitted, truncated) == (text, 0, False)


def test_truncate_diff_caps_very_long_lines():
    text = "+" + "x" * 20_000
    clipped, omitted, truncated = truncate_diff(text, max_lines=80, max_chars=1_000)
    assert truncated and omitted == 0
    assert len(clipped) < 1_100
    assert clipped.endswith("… (긴 줄 일부 생략)")


def test_output_budget_omits_diffs_beyond_total():
    budget = OutputBudget(max_chars=1_000, reserve=0, max_lines=80)
    first = budget.fit("a.tex", "x" * 600)
    second = budget.fit("b.tex", "y" * 600)
    assert first["diff"] == "x" * 600
    assert second["diff"] is None and "생략" in second["diff_note"]
    report = budget.report()
    assert report["omitted"] == ["b.tex"]


def test_output_budget_records_shortened_diffs():
    budget = OutputBudget()
    result = budget.fit("long.tex", "\n".join(f"+{i}" for i in range(500)))
    assert result["diff_truncated"] is True
    assert result["omitted_lines"] == 500 - MAX_DIFF_LINES
    assert budget.report()["shortened"] == ["long.tex"]


def test_shrink_to_limit_drops_latest_diffs_first():
    payload = {"files": [{"path": f"{i}.tex", "diff": "z" * 5_000} for i in range(10)]}
    shrunk = shrink_to_limit(payload, max_chars=20_000)
    assert len(dumps(shrunk)) <= 20_000
    assert shrunk["files"][0]["diff"] is not None
    assert shrunk["files"][-1]["diff"] is None
    assert shrunk["truncation"]["dropped_for_total_limit"] >= 1


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
