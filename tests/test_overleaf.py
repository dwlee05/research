from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mungchi import config
from mungchi.state import StateStore
from mungchi.tools import overleaf_tool
from mungchi.tools.overleaf_tool import (
    LOG_FORMAT,
    GitClient,
    auth_header,
    check_overleaf_updates,
    git_subcommand,
    is_mine,
    parse_log,
    run_check,
)

TOKEN = "olp_TESTTOKEN0123456789abcdef"
PAPER_A = "64a1b2c3d4e5f60718293a4b"
PAPER_B = "64b1b2c3d4e5f60718293a4b"
PAPER_C = "64c1b2c3d4e5f60718293a4b"
PAPER_D = "64d1b2c3d4e5f60718293a4b"
PAPER_E = "64e1b2c3d4e5f60718293a4b"
NOW = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
ENCODED = base64.b64encode(f"git:{TOKEN}".encode()).decode()

# The only keys a list-only result may contain: no commits, subjects, diffs or git URLs.
ALLOWED_KEYS = {"configured", "since", "projects", "unchanged", "errors", "name", "link", "edited_by", "last_edit", "edits", "error"}
# Anything that would read commit contents is forbidden.
FORBIDDEN_SUBCOMMANDS = {"show", "diff", "ls-tree", "cat-file", "blame", "whatchanged"}
FORBIDDEN_FLAGS = ("--stat", "--numstat", "--shortstat", "-p", "--patch", "--name-only", "--name-status", "-u")


def record(name, email, date):
    return f"{name}\x1f{email}\x1f{date}\x1e\n"


LOGS = {
    PAPER_A: (
        record("Kim Coauthor", "kim@uni.ac.kr", "2026-10-04T10:00:00+09:00")
        + record("dongwook lee", "other@example.com", "2026-10-04T20:00:00+09:00")  # mine, by name
        + record("Park", "park@uni.ac.kr", "2026-10-04T06:30:00Z")  # 15:30 in Seoul
        + record("Someone", "DWLEE@Example.COM", "2026-10-02T10:00:00+09:00")  # mine, by email
        + record("KIM  coauthor", "kim@other.org", "2026-10-03T09:00:00+09:00")  # same person, other spelling
        + record("Park", "park@uni.ac.kr", "2026-09-01T10:00:00+09:00")  # before the window
    ),
    PAPER_B: record("Choi", "choi@uni.ac.kr", "2026-10-04T23:00:00+09:00"),
    PAPER_C: record("Dongwook Lee", "dwlee@example.com", "2026-10-04T08:00:00+09:00"),  # only mine
    PAPER_D: "",  # no commits at all
}


class FakeGit:
    """Fake ``git``: answers clone/fetch/rev-parse/log; any content-reading command fails the test.

    Forbidden calls are recorded too, because ``run_check`` turns failures
    into an ``errors`` entry instead of raising.
    """

    def __init__(self, logs=None, fail_for: dict[str, str] | None = None):
        self.calls: list[list[str]] = []
        self.forbidden: list[list[str]] = []
        self.logs = LOGS if logs is None else logs
        self.fail_for = fail_for or {}

    @staticmethod
    def project_of(cmd: list[str]) -> str:
        for arg in reversed(cmd):
            name = Path(arg.rstrip("/")).name
            if re.fullmatch(r"[0-9a-f]{24}", name):
                return name
        return ""

    def __call__(self, cmd, cwd):
        cmd = list(cmd)
        self.calls.append(cmd)
        sub = git_subcommand(cmd[1:])
        if sub in FORBIDDEN_SUBCOMMANDS or any(arg in FORBIDDEN_FLAGS or arg.startswith("--stat=") for arg in cmd):
            self.forbidden.append(cmd)
            raise AssertionError(f"content-reading git command: {cmd}")
        project = self.project_of(cmd)
        if sub in ("clone", "fetch") and project in self.fail_for:
            return subprocess.CompletedProcess(cmd, 128, "", self.fail_for[project])
        out = ""
        if sub == "clone":
            Path(cmd[-1], ".git").mkdir(parents=True)
        elif sub == "log":
            out = self.logs.get(project, "")
        return subprocess.CompletedProcess(cmd, 0, out, "")


def env(tmp_path, **extra):
    base = {
        "OVERLEAF_GIT_TOKEN": TOKEN,
        "OVERLEAF_PROJECTS": f"논문A={PAPER_A}, Paper B={PAPER_B}, 논문C={PAPER_C}, 논문D={PAPER_D}",
        "OVERLEAF_CACHE_DIR": str(tmp_path / "cache"),
        "MY_NAMES": "Dongwook Lee",
        "MY_EMAILS": "dwlee@example.com",
    }
    base.update(extra)
    return base


def all_keys(node):
    if isinstance(node, dict):
        yield from node
        for value in node.values():
            yield from all_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from all_keys(item)


# ---------------------------------------------------------------- helpers


def test_is_mine_is_case_insensitive_on_names_and_emails():
    assert is_mine("dongwook lee", "x@y.z", ["Dongwook Lee"], [])
    assert is_mine("  Dongwook   LEE ", "", ["Dongwook Lee"], [])
    assert is_mine("Anyone", "DWLee@Example.com", [], ["dwlee@example.com"])
    assert not is_mine("Kim Coauthor", "kim@uni.ac.kr", ["Dongwook Lee"], ["dwlee@example.com"])
    assert not is_mine("", "", ["Dongwook Lee"], ["dwlee@example.com"])


def test_log_format_reads_only_author_and_date():
    assert LOG_FORMAT == "%an%x1f%ae%x1f%aI%x1e"
    commits = parse_log(LOGS[PAPER_A])
    assert [c.author_name for c in commits] == ["Kim Coauthor", "dongwook lee", "Park", "Someone", "KIM  coauthor", "Park"]
    assert commits[2].date == datetime(2026, 10, 4, 6, 30, tzinfo=timezone.utc)
    assert all(c.date.tzinfo is not None for c in commits)
    projects, invalid = config.parse_overleaf_projects(f"Paper A={PAPER_A}, {PAPER_A}, bad id=../etc")
    assert [(p.name, p.project_id) for p in projects] == [("Paper A", PAPER_A), (PAPER_A, PAPER_A)]
    assert invalid == ["bad id=../etc"]


# ---------------------------------------------------------------- output


def test_run_check_output_shape_sorting_and_time_format(tmp_path):
    runner = FakeGit()
    store = StateStore(tmp_path / "state.json")
    payload = run_check(env=env(tmp_path), now=NOW, runner=runner, store=store)

    assert payload == {
        "configured": True,
        "since": "2026-09-28T09:00+09:00",  # no stored check yet: LOOKBACK_DAYS (7) in Seoul time
        "projects": [
            {
                "name": "Paper B",
                "link": f"https://www.overleaf.com/project/{PAPER_B}",
                "edited_by": [{"name": "Choi", "last_edit": "2026-10-04 23:00", "edits": 1}],
            },
            {
                "name": "논문A",
                "link": f"https://www.overleaf.com/project/{PAPER_A}",
                "edited_by": [
                    {"name": "Park", "last_edit": "2026-10-04 15:30", "edits": 1},
                    {"name": "Kim Coauthor", "last_edit": "2026-10-04 10:00", "edits": 2},
                ],
            },
        ],
        "unchanged": ["논문C", "논문D"],
        "errors": [],
    }
    assert set(all_keys(payload)) <= ALLOWED_KEYS
    assert runner.forbidden == []
    # Every successfully checked project moves its "last checked" time forward.
    for project_id in (PAPER_A, PAPER_B, PAPER_C, PAPER_D):
        assert store.last_checked(f"overleaf:{project_id}") == NOW


def test_only_git_log_metadata_is_read(tmp_path):
    runner = FakeGit()
    run_check(env=env(tmp_path), now=NOW, runner=runner, store=StateStore(tmp_path / "s.json"))
    subcommands = {git_subcommand(cmd[1:]) for cmd in runner.calls}
    assert subcommands == {"clone", "rev-parse", "log"}
    logs = [cmd for cmd in runner.calls if git_subcommand(cmd[1:]) == "log"]
    assert len(logs) == 4
    for cmd in logs:
        assert "--since=2026-09-28T00:00:00+00:00" in cmd
        assert f"--format={LOG_FORMAT}" in cmd
        assert not any(arg in FORBIDDEN_FLAGS for arg in cmd)
    assert runner.forbidden == []


def test_my_commits_are_excluded_case_insensitively(tmp_path):
    logs = {
        PAPER_A: record("DONGWOOK LEE", "x@y.z", "2026-10-04T10:00:00+09:00")
        + record("Somebody", "DwLee@EXAMPLE.com", "2026-10-04T11:00:00+09:00")
        + record("Kim", "kim@uni.ac.kr", "2026-10-04T12:00:00+09:00")
    }
    payload = run_check(
        env=env(tmp_path, OVERLEAF_PROJECTS=f"논문A={PAPER_A}"),
        now=NOW,
        runner=FakeGit(logs),
        store=StateStore(tmp_path / "s.json"),
    )
    [project] = payload["projects"]
    assert project["edited_by"] == [{"name": "Kim", "last_edit": "2026-10-04 12:00", "edits": 1}]
    text = json.dumps(payload, ensure_ascii=False)
    assert "DONGWOOK" not in text and "Somebody" not in text


def test_time_format_follows_configured_timezone(tmp_path):
    payload = run_check(
        env=env(tmp_path, OVERLEAF_PROJECTS=f"Paper B={PAPER_B}", TIMEZONE="Europe/London"),
        now=NOW,
        runner=FakeGit(),
        store=StateStore(tmp_path / "s.json"),
    )
    # 2026-10-04 23:00 in Seoul is 15:00 in London (BST, UTC+1).
    assert payload["projects"][0]["edited_by"][0]["last_edit"] == "2026-10-04 15:00"
    assert payload["since"] == "2026-09-28T01:00+01:00"


def test_since_is_the_widest_window_across_projects(tmp_path):
    store = StateStore(tmp_path / "s.json")
    store.mark_checked(f"overleaf:{PAPER_A}", NOW - timedelta(hours=24))
    store.mark_checked(f"overleaf:{PAPER_B}", NOW - timedelta(hours=48))
    payload = run_check(
        env=env(tmp_path, OVERLEAF_PROJECTS=f"논문A={PAPER_A}, Paper B={PAPER_B}"),
        now=NOW,
        runner=FakeGit(),
        store=store,
    )
    assert payload["since"] == "2026-10-03T09:00+09:00"
    # 논문A is only checked since its own last check: Kim's 10-03 commit is out of range.
    paper_a = next(p for p in payload["projects"] if p["name"] == "논문A")
    assert paper_a["edited_by"] == [
        {"name": "Park", "last_edit": "2026-10-04 15:30", "edits": 1},
        {"name": "Kim Coauthor", "last_edit": "2026-10-04 10:00", "edits": 1},
    ]


# ---------------------------------------------------------------- errors and secrets


def test_errors_are_scrubbed_and_other_projects_still_reported(tmp_path):
    runner = FakeGit(
        fail_for={
            PAPER_E: (
                f"fatal: Authentication failed for 'https://git:{TOKEN}@git.overleaf.com/{PAPER_E}/' "
                f"(Authorization: Basic {ENCODED})"
            )
        }
    )
    store = StateStore(tmp_path / "state.json")
    payload = run_check(
        env=env(tmp_path, OVERLEAF_PROJECTS=f"논문E={PAPER_E}, Paper B={PAPER_B}, bad id=../etc"),
        now=NOW,
        runner=runner,
        store=store,
    )
    text = json.dumps(payload, ensure_ascii=False)
    assert [p["name"] for p in payload["projects"]] == ["Paper B"]
    assert [e["name"] for e in payload["errors"]] == ["bad id=../etc", "논문E"]
    assert "형식" in payload["errors"][0]["error"]
    error = payload["errors"][1]["error"]
    assert "git clone 실패" in error and "<Overleaf git 주소>" in error
    assert TOKEN not in text and ENCODED not in text
    assert "git.overleaf.com" not in text  # the project-id-bearing git URL never leaves the tool
    assert store.last_checked(f"overleaf:{PAPER_E}") is None
    assert store.last_checked(f"overleaf:{PAPER_B}") == NOW


def test_token_only_travels_in_auth_header_of_network_commands(tmp_path):
    runner = FakeGit()
    payload = run_check(env=env(tmp_path), now=NOW, runner=runner, store=StateStore(tmp_path / "s.json"))
    header = auth_header(TOKEN)
    assert header == "Authorization: Basic " + ENCODED
    for cmd in runner.calls:
        joined = " ".join(cmd)
        assert TOKEN not in joined  # raw token never on the command line
        if git_subcommand(cmd[1:]) in ("clone", "fetch"):
            assert f"http.extraHeader={header}" in cmd
        else:
            assert "http.extraHeader" not in joined
    clone = next(c for c in runner.calls if git_subcommand(c[1:]) == "clone")
    assert f"https://git.overleaf.com/{PAPER_A}" in clone  # no credentials in the remote URL
    text = json.dumps(payload)
    assert TOKEN not in text and "git.overleaf.com" not in text


def test_git_client_never_leaks_token_on_timeout():
    def slow(cmd, cwd):
        raise subprocess.TimeoutExpired(cmd, 1)

    client = GitClient(TOKEN, runner=slow)
    try:
        client.run(["fetch", "origin"], auth=True)
    except Exception as exc:  # noqa: BLE001
        assert TOKEN not in str(exc)
        assert "시간 초과" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected GitError")


# ---------------------------------------------------------------- tool handler


def test_unconfigured_lists_missing_env_vars():
    payload = run_check()
    assert payload["configured"] is False
    assert payload["missing"] == ["OVERLEAF_GIT_TOKEN", "OVERLEAF_PROJECTS", "MY_NAMES", "MY_EMAILS"]
    assert "OVERLEAF_GIT_TOKEN" in payload["hint"]


def test_tool_handler_unconfigured_returns_json():
    result = asyncio.run(check_overleaf_updates.handler({"since_hours": 24}))
    data = json.loads(result["content"][0]["text"])
    assert data["configured"] is False
    assert "OVERLEAF_PROJECTS" in data["missing"]


def test_tool_handler_returns_compact_list_only_json(tmp_path, monkeypatch):
    for key, value in env(tmp_path).items():
        monkeypatch.setenv(key, value)
    runner = FakeGit()
    monkeypatch.setattr(overleaf_tool, "_default_runner", runner)
    monkeypatch.setattr(overleaf_tool, "utcnow", lambda: NOW)
    result = asyncio.run(check_overleaf_updates.handler({"since_hours": 48}))
    text = result["content"][0]["text"]
    data = json.loads(text)
    assert set(data) == {"configured", "since", "projects", "unchanged", "errors"}
    assert set(all_keys(data)) <= ALLOWED_KEYS
    assert data["since"] == "2026-10-03T09:00+09:00"
    assert [p["name"] for p in data["projects"]] == ["Paper B", "논문A"]
    assert TOKEN not in text and "git.overleaf.com" not in text
    assert runner.forbidden == []


# ---------------------------------------------------------------- real git


def _git(*args, cwd, name="Kim Coauthor", email="kim@uni.ac.kr", date="2026-10-04T10:00:00+09:00"):
    env = {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
        "GIT_COMMITTER_DATE": date,
        "HOME": str(cwd),
        "PATH": os.environ.get("PATH", ""),
    }
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_real_git_output_is_parsed(tmp_path):
    """End-to-end against a local repository (no network): the cache clone's
    origin points at a local path, so ``sync_repo`` only runs ``git fetch``."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    me = {"name": "Dongwook Lee", "email": "dwlee@example.com"}
    _git("init", "-q", "-b", "master", cwd=upstream)
    (upstream / "main.tex").write_text("\\section{Intro}\nOld paragraph.\n", encoding="utf-8")
    _git("add", ".", cwd=upstream, **me)
    _git("commit", "-q", "-m", "Initial", cwd=upstream, date="2026-10-01T09:00:00+09:00", **me)

    cache = tmp_path / "cache"
    cache.mkdir()
    _git("clone", "-q", "--no-checkout", str(upstream), str(cache / PAPER_A), cwd=tmp_path)

    (upstream / "main.tex").write_text("\\section{Intro}\nNew paragraph with motivation.\n", encoding="utf-8")
    (upstream / "refs.bib").write_text("@article{a,\n title={A}\n}\n", encoding="utf-8")
    _git("add", ".", cwd=upstream)
    _git("commit", "-q", "-m", "Rewrite intro and add refs", cwd=upstream)
    (upstream / "notes.md").write_text("mine\n", encoding="utf-8")
    _git("add", ".", cwd=upstream, name="DONGWOOK LEE", email="DWLee@Example.com")
    _git(
        "commit", "-q", "-m", "My notes", cwd=upstream,
        name="DONGWOOK LEE", email="DWLee@Example.com", date="2026-10-04T12:00:00+09:00",
    )

    calls: list[list[str]] = []

    def recording_runner(cmd, cwd):
        calls.append(list(cmd))
        return overleaf_tool._default_runner(cmd, cwd)

    payload = run_check(
        env=env(tmp_path, OVERLEAF_PROJECTS=f"논문A={PAPER_A}", OVERLEAF_CACHE_DIR=str(cache)),
        now=NOW,
        runner=recording_runner,
        store=StateStore(tmp_path / "state.json"),
    )
    assert payload["errors"] == [], payload
    assert payload["projects"] == [
        {
            "name": "논문A",
            "link": f"https://www.overleaf.com/project/{PAPER_A}",
            "edited_by": [{"name": "Kim Coauthor", "last_edit": "2026-10-04 10:00", "edits": 1}],
        }
    ]
    assert payload["unchanged"] == []
    text = json.dumps(payload, ensure_ascii=False)
    assert "New paragraph" not in text and "refs.bib" not in text and "Rewrite intro" not in text
    assert {git_subcommand(cmd[1:]) for cmd in calls} == {"fetch", "rev-parse", "log"}
