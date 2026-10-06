"""``check_overleaf_updates``: which Overleaf projects co-authors edited, via git.

List-only by design: only ``git log`` metadata (author name, email, date) is
read. Commit contents, diffstats and diffs are never fetched, which keeps each
check to a few tokens per project. The user opens the projects themselves.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from claude_agent_sdk import ToolAnnotations, tool

from .. import config
from ..state import StateStore, ensure_aware, resolve_since, utcnow
from .common import (
    MAX_RESULT_SIZE_CHARS,
    MAX_SINCE_HOURS,
    SINCE_HOURS_SCHEMA,
    int_arg,
    safe_error,
    scrub,
    to_local_iso,
    tool_result,
    unconfigured,
)

SOURCE_PREFIX = "overleaf:"
OVERLEAF_GIT_BASE = "https://git.overleaf.com"
OVERLEAF_PROJECT_URL = "https://www.overleaf.com/project"
GIT_TIMEOUT_SECONDS = 180
TIME_FORMAT = "%Y-%m-%d %H:%M"
NO_NAME = "(이름 없음)"
# Author name, email and ISO date only. Unit/record separators keep the log
# machine-parseable even with odd names.
LOG_FORMAT = "%an%x1f%ae%x1f%aI%x1e"
# The git remote URL carries the project id; error messages show a label instead.
_GIT_URL_RE = re.compile(r"(?i)https?://(?:[^/\s@]+@)?git\.overleaf\.com/\S*")
GIT_URL_LABEL = "<Overleaf git 주소>"
INVALID_ENTRY_ERROR = "OVERLEAF_PROJECTS 항목 형식이 잘못됨 ('이름=프로젝트ID' 형식으로 적어 주세요)"

Runner = Callable[[Sequence[str], "Path | None"], subprocess.CompletedProcess]


class GitError(Exception):
    """A git failure whose message has already been scrubbed of secrets."""


@dataclass
class Commit:
    author_name: str
    author_email: str
    date: datetime


def _default_runner(cmd: Sequence[str], cwd: Path | None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"  # never block on a credential prompt
    return subprocess.run(
        list(cmd),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=GIT_TIMEOUT_SECONDS,
        env=env,
        check=False,
    )


def auth_header(token: str) -> str:
    encoded = base64.b64encode(f"git:{token}".encode()).decode()
    return f"Authorization: Basic {encoded}"


class GitClient:
    """Runs git with the Overleaf token passed per-command (``-c http.extraHeader``).

    The token is never written to ``.git/config`` and never appears in the
    messages of the exceptions this class raises.
    """

    def __init__(self, token: str, runner: Runner | None = None):
        self._header = auth_header(token)
        self._secrets = [token, self._header.split(" ", 2)[2]]
        self._runner = runner or _default_runner

    def run(self, args: Sequence[str], cwd: Path | None = None, auth: bool = False) -> str:
        cmd = ["git", "--literal-pathspecs", "-c", "core.quotepath=false"]
        if auth:
            cmd += ["-c", f"http.extraHeader={self._header}"]
        cmd += list(args)
        action = git_subcommand(args)
        try:
            proc = self._runner(cmd, cwd)
        except FileNotFoundError:
            raise GitError("git이 설치되어 있지 않습니다. git을 설치해 주세요.") from None
        except subprocess.TimeoutExpired:
            raise GitError(f"git {action} 시간 초과({GIT_TIMEOUT_SECONDS}초)") from None
        except OSError as exc:
            raise GitError(f"git {action} 실행 실패: {safe_error(exc, self._secrets)}") from None
        if proc.returncode != 0:
            stderr = scrub((proc.stderr or "").strip(), self._secrets)
            raise GitError(f"git {action} 실패(코드 {proc.returncode}): {stderr[-400:]}")
        return proc.stdout or ""


def git_subcommand(args: Sequence[str]) -> str:
    """First non-option argument, skipping the values of ``-C`` / ``-c``."""
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in ("-C", "-c"):
            skip_next = True
            continue
        if not arg.startswith("-"):
            return arg
    return "git"


def _norm(value: str) -> str:
    return " ".join(value.split()).casefold()


def is_mine(name: str, email: str, my_names: Sequence[str], my_emails: Sequence[str]) -> bool:
    """Case-insensitive match of a commit author against MY_NAMES / MY_EMAILS."""
    names = {_norm(n) for n in my_names if n.strip()}
    emails = {_norm(e) for e in my_emails if e.strip()}
    return (bool(name) and _norm(name) in names) or (bool(email) and _norm(email) in emails)


def parse_log(output: str) -> list[Commit]:
    commits: list[Commit] = []
    for record in output.split("\x1e"):
        record = record.strip("\r\n")
        if not record.strip():
            continue
        parts = record.split("\x1f")
        if len(parts) < 3:
            continue
        name, email, date_raw = parts[0], parts[1], parts[2]
        try:
            date = ensure_aware(datetime.fromisoformat(date_raw.strip().replace("Z", "+00:00")))
        except ValueError:
            continue
        commits.append(Commit(name.strip(), email.strip(), date))
    return commits


def project_link(project_id: str) -> str:
    """Overleaf web page of the project (not the git URL)."""
    return f"{OVERLEAF_PROJECT_URL}/{project_id}"


def format_time(dt: datetime, tz: tzinfo) -> str:
    return ensure_aware(dt).astimezone(tz).strftime(TIME_FORMAT)


def hide_git_url(text: str) -> str:
    return _GIT_URL_RE.sub(GIT_URL_LABEL, text)


def sync_repo(git: GitClient, project_id: str, cache_dir: Path) -> Path:
    repo = cache_dir / project_id
    if (repo / ".git").is_dir():
        git.run(["-C", str(repo), "fetch", "--quiet", "--prune", "origin"], auth=True)
        return repo
    if repo.exists() and any(repo.iterdir()):
        raise GitError(f"캐시 폴더가 git 저장소가 아닙니다: {repo} (지우고 다시 시도)")
    repo.parent.mkdir(parents=True, exist_ok=True)
    git.run(
        ["clone", "--quiet", "--no-checkout", f"{OVERLEAF_GIT_BASE}/{project_id}", str(repo)],
        auth=True,
    )
    return repo


def remote_ref(git: GitClient, repo: Path) -> str:
    for ref in ("origin/HEAD", "origin/master", "origin/main"):
        try:
            git.run(["-C", str(repo), "rev-parse", "--verify", "--quiet", ref])
            return ref
        except GitError:
            continue
    return "FETCH_HEAD"


def summarize_editors(commits: Sequence[Commit], tz: tzinfo) -> list[dict[str, Any]]:
    """One entry per co-author: name, last edit time and commit count, newest first.

    Commits are grouped by author name (case- and space-insensitive); the
    name is shown as written in the most recent commit.
    """
    people: dict[str, dict[str, Any]] = {}
    for commit in sorted(commits, key=lambda c: c.date, reverse=True):
        shown = commit.author_name or commit.author_email or NO_NAME
        person = people.get(_norm(shown))
        if person is None:
            people[_norm(shown)] = {"name": shown, "last": commit.date, "edits": 1}
        else:
            person["edits"] += 1
    ordered = sorted(people.values(), key=lambda p: (-p["last"].timestamp(), p["name"]))
    return [{"name": p["name"], "last_edit": format_time(p["last"], tz), "edits": p["edits"]} for p in ordered]


def coauthor_commits(
    git: GitClient,
    project: config.OverleafProject,
    cache_dir: Path,
    since: datetime,
    my_names: Sequence[str],
    my_emails: Sequence[str],
) -> list[Commit]:
    """Co-author commits after ``since``, from ``git log`` metadata only."""
    repo = sync_repo(git, project.project_id, cache_dir)
    ref = remote_ref(git, repo)
    log = git.run(
        ["-C", str(repo), "log", ref, "--no-merges", f"--since={since.isoformat()}", f"--format={LOG_FORMAT}"]
    )
    commits = [c for c in parse_log(log) if ensure_aware(c.date) > since]
    return [c for c in commits if not is_mine(c.author_name, c.author_email, my_names, my_emails)]


def run_check(
    since_hours: int = 0,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
    runner: Runner | None = None,
    store: StateStore | None = None,
) -> dict[str, Any]:
    cfg = config.load_overleaf_config(env)
    if not cfg.configured:
        return unconfigured(cfg.missing, config.overleaf_hint(cfg.missing))

    now = ensure_aware(now or utcnow())
    store = store or StateStore(config.get_state_path(env))
    lookback_days = config.get_lookback_days(env)
    tz = config.get_timezone(env)
    git = GitClient(cfg.token, runner)
    secrets = config.secret_values(env)

    edited: list[tuple[datetime, dict[str, Any]]] = []
    unchanged: list[str] = []
    errors: list[dict[str, str]] = [{"name": entry, "error": INVALID_ENTRY_ERROR} for entry in cfg.invalid_entries]
    window_starts: list[datetime] = []
    for project in cfg.projects:
        key = SOURCE_PREFIX + project.project_id
        since, _basis = resolve_since(since_hours, store.last_checked(key), now, lookback_days)
        window_starts.append(since)
        try:
            commits = coauthor_commits(git, project, cfg.cache_dir, since, cfg.my_names, cfg.my_emails)
        except Exception as exc:  # noqa: BLE001 - one project failing must not hide the others
            message = str(exc) if isinstance(exc, GitError) else safe_error(exc, secrets)
            errors.append({"name": project.name, "error": hide_git_url(scrub(message, secrets))})
            continue
        store.mark_checked(key, now)
        if not commits:
            unchanged.append(project.name)
            continue
        latest = max(c.date for c in commits)
        edited.append(
            (
                latest,
                {
                    "name": project.name,
                    "link": project_link(project.project_id),
                    "edited_by": summarize_editors(commits, tz),
                },
            )
        )

    edited.sort(key=lambda item: (-item[0].timestamp(), item[1]["name"]))
    return {
        "configured": True,
        # Projects keep their own "last checked" time; report the widest window.
        "since": to_local_iso(min(window_starts) if window_starts else now, tz),
        "projects": [project for _latest, project in edited],
        "unchanged": unchanged,
        "errors": errors,
    }


@tool(
    "check_overleaf_updates",
    (
        "OVERLEAF_PROJECTS의 각 Overleaf 프로젝트에서 기간 내 공저자(나 제외)가 편집했는지 목록만 확인한다. "
        "프로젝트(최근 편집 순, 프로젝트 링크 포함)마다 편집한 사람, 마지막 편집 시각, 편집(커밋) 수를 "
        "짧은 JSON으로 돌려준다. 변경 없는 프로젝트는 unchanged, 확인에 실패한 프로젝트는 errors에 있다. "
        "원고 내용·diff는 읽지 않는다. 읽기 전용. configured=false면 설정이 없는 것이니 재시도하지 말 것."
    ),
    SINCE_HOURS_SCHEMA,
    annotations=ToolAnnotations(readOnlyHint=True, maxResultSizeChars=MAX_RESULT_SIZE_CHARS),
)
async def check_overleaf_updates(args: dict[str, Any]) -> dict[str, Any]:
    since_hours = int_arg(args.get("since_hours"), 0, 0, MAX_SINCE_HOURS)
    try:
        payload = await asyncio.to_thread(run_check, since_hours)
    except Exception as exc:  # noqa: BLE001 - last line of defence
        payload = {"configured": True, "ok": False, "error": hide_git_url(safe_error(exc))}
    return tool_result(payload)
