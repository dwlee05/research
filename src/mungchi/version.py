"""Which code is running: the git commit of this checkout (``abc1234``, ``abc1234-dirty``).

The Slack bots write it to the state file when they start (``running_version``,
``running_since``), and ``python -m mungchi service status`` compares it with
the repository's current commit, so a bot still running old code after
``git pull`` is easy to spot. Without git the package version (``v0.1.0``)
stands in. Nothing here raises.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Sequence

from . import __version__

GIT_TIMEOUT_SECONDS = 5.0
# ``git`` arguments -> its standard output, or None when git failed (missing, not a repository, timeout).
GitRunner = Callable[[Sequence[str]], "str | None"]

_GIT_VERSION_RE = re.compile(r"^[0-9a-f]{4,40}(?:-dirty)?$")
_HASH_RE = re.compile(r"^[0-9a-f]{4,40}$")


def _is_project_root(path: Path) -> bool:
    try:
        text = (path / "pyproject.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return re.search(r"""(?m)^\s*name\s*=\s*["']mungchi["']""", text) is not None


def detect_repo_dir(module_file: str | Path | None = None, cwd: str | Path | None = None) -> Path:
    """The project root (holding mungchi's ``pyproject.toml``), else the working directory.

    The package's own location is tried first (an editable install lives in
    ``<repo>/src/mungchi``), then the working directory and its parents.
    Symlinks are kept as written so the bot runs from the same path as before.
    """
    module_path = Path(os.path.abspath(module_file or __file__))
    here = Path(os.path.abspath(cwd or os.getcwd()))
    for candidate in (*module_path.parents, here, *here.parents):
        if _is_project_root(candidate):
            return candidate
    return here


def run_git(args: Sequence[str], *, timeout: float = GIT_TIMEOUT_SECONDS) -> str | None:
    """``git <args>``'s standard output, or None when it failed. Never raises, never waits long."""
    try:
        done = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return done.stdout if done.returncode == 0 else None


def git_version(repo_dir: str | Path, git: GitRunner | None = None) -> str | None:
    """``git rev-parse --short HEAD`` of ``repo_dir``, with ``-dirty`` for uncommitted changes to tracked files.

    None when git is missing or ``repo_dir`` is not a git checkout. ``git``
    defaults to ``run_git``.
    """
    git = git or run_git
    repo = str(repo_dir)
    head = (git(["-C", repo, "rev-parse", "--short", "HEAD"]) or "").strip()
    if not _HASH_RE.match(head):
        return None
    # --no-optional-locks: only look, never refresh (write) the index of the checkout.
    changes = git(["-C", repo, "--no-optional-locks", "status", "--porcelain", "--untracked-files=no"])
    return f"{head}-dirty" if changes and changes.strip() else head


def package_version() -> str:
    return f"v{__version__}"


def code_version(repo_dir: str | Path | None = None, *, git: GitRunner | None = None) -> str:
    """The running code: ``abc1234`` / ``abc1234-dirty``, or ``v0.1.0`` (package version) without git."""
    try:
        found = git_version(repo_dir if repo_dir is not None else detect_repo_dir(), git)
    except Exception:  # noqa: BLE001 - a version label must never stop the bots
        found = None
    return found or package_version()


def is_git_version(value: str | None) -> bool:
    """True for ``abc1234`` / ``abc1234-dirty`` (a commit), False for the package version or nothing."""
    return bool(value) and _GIT_VERSION_RE.match(value or "") is not None
