from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mungchi import config
from mungchi.state import StateStore
from mungchi.tools.overleaf_tool import (
    GitClient,
    auth_header,
    check_overleaf_updates,
    git_subcommand,
    is_mine,
    parse_log,
    run_check,
    split_patch,
)

TOKEN = "olp_TESTTOKEN0123456789abcdef"
PROJECT = "64a1b2c3d4e5f60718293a4b"
NOW = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)

LOG = (
    "1111111111aaaaaaaaaa1111111111aaaaaaaaaa\x1fKim Coauthor\x1fkim@uni.ac.kr\x1f2026-10-04T10:00:00+09:00\x1fRewrite intro\x1e\n"
    "2222222222bbbbbbbbbb2222222222bbbbbbbbbb\x1fdongwook lee\x1fother@example.com\x1f2026-10-03T10:00:00+09:00\x1fMy edit\x1e\n"
    "3333333333cccccccccc3333333333cccccccccc\x1fSomeone\x1fDWLEE@Example.COM\x1f2026-10-02T10:00:00+09:00\x1fMy other edit\x1e\n"
    "4444444444dddddddddd4444444444dddddddddd\x1fPark\x1fpark@uni.ac.kr\x1f2026-09-01T10:00:00+09:00\x1fToo old\x1e\n"
)
PATCH = (
    "diff --git a/main.tex b/main.tex\n"
    "index 83db48f..bf269f4 100644\n"
    "--- a/main.tex\n"
    "+++ b/main.tex\n"
    "@@ -1,2 +1,2 @@\n"
    " \\section{Introduction}\n"
    "-Old first paragraph.\n"
    "+New first paragraph with motivation.\n"
)


class FakeGit:
    def __init__(self, fail_with: str | None = None):
        self.calls: list[list[str]] = []
        self.fail_with = fail_with

    def __call__(self, cmd, cwd):
        cmd = list(cmd)
        self.calls.append(cmd)
        sub = git_subcommand(cmd[1:])
        if self.fail_with and sub in ("clone", "fetch"):
            return subprocess.CompletedProcess(cmd, 128, "", self.fail_with)
        out = ""
        if sub == "clone":
            Path(cmd[-1], ".git").mkdir(parents=True)
        elif sub == "log":
            out = LOG
        elif sub == "show" and "--numstat" in cmd:
            out = "1\t1\tmain.tex\n-\t-\tfigures/plot.png\n"
        elif sub == "show" and "--patch" in cmd:
            out = PATCH
        elif sub == "show":
            out = " main.tex          | 2 +-\n figures/plot.png  | Bin 0 -> 1234 bytes\n"
        elif sub == "ls-tree":
            out = "100644 blob bf269f4 1234\tmain.tex\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")


def env(tmp_path, **extra):
    base = {
        "OVERLEAF_GIT_TOKEN": TOKEN,
        "OVERLEAF_PROJECTS": f"My Paper={PROJECT}",
        "OVERLEAF_CACHE_DIR": str(tmp_path / "cache"),
        "MY_NAMES": "Dongwook Lee",
        "MY_EMAILS": "dwlee@example.com",
    }
    base.update(extra)
    return base


def test_is_mine_is_case_insensitive_on_names_and_emails():
    assert is_mine("dongwook lee", "x@y.z", ["Dongwook Lee"], [])
    assert is_mine("  Dongwook   LEE ", "", ["Dongwook Lee"], [])
    assert is_mine("Anyone", "DWLee@Example.com", [], ["dwlee@example.com"])
    assert not is_mine("Kim Coauthor", "kim@uni.ac.kr", ["Dongwook Lee"], ["dwlee@example.com"])
    assert not is_mine("", "", ["Dongwook Lee"], ["dwlee@example.com"])


def test_parse_log_and_projects():
    commits = parse_log(LOG)
    assert [c.author_name for c in commits] == ["Kim Coauthor", "dongwook lee", "Someone", "Park"]
    assert commits[0].subject == "Rewrite intro"
    assert commits[0].date.tzinfo is not None
    projects, invalid = config.parse_overleaf_projects(f"Paper A={PROJECT}, {PROJECT}, bad id=../etc")
    assert [(p.name, p.project_id) for p in projects] == [("Paper A", PROJECT), (PROJECT, PROJECT)]
    assert invalid == ["bad id=../etc"]


def test_split_patch_drops_index_lines():
    sections = split_patch(PATCH)
    assert [path for path, _ in sections] == ["main.tex"]
    assert "index 83db48f" not in sections[0][1]


def test_run_check_reports_only_coauthor_commits_with_diffs(tmp_path):
    runner = FakeGit()
    store = StateStore(tmp_path / "state.json")
    payload = run_check(env=env(tmp_path), now=NOW, runner=runner, store=store)

    assert payload["configured"] is True and payload["ok"] is True
    project = payload["projects"][0]
    assert project["name"] == "My Paper"
    assert project["coauthor_commit_count"] == 1
    assert project["my_commits_excluded"] == 2
    [coauthor] = project["coauthors"]
    assert coauthor["name"] == "Kim Coauthor"
    [commit] = coauthor["commits"]
    assert commit["subject"] == "Rewrite intro"
    assert "main.tex" in commit["diffstat"]
    [file_diff] = commit["files"]
    assert file_diff["path"] == "main.tex"
    assert "+New first paragraph with motivation." in file_diff["diff"]
    assert store.last_checked(f"overleaf:{PROJECT}") == NOW


def test_token_only_travels_in_auth_header_of_network_commands(tmp_path):
    runner = FakeGit()
    payload = run_check(env=env(tmp_path), now=NOW, runner=runner, store=StateStore(tmp_path / "s.json"))
    header = auth_header(TOKEN)
    assert header == "Authorization: Basic " + base64.b64encode(f"git:{TOKEN}".encode()).decode()
    for cmd in runner.calls:
        joined = " ".join(cmd)
        assert TOKEN not in joined  # raw token never on the command line
        if git_subcommand(cmd[1:]) in ("clone", "fetch"):
            assert f"http.extraHeader={header}" in cmd
        else:
            assert "http.extraHeader" not in joined
    clone = next(c for c in runner.calls if git_subcommand(c[1:]) == "clone")
    assert f"https://git.overleaf.com/{PROJECT}" in clone  # no credentials in the remote URL
    assert TOKEN not in json.dumps(payload)


def test_git_failure_is_scrubbed_and_state_untouched(tmp_path):
    encoded = base64.b64encode(f"git:{TOKEN}".encode()).decode()
    runner = FakeGit(
        fail_with=f"fatal: Authentication failed for 'https://git:{TOKEN}@git.overleaf.com/x' (Authorization: Basic {encoded})"
    )
    store = StateStore(tmp_path / "state.json")
    payload = run_check(env=env(tmp_path), now=NOW, runner=runner, store=store)
    text = json.dumps(payload, ensure_ascii=False)
    assert payload["ok"] is False
    assert payload["projects"][0]["ok"] is False
    assert TOKEN not in text and encoded not in text
    assert "git clone 실패" in payload["projects"][0]["error"]
    assert store.last_checked(f"overleaf:{PROJECT}") is None


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


def _git(*args, cwd, name="Kim Coauthor", email="kim@uni.ac.kr"):
    env = {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
        "HOME": str(cwd),
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_real_git_output_is_parsed(tmp_path):
    """End-to-end against a local repository (no network): the cache clone's
    origin points at a local path, so ``sync_repo`` only runs ``git fetch``."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git("init", "-q", "-b", "master", cwd=upstream)
    (upstream / "main.tex").write_text("\\section{Intro}\nOld paragraph.\n", encoding="utf-8")
    (upstream / "logo.png").write_bytes(b"\x89PNG\r\n")
    _git("add", ".", cwd=upstream, name="Dongwook Lee", email="dwlee@example.com")
    _git("commit", "-q", "-m", "Initial", cwd=upstream, name="Dongwook Lee", email="dwlee@example.com")

    cache = tmp_path / "cache"
    cache.mkdir()
    _git("clone", "-q", "--no-checkout", str(upstream), str(cache / PROJECT), cwd=tmp_path)

    (upstream / "main.tex").write_text("\\section{Intro}\nNew paragraph with motivation.\n", encoding="utf-8")
    (upstream / "refs.bib").write_text("@article{a,\n title={A}\n}\n", encoding="utf-8")
    _git("add", ".", cwd=upstream)
    _git("commit", "-q", "-m", "Rewrite intro and add refs", cwd=upstream)
    (upstream / "notes.md").write_text("mine\n", encoding="utf-8")
    _git("add", ".", cwd=upstream, name="DONGWOOK LEE", email="DWLee@Example.com")
    _git("commit", "-q", "-m", "My notes", cwd=upstream, name="DONGWOOK LEE", email="DWLee@Example.com")

    payload = run_check(
        env=env(tmp_path, OVERLEAF_CACHE_DIR=str(cache)),
        store=StateStore(tmp_path / "state.json"),
    )
    project = payload["projects"][0]
    assert project["ok"] is True, project
    assert project["my_commits_excluded"] == 2
    [coauthor] = project["coauthors"]
    assert (coauthor["name"], coauthor["email"]) == ("Kim Coauthor", "kim@uni.ac.kr")
    [commit] = coauthor["commits"]
    assert commit["subject"] == "Rewrite intro and add refs"
    assert "main.tex" in commit["diffstat"] and "refs.bib" in commit["diffstat"]
    diffs = {f["path"]: f["diff"] for f in commit["files"]}
    assert set(diffs) == {"main.tex", "refs.bib"}
    assert "+New paragraph with motivation." in diffs["main.tex"]
    assert "-Old paragraph." in diffs["main.tex"]
    assert "+@article{a," in diffs["refs.bib"]
