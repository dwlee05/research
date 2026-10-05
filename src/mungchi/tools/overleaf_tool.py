"""``check_overleaf_updates``: co-author commits in Overleaf projects via git."""

from __future__ import annotations

import asyncio
import base64
import os
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from claude_agent_sdk import ToolAnnotations, tool

from .. import config
from ..state import StateStore, describe_basis, ensure_aware, resolve_since, utcnow
from .common import (
    MAX_RESULT_SIZE_CHARS,
    MAX_SINCE_HOURS,
    MAX_TEXT_FILE_BYTES,
    SINCE_HOURS_SCHEMA,
    OutputBudget,
    int_arg,
    is_text_path,
    safe_error,
    scrub,
    shrink_to_limit,
    to_local_iso,
    tool_result,
    unconfigured,
)

SOURCE_PREFIX = "overleaf:"
OVERLEAF_GIT_BASE = "https://git.overleaf.com"
GIT_TIMEOUT_SECONDS = 180
MAX_COMMITS_WITH_DIFF = 30
MAX_DIFFSTAT_CHARS = 2_000
# Unit/record separators keep the log machine-parseable even with odd subjects.
LOG_FORMAT = "%H%x1f%an%x1f%ae%x1f%aI%x1f%s%x1e"

Runner = Callable[[Sequence[str], "Path | None"], subprocess.CompletedProcess]


class GitError(Exception):
    """A git failure whose message has already been scrubbed of secrets."""


@dataclass
class Commit:
    sha: str
    author_name: str
    author_email: str
    date: datetime
    subject: str


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


def is_mine(name: str, email: str, my_names: Sequence[str], my_emails: Sequence[str]) -> bool:
    """Case-insensitive match of a commit author against MY_NAMES / MY_EMAILS."""

    def norm(value: str) -> str:
        return " ".join(value.split()).casefold()

    names = {norm(n) for n in my_names if n.strip()}
    emails = {norm(e) for e in my_emails if e.strip()}
    return (bool(name) and norm(name) in names) or (bool(email) and norm(email) in emails)


def parse_log(output: str) -> list[Commit]:
    commits: list[Commit] = []
    for record in output.split("\x1e"):
        record = record.strip("\r\n")
        if not record.strip():
            continue
        parts = record.split("\x1f")
        if len(parts) < 5:
            continue
        sha, name, email, date_raw, subject = parts[0], parts[1], parts[2], parts[3], "\x1f".join(parts[4:])
        try:
            date = ensure_aware(datetime.fromisoformat(date_raw.strip().replace("Z", "+00:00")))
        except ValueError:
            continue
        commits.append(Commit(sha.strip(), name.strip(), email.strip(), date, subject.strip()))
    return commits


def parse_numstat(output: str) -> list[str]:
    """Paths from ``git show --numstat`` output."""
    paths: list[str] = []
    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[2]:
            paths.append(parts[2])
    return paths


def parse_ls_tree_sizes(output: str) -> dict[str, int]:
    """``git ls-tree -r -l`` lines: ``<mode> <type> <sha> <size>\t<path>``."""
    sizes: dict[str, int] = {}
    for line in output.splitlines():
        meta, _, path = line.partition("\t")
        fields = meta.split()
        if len(fields) == 4 and fields[3].isdigit():
            sizes[path] = int(fields[3])
    return sizes


def split_patch(patch: str) -> list[tuple[str, str]]:
    """Split a multi-file patch into ``(path, section)`` pairs."""
    sections: list[list[str]] = []
    for line in patch.splitlines():
        if line.startswith("diff --git ") or not sections:
            sections.append([])
        if line.startswith("index "):
            continue  # blob hashes are noise for the reader
        sections[-1].append(line)
    result: list[tuple[str, str]] = []
    for section in sections:
        path = ""
        for line in section:
            if line.startswith("+++ b/"):
                path = line[len("+++ b/") :]
                break
            if line.startswith("--- a/"):
                path = line[len("--- a/") :]
        if not path and section and section[0].startswith("diff --git "):
            path = section[0].rsplit(" b/", 1)[-1]
        if any(line.strip() for line in section):
            result.append((path, "\n".join(section)))
    return result


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


def commit_details(
    git: GitClient, repo: Path, commit: Commit, label: str, budget: OutputBudget
) -> dict[str, Any]:
    base = ["-C", str(repo)]
    stat = git.run(base + ["show", "--stat=120", "--format=", "--no-renames", commit.sha]).strip()
    if len(stat) > MAX_DIFFSTAT_CHARS:
        stat = stat[:MAX_DIFFSTAT_CHARS] + "\n… (diffstat 일부 생략)"
    details: dict[str, Any] = {"diffstat": stat}

    paths = parse_numstat(git.run(base + ["show", "--numstat", "--format=", "--no-renames", commit.sha]))
    text_paths = [p for p in paths if is_text_path(p)]
    if not text_paths:
        details["files"] = []
        return details
    sizes = parse_ls_tree_sizes(git.run(base + ["ls-tree", "-r", "-l", commit.sha, "--", *text_paths]))
    small = [p for p in text_paths if sizes.get(p, 0) <= MAX_TEXT_FILE_BYTES]
    skipped = [p for p in text_paths if p not in small]
    files: list[dict[str, Any]] = []
    if small:
        patch = git.run(
            base
            + ["show", "--format=", "--patch", "--no-color", "--no-renames", "--unified=2", commit.sha, "--", *small]
        )
        for path, section in split_patch(patch):
            files.append({"path": path, **budget.fit(f"{label}:{path}@{commit.sha[:8]}", section)})
    files.extend({"path": p, "diff": None, "diff_note": "200KB를 넘는 파일이라 diff 생략"} for p in skipped)
    details["files"] = files
    return details


def check_project(
    git: GitClient,
    project: config.OverleafProject,
    cache_dir: Path,
    since: datetime,
    my_names: Sequence[str],
    my_emails: Sequence[str],
    tz: tzinfo,
    budget: OutputBudget,
) -> dict[str, Any]:
    repo = sync_repo(git, project.project_id, cache_dir)
    ref = remote_ref(git, repo)
    log = git.run(
        ["-C", str(repo), "log", ref, "--no-merges", f"--since={since.isoformat()}", f"--format={LOG_FORMAT}"]
    )
    commits = [c for c in parse_log(log) if ensure_aware(c.date) > since]
    coauthor_commits = [c for c in commits if not is_mine(c.author_name, c.author_email, my_names, my_emails)]

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for index, commit in enumerate(coauthor_commits):
        item: dict[str, Any] = {
            "commit": commit.sha[:8],
            "date": to_local_iso(commit.date, tz),
            "subject": commit.subject,
        }
        if index < MAX_COMMITS_WITH_DIFF:
            try:
                item.update(commit_details(git, repo, commit, project.name, budget))
            except GitError as exc:
                item["diff_note"] = f"diff 가져오기 실패: {exc}"
        else:
            item["diff_note"] = f"diff는 최근 {MAX_COMMITS_WITH_DIFF}개 커밋까지만"
        grouped[(commit.author_name, commit.author_email)].append(item)

    return {
        "name": project.name,
        "project_id": project.project_id,
        "ok": True,
        "coauthor_commit_count": len(coauthor_commits),
        "my_commits_excluded": len(commits) - len(coauthor_commits),
        "coauthors": [
            {"name": name or "(이름 없음)", "email": email, "commits": items}
            for (name, email), items in sorted(grouped.items())
        ],
    }


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
    budget = OutputBudget()
    secrets = config.secret_values(env)

    projects: list[dict[str, Any]] = []
    for project in cfg.projects:
        key = SOURCE_PREFIX + project.project_id
        since, basis = resolve_since(since_hours, store.last_checked(key), now, lookback_days)
        window = {
            "since": to_local_iso(since, tz),
            "until": to_local_iso(now, tz),
            "basis": describe_basis(basis, lookback_days),
        }
        try:
            result = check_project(
                git, project, cfg.cache_dir, since, cfg.my_names, cfg.my_emails, tz, budget
            )
        except Exception as exc:  # noqa: BLE001 - one project failing must not hide the others
            message = str(exc) if isinstance(exc, GitError) else safe_error(exc, secrets)
            projects.append(
                {
                    "name": project.name,
                    "project_id": project.project_id,
                    "ok": False,
                    "window": window,
                    "error": scrub(message, secrets),
                }
            )
            continue
        store.mark_checked(key, now)
        result["window"] = window
        projects.append(result)

    payload: dict[str, Any] = {
        "configured": True,
        "ok": any(p.get("ok") for p in projects),
        "source": "overleaf",
        "projects": projects,
    }
    if cfg.invalid_entries:
        payload["invalid_project_entries"] = cfg.invalid_entries
    truncation = budget.report()
    if truncation:
        payload["truncation"] = truncation
    return shrink_to_limit(payload)


@tool(
    "check_overleaf_updates",
    (
        "OVERLEAF_PROJECTS의 각 Overleaf 프로젝트를 git으로 가져와, 기간 내 공저자 커밋(내 커밋 제외)을 "
        "공저자별로 묶어 커밋 시각·메시지·diffstat·텍스트 파일 diff(잘림 표시 포함)를 JSON으로 돌려준다. "
        "읽기 전용. configured=false면 설정이 없는 것이니 재시도하지 말 것."
    ),
    SINCE_HOURS_SCHEMA,
    annotations=ToolAnnotations(readOnlyHint=True, maxResultSizeChars=MAX_RESULT_SIZE_CHARS),
)
async def check_overleaf_updates(args: dict[str, Any]) -> dict[str, Any]:
    since_hours = int_arg(args.get("since_hours"), 0, 0, MAX_SINCE_HOURS)
    try:
        payload = await asyncio.to_thread(run_check, since_hours)
    except Exception as exc:  # noqa: BLE001 - last line of defence
        payload = {"configured": True, "ok": False, "source": "overleaf", "error": safe_error(exc)}
    return tool_result(payload)
