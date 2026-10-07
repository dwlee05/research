"""``python -m mungchi service ...``: keep the Slack bots running in the background on macOS.

Why an app: macOS (TCC) gives Calendar access to the *responsible app* of a
process. Started from Terminal, that is Terminal; a bare ``python`` started by
launchd has no usable responsible app and no usage description. So the bots
run inside a tiny AppleScript applet, ``~/Applications/MungchiBot.app``
("비서실 고뭉치"), with its own bundle id and calendar usage texts. Its
children (``run-bot.sh`` -> ``python -m mungchi service run``) inherit it as
their responsible process, so the permission dialog and the grant belong to
the applet.

A LaunchAgent (``~/Library/LaunchAgents/local.mungchi.bot.plist``) opens the
applet with ``open -W -g`` at login and again whenever it ends (KeepAlive).

Every external command (osacompile, codesign, launchctl, pgrep, pkill) goes
through an injectable runner, and the generated files (AppleScript source,
``run-bot.sh``, Info.plist edits, LaunchAgent plist) come from pure functions,
so all of it is tested on Linux. The macOS behaviour itself (TCC attribution,
launchd, osacompile) is not exercised by the tests.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import plistlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

from . import config
from . import main as main_module
from . import version
from .main import KoreanArgumentParser, KoreanHelpFormatter
from .state import StateStore
from .tools import macos_calendar
from .tools.common import safe_error, scrub
from .version import GitRunner, detect_repo_dir

LABEL = "local.mungchi.bot"
APP_NAME = "MungchiBot"
APP_DISPLAY_NAME = "비서실 고뭉치"
CALENDAR_USAGE_TEXT = "'일정' 에이전트가 오늘·내일 일정을 확인하려고 캘린더를 읽습니다. 일정을 바꾸지는 않습니다."

COMMAND = "python -m mungchi service"
# ``pgrep -f`` / ``pkill -f`` patterns (matched against the full command line).
SERVICE_PROCESS_PATTERN = "mungchi service run"
FOREGROUND_PROCESS_PATTERN = "mungchi slack"

THROTTLE_SECONDS = 30
# The user has to notice the dialog and click; never wait forever though.
SERVICE_REQUEST_TIMEOUT_SECONDS = 300
# Extra time the whole calendar check may take beyond the dialog timeout.
PREFLIGHT_MARGIN_SECONDS = 60.0
DEFAULT_LOG_LINES = 50
STATUS_LOG_LINES = 10
STOP_WAIT_SECONDS = 10.0
POLL_SECONDS = 0.5
BOOTSTRAP_RETRY_SECONDS = 2.0

# Every calendar line ``service run`` writes starts with this, so ``status``
# can show what the applet itself was granted (not the Terminal running ``status``).
CALENDAR_LOG_KEY = "캘린더 접근 권한:"
SERVICE_LOG_TAG = "[서비스]"

NOT_MAC_TEXT = (
    "[오류] service 명령(백그라운드 서비스)은 macOS에서만 쓸 수 있습니다. "
    "다른 운영체제에서는 README '백그라운드로 실행하기'의 Linux(systemd) 안내를 보세요."
)
SERVICE_PERMISSION_HINT = (
    f"Mac 캘린더 앱을 읽을 수 없습니다. {macos_calendar.SETTINGS_PATH}에서 '{APP_DISPLAY_NAME}'(또는 {APP_NAME})를 "
    f"'전체 접근'으로 바꾼 뒤 {COMMAND} restart 를 실행하세요."
)
SLEEP_TIP = (
    "[팁] Mac이 잠자기에 들어가면 봇도 멈춥니다(놓친 아침 브리핑은 깨어난 뒤 기본 12:00 전까지 보냅니다). "
    "시스템 설정 → 에너지에서 '디스플레이가 꺼져 있을 때 자동으로 잠자기 방지'를 켜 두세요."
)

# Settings a shell may export that the service (started by launchd) never sees.
SHELL_ONLY_NAMES = tuple(
    dict.fromkeys(
        (
            *config.CLAUDE_ENV_VARS,
            "MUNGCHI_MODEL",
            *config.SECRET_ENV_VARS,
            "SLACK_ALLOWED_USER_IDS",
            "SSL_CERT_FILE",
        )
    )
)

# ``launchctl bootout`` of a job that is not loaded.
_NOT_LOADED_RE = re.compile(r"(?i)no such process|could not find|not loaded|\b113\b")
_STATE_RE = re.compile(r"(?m)^\s*state\s*=\s*(.+?)\s*$")
_LOG_TIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


# ---------------------------------------------------------------- command runner


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def detail(self) -> str:
        """One line of the command's own error text (scrubbed), for Korean messages."""
        text = " ".join((self.stderr or self.stdout or "").split())
        return scrub(text)[:300] or f"종료 코드 {self.returncode}"


Runner = Callable[[Sequence[str]], CommandResult]


def run_command(args: Sequence[str], timeout: float = 120) -> CommandResult:
    """Run an external command (no shell) and capture its output."""
    try:
        done = subprocess.run(
            list(args), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
        )
    except FileNotFoundError:
        return CommandResult(127, "", f"{args[0]}: 명령을 찾을 수 없습니다")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"{args[0]}: 시간 초과")
    return CommandResult(done.returncode, done.stdout or "", done.stderr or "")


# ---------------------------------------------------------------- pure generators


def applescript_string(text: str) -> str:
    """``text`` as an AppleScript string literal (backslash, quote and control characters escaped)."""
    escaped = (
        text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    )
    return f'"{escaped}"'


def applet_source(script_path: str | Path) -> str:
    """AppleScript of the applet: run ``run-bot.sh`` and swallow its exit status.

    Without ``try`` a non-zero exit (a crash, or ``service stop``) would show
    an error dialog; with it the applet just quits and launchd starts it again.
    """
    return (
        "try\n"
        f"    do shell script quoted form of {applescript_string(str(script_path))}\n"
        "end try\n"
    )


def run_bot_script(repo_dir: str | Path, python: str | Path) -> str:
    """``Contents/Resources/run-bot.sh``: go to the repository and start the service process.

    The log directory is created first so that even a failing ``cd`` leaves a
    line in ``bot.log``.
    """
    repo = shlex.quote(str(repo_dir))
    return (
        "#!/bin/bash\n"
        "# 비서실 고뭉치 Slack 봇 서비스 실행 스크립트 (python -m mungchi service install 이 만든 파일)\n"
        'LOG_DIR="$HOME/Library/Logs/mungchi"\n'
        'mkdir -p "$LOG_DIR"\n'
        f'cd {repo} || {{ echo "{SERVICE_LOG_TAG} 저장소 폴더로 이동하지 못했습니다:" {repo} >> "$LOG_DIR/bot.log"; exit 1; }}\n'
        f'exec {shlex.quote(str(python))} -m mungchi service run >> "$LOG_DIR/bot.log" 2>&1\n'
    )


def applet_info(info: Mapping[str, Any]) -> dict[str, Any]:
    """The applet's Info.plist with our identity and calendar usage texts."""
    updated = dict(info)
    updated.update(
        {
            "CFBundleIdentifier": LABEL,
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_DISPLAY_NAME,
            # No Dock icon, no App Nap.
            "LSUIElement": True,
            "NSAppSleepDisabled": True,
            "NSCalendarsUsageDescription": CALENDAR_USAGE_TEXT,
            "NSCalendarsFullAccessUsageDescription": CALENDAR_USAGE_TEXT,
        }
    )
    return updated


def launch_agent(app_path: str | Path, log_path: str | Path) -> dict[str, Any]:
    """The LaunchAgent: open the applet at login and whenever it has ended.

    ``open -W`` waits until the applet quits, so launchd sees the bot's end
    and starts it again after ``ThrottleInterval``. No ``ProcessType``: the
    job's own process is only ``open``, the applet is started by LaunchServices.
    """
    return {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/open", "-W", "-g", str(app_path)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": THROTTLE_SECONDS,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }


# ---------------------------------------------------------------- helpers


def read_env_file(path: Path) -> dict[str, str]:
    from dotenv import dotenv_values

    return {key: value or "" for key, value in dotenv_values(path).items()}


def tail_lines(path: Path, count: int, block_size: int = 65536) -> list[str]:
    """The last ``count`` lines of a (possibly large) UTF-8 text file."""
    if count <= 0:
        return []
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        data = b""
        while position > 0 and data.count(b"\n") <= count:
            step = min(block_size, position)
            position -= step
            handle.seek(position)
            data = handle.read(step) + data
    return [line.decode("utf-8", "replace") for line in data.splitlines()[-count:]]


def last_calendar_record(path: Path) -> tuple[str, str] | None:
    """``(time, status text)`` of the newest calendar line ``service run`` wrote, if any."""
    marker = f"{SERVICE_LOG_TAG} {CALENDAR_LOG_KEY}".encode()
    found: bytes | None = None
    try:
        with path.open("rb") as handle:
            for raw in handle:
                if marker in raw:
                    found = raw
    except OSError:
        return None
    if found is None:
        return None
    line = found.decode("utf-8", "replace").rstrip("\r\n")
    when = _LOG_TIME_RE.match(line)
    text = line.split(CALENDAR_LOG_KEY, 1)[1].strip()
    return (when.group(1) if when else "", text)


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


# ---------------------------------------------------------------- the service


class Service:
    """Install, control and inspect the background service.

    Everything outside Python goes through ``runner`` (and ``git``, for the
    repository's commit); ``home``, ``uid``, ``platform``, the interpreter
    paths, ``sleep`` and ``out`` are injectable so tests never touch the real
    system.
    """

    def __init__(
        self,
        *,
        runner: Runner | None = None,
        home: str | Path | None = None,
        uid: int | None = None,
        platform: str | None = None,
        out: TextIO | None = None,
        sleep: Callable[[float], None] | None = None,
        python: str | None = None,
        prefix: str | None = None,
        base_prefix: str | None = None,
        repo_dir: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
        stop_wait_seconds: float = STOP_WAIT_SECONDS,
        git: GitRunner | None = None,
    ):
        self.runner = run_command if runner is None else runner
        # Only ``status`` asks git (read-only) which commit the repository is at.
        self.git = git
        self.home = Path(home) if home is not None else Path.home()
        self._uid = uid
        self.platform = config.current_platform() if platform is None else platform
        self.out = out
        self.sleep = time.sleep if sleep is None else sleep
        self.python = python or os.path.abspath(sys.executable)
        self.prefix = sys.prefix if prefix is None else prefix
        self.base_prefix = sys.base_prefix if base_prefix is None else base_prefix
        self._repo_dir = Path(repo_dir) if repo_dir is not None else None
        self.environ = os.environ if environ is None else environ
        self.stop_wait_seconds = stop_wait_seconds

    # -- paths

    @property
    def uid(self) -> int:
        if self._uid is None:
            self._uid = os.getuid()
        return self._uid

    @property
    def repo_dir(self) -> Path:
        if self._repo_dir is None:
            self._repo_dir = detect_repo_dir()
        return self._repo_dir

    @property
    def app_path(self) -> Path:
        return self.home / "Applications" / f"{APP_NAME}.app"

    @property
    def run_script_path(self) -> Path:
        return self.app_path / "Contents" / "Resources" / "run-bot.sh"

    @property
    def info_plist_path(self) -> Path:
        return self.app_path / "Contents" / "Info.plist"

    @property
    def plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    @property
    def log_dir(self) -> Path:
        return self.home / "Library" / "Logs" / "mungchi"

    @property
    def bot_log(self) -> Path:
        return self.log_dir / "bot.log"

    @property
    def launchd_log(self) -> Path:
        return self.log_dir / "launchd.log"

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    @property
    def target(self) -> str:
        return f"{self.domain}/{LABEL}"

    # -- small helpers

    def say(self, line: str = "") -> None:
        print(line, file=self.out or sys.stdout, flush=True)

    def show(self, path: Path) -> str:
        """``path`` with the home directory written as ``~``."""
        try:
            return "~/" + path.relative_to(self.home).as_posix()
        except ValueError:
            return str(path)

    def run(self, *args: str) -> CommandResult:
        return self.runner(list(args))

    def require_macos(self) -> bool:
        if self.platform == "darwin":
            return True
        self.say(NOT_MAC_TEXT)
        return False

    @property
    def installed(self) -> bool:
        return self.plist_path.is_file() and self.app_path.is_dir()

    def pgrep(self, pattern: str) -> list[int]:
        """PIDs of this user's processes whose full command line matches ``pattern``."""
        result = self.run("pgrep", "-u", str(self.uid), "-f", pattern)
        if not result.ok:  # 1: nothing matched
            return []
        own = os.getpid()
        return [int(token) for token in result.stdout.split() if token.isdigit() and int(token) != own]

    def launchd_state(self) -> tuple[bool, str]:
        """``(loaded, state)`` from ``launchctl print``; state is "" when not reported."""
        result = self.run("launchctl", "print", self.target)
        if not result.ok:
            return False, ""
        match = _STATE_RE.search(result.stdout)
        return True, match.group(1) if match else ""

    def bootout(self) -> tuple[str, CommandResult]:
        """``("stopped" | "not_loaded" | "failed", result)``; never raises."""
        result = self.run("launchctl", "bootout", self.target)
        if result.ok:
            return "stopped", result
        if result.returncode in (3, 113) or _NOT_LOADED_RE.search(result.stderr + result.stdout):
            return "not_loaded", result
        return "failed", result

    def bootstrap(self) -> CommandResult:
        result = self.run("launchctl", "bootstrap", self.domain, str(self.plist_path))
        if result.ok:
            return result
        if self.launchd_state()[0]:  # it is loaded after all (e.g. loaded twice)
            return CommandResult(0)
        # A bootstrap right after a bootout can fail while launchd is still tearing down.
        self.sleep(BOOTSTRAP_RETRY_SECONDS)
        return self.run("launchctl", "bootstrap", self.domain, str(self.plist_path))

    def terminate_service_processes(self) -> tuple[list[int], list[int]]:
        """End ``mungchi service run`` processes: SIGTERM, then SIGKILL after a short wait.

        Returns ``(found, still_running)``. The applet quits by itself once its
        child has ended.
        """
        found = self.pgrep(SERVICE_PROCESS_PATTERN)
        if not found:
            return [], []
        self.run("pkill", "-TERM", "-u", str(self.uid), "-f", SERVICE_PROCESS_PATTERN)
        waited = 0.0
        while waited < self.stop_wait_seconds:
            self.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
            if not self.pgrep(SERVICE_PROCESS_PATTERN):
                return found, []
        self.run("pkill", "-KILL", "-u", str(self.uid), "-f", SERVICE_PROCESS_PATTERN)
        self.sleep(POLL_SECONDS)
        return found, self.pgrep(SERVICE_PROCESS_PATTERN)

    def warn_foreground(self) -> list[int]:
        """Warn about a ``mungchi slack`` started by hand (two connections split the events)."""
        pids = self.pgrep(FOREGROUND_PROCESS_PATTERN)
        if pids:
            self.say(
                f"[경고] 터미널에서 직접 띄운 Slack 봇(python -m mungchi slack)이 돌고 있습니다 (PID {_pids(pids)}). "
                "같은 봇이 두 곳에서 Slack에 연결하면 이벤트가 두 프로세스로 나뉘어 어떤 멘션은 답이 없습니다. "
                "그 터미널 탭에서 Ctrl+C로 꺼 주세요."
            )
        return pids

    def service_env(self) -> Mapping[str, str]:
        """The settings the service runs with: the repository's ``.env`` (it never sees shell exports)."""
        env_file = self.repo_dir / ".env"
        if env_file.is_file():
            with contextlib.suppress(Exception):
                return read_env_file(env_file)
        return self.environ

    def morning_brief_lines(self) -> list[str]:
        """``status`` lines: the morning briefing schedule (BRIEF_TIME) and ``last_brief_date``."""
        env = self.service_env()
        schedule = config.load_brief_schedule(env)
        line = f"- 아침 브리핑: {schedule.describe()}"
        if schedule.enabled:
            line += f" → {config.describe_brief_destination(config.load_slack_config(env))}"
        lines = [line, *(f"  [경고] {warning}" for warning in schedule.warnings)]
        last = StateStore(config.get_state_path(env, base_dir=self.repo_dir)).last_brief_date()
        lines.append(f"- 마지막 아침 브리핑 (last_brief_date): {last or '아직 없음'}")
        return lines

    def running_code_lines(self, service_pids: Sequence[int], foreground_pids: Sequence[int]) -> tuple[list[str], list[str]]:
        """``status`` lines about the code the bots run, and a closing warning when it is not the repository's.

        The bots write ``running_version`` / ``running_since`` to the state
        file when they start; the repository's commit comes from git.
        """
        env = self.service_env()
        record = StateStore(config.get_state_path(env, base_dir=self.repo_dir)).running()
        current = version.git_version(self.repo_dir, self.git)
        tz = config.get_timezone(env)
        repo_line = f"- 저장소 코드: {current}" if current else "- 저장소 코드: 알 수 없음 (git으로 확인하지 못했습니다)"

        def started(record: tuple[str, datetime | None]) -> str:
            _version, since = record
            return f"{since.astimezone(tz):%Y-%m-%d %H:%M} 시작" if since else "시작 시각 모름"

        if not service_pids and not foreground_pids:
            last = f". 마지막으로 시작한 코드: {record[0]}, {started(record)}" if record else ""
            return [f"- 실행 중인 코드: 없음 (봇이 돌고 있지 않습니다{last})", repo_line], []
        restart = f"{COMMAND} restart" if service_pids else "터미널에서 띄운 봇을 Ctrl+C로 끄고 python -m mungchi slack 으로 다시 켜세요"
        if record is None:
            line = "- 실행 중인 코드: 기록 없음 (코드 버전을 기록하기 전의 예전 코드로 시작한 봇입니다)"
            newest = f"최신({current})을" if current else "최신 코드를"
            return [line, repo_line], [f"⚠️ 봇이 버전 기록이 없는 예전 코드로 돌고 있어요. {newest} 적용하려면: {restart}"]
        running_version = record[0]
        line = f"- 실행 중인 코드: {running_version} ({started(record)})"
        if current is None or not version.is_git_version(running_version):
            return [line + " (저장소 코드와 비교하지 못했습니다)", repo_line], []
        if running_version == current:
            return [line + " → 저장소 최신 코드와 같습니다", repo_line], []
        return [line, repo_line], [
            f"⚠️ 봇이 예전 코드({running_version})로 돌고 있어요. 최신({current})을 적용하려면: {restart}"
        ]

    def log_secrets(self) -> list[str]:
        """Secret values to hide when showing logs (the bot already scrubs; this is a second net)."""
        values = config.secret_values(self.environ)
        env_file = self.repo_dir / ".env"
        if env_file.is_file():
            with contextlib.suppress(Exception):
                values += config.secret_values(read_env_file(env_file))
        return values

    # -- install

    def install(self) -> int:
        if not self.require_macos():
            return 1
        if self.prefix == self.base_prefix:
            self.say(
                "[오류] 가상환경 안에서 실행해야 서비스가 같은 Python과 패키지를 씁니다. "
                "저장소 폴더에서 source .venv/bin/activate 로 가상환경을 켠 뒤 다시 실행하세요."
            )
            return 1
        repo = self.repo_dir
        env_file = repo / ".env"
        if not env_file.is_file():
            self.say(
                f"[오류] 저장소 폴더({repo})에 .env 파일이 없습니다. "
                "cp .env.example .env 로 만들고 값을 채운 뒤 다시 실행하세요."
            )
            return 1
        env = read_env_file(env_file)
        problems = config.slack_bot_problems(config.load_slack_config(env))
        if problems:
            self.say("[오류] .env의 Slack 설정으로는 봇을 시작할 수 없어서 설치하지 않습니다.")
            for problem in problems:
                self.say(f"- {problem}")
            self.say(f".env를 고친 뒤 다시 실행하세요. {config.SLACK_README_HINT}")
            return 1

        self.say(f"백그라운드 서비스를 설치합니다 (저장소: {repo}, Python: {self.python}).")
        self.warn_foreground()
        self._warn_shell_only_settings(env)
        if not self._build_app():
            return 1
        self._write_launch_agent()
        outcome, result = self.bootout()
        if outcome == "failed":
            self.say(f"[경고] 예전 서비스를 launchd에서 내리지 못했습니다(무시하고 계속): {result.detail()}")
        _found, remaining = self.terminate_service_processes()
        if remaining:
            self.say(f"[경고] 예전 봇 프로세스가 아직 남아 있습니다 (PID {_pids(remaining)}).")
        result = self.bootstrap()
        if not result.ok:
            self.say(f"[오류] launchd에 서비스를 등록하지 못했습니다: {result.detail()}")
            self.say(f"앱과 설정 파일은 만들어 두었습니다. 잠시 뒤 {COMMAND} start 를 실행해 보세요.")
            return 1
        source = config.load_calendar_config(env, platform=self.platform).source
        self._print_install_summary(source)
        return 0

    def _warn_shell_only_settings(self, env: Mapping[str, str]) -> None:
        names = [
            name
            for name in SHELL_ONLY_NAMES
            if (self.environ.get(name) or "").strip() and not (env.get(name) or "").strip()
        ]
        if names:
            self.say(
                "[참고] 다음 값은 지금 터미널에만 있고 .env에는 없습니다. 서비스는 터미널에서 export한 값을 "
                f"읽지 못하니, 봇에 필요한 값이면 .env에 넣으세요: {', '.join(names)}"
            )

    def _build_app(self) -> bool:
        app = self.app_path
        app.parent.mkdir(parents=True, exist_ok=True)
        if app.exists() or app.is_symlink():
            self.say(f"예전 앱을 지우고 새로 만듭니다: {self.show(app)}")
            _remove(app)
        with tempfile.TemporaryDirectory(prefix="mungchi-applet-") as tmp:
            source = Path(tmp) / f"{APP_NAME}.applescript"
            source.write_text(applet_source(self.run_script_path), encoding="utf-8")
            result = self.run("osacompile", "-o", str(app), str(source))
        if not result.ok or not self.info_plist_path.is_file():
            self.say(f"[오류] 앱을 만들지 못했습니다(osacompile): {result.detail()}")
            return False

        script = self.run_script_path
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(run_bot_script(self.repo_dir, self.python), encoding="utf-8")
        script.chmod(0o755)

        try:
            with self.info_plist_path.open("rb") as handle:
                info = plistlib.load(handle)
            with self.info_plist_path.open("wb") as handle:
                plistlib.dump(applet_info(info), handle)
        except (OSError, plistlib.InvalidFileException, ValueError) as exc:
            self.say(f"[오류] 앱 정보(Info.plist)를 고치지 못했습니다: {safe_error(exc)}")
            return False

        # Ad-hoc signature over the finished bundle (after every edit above).
        signed = self.run("codesign", "--force", "--deep", "--sign", "-", str(app))
        if not signed.ok:
            self.say(
                f"[경고] 앱 서명(codesign)에 실패했습니다: {signed.detail()} — 그대로 계속하지만, "
                "캘린더 확인 창이 뜨지 않거나 권한이 유지되지 않을 수 있습니다."
            )
        self.say(f"앱을 만들었습니다: {self.show(app)} ('{APP_DISPLAY_NAME}')")
        return True

    def _write_launch_agent(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.plist_path.parent.mkdir(parents=True, exist_ok=True)
        self.plist_path.write_bytes(plistlib.dumps(launch_agent(self.app_path, self.launchd_log)))
        self.plist_path.chmod(0o644)
        self.say(f"launchd 설정을 만들었습니다: {self.show(self.plist_path)}")

    def _print_install_summary(self, calendar_source: str) -> None:
        self.say()
        self.say(
            "설치를 마쳤습니다. Slack 봇(고뭉치·업뎃·일정)이 백그라운드에서 돌고, 로그인하면 자동으로 시작하며, "
            f"봇이 죽으면 자동으로 다시 시작합니다(최소 {THROTTLE_SECONDS}초 간격)."
        )
        self.say(f"- 로그: {self.show(self.bot_log)} (launchd 로그: {self.show(self.launchd_log)})")
        self.say()
        if calendar_source == config.CALENDAR_SOURCE_MACOS:
            self.say(
                f"★ 곧 '{APP_DISPLAY_NAME}'의 캘린더 접근 확인 창이 뜹니다. '허용'을 눌러 주세요 "
                f"(창에 {APP_NAME}으로 나올 수도 있습니다)."
            )
            self.say("  터미널에 줬던 캘린더 권한은 이 앱으로 넘어가지 않아서 한 번 더 허용해야 합니다.")
        else:
            self.say("[참고] 지금 .env 설정으로는 Mac 캘린더 앱을 읽지 않으므로 캘린더 확인 창은 뜨지 않습니다.")
        self.say()
        self.say("자주 쓰는 명령:")
        self.say(f"  {COMMAND} status      # 상태와 최근 로그")
        self.say(f"  {COMMAND} logs -f     # 로그 계속 보기 (Ctrl+C로 그만 보기)")
        self.say(f"  {COMMAND} restart     # .env를 고친 뒤 다시 시작")
        self.say(f"  {COMMAND} stop        # 멈추기 (다음 로그인 때 다시 켜짐)")
        self.say(f"  {COMMAND} uninstall   # 서비스 지우기 (로그는 남김)")
        self.say()
        self.say(SLEEP_TIP)

    # -- stop / start / restart / uninstall

    def stop(self) -> int:
        if not self.require_macos():
            return 1
        return self._stop(after="stop")

    def _stop(self, after: str) -> int:
        outcome, result = self.bootout()
        found, remaining = self.terminate_service_processes()
        if outcome == "stopped":
            self.say(f"launchd에서 서비스를 내렸습니다 ({LABEL}).")
        elif outcome == "not_loaded":
            self.say("launchd에 등록된 서비스가 없었습니다 (이미 멈춰 있음).")
        else:
            self.say(f"[경고] launchd에서 서비스를 내리지 못했습니다: {result.detail()}")
        if found and not remaining:
            self.say(f"봇 프로세스를 끝냈습니다 (PID {_pids(found)}).")
        elif not found:
            self.say("실행 중인 봇 프로세스는 없었습니다.")
        if remaining:
            self.say(
                f"[오류] 봇 프로세스가 아직 남아 있습니다 (PID {_pids(remaining)}). "
                "'활성 상태 보기' 앱에서 끝내 주세요."
            )
            return 1
        if after == "stop":
            if self.installed:
                self.say(f"서비스를 멈췄습니다. 다음에 로그인하면 다시 켜집니다. 지금 다시 켜려면: {COMMAND} start")
            else:
                self.say("서비스가 설치되어 있지 않습니다.")
        return 0

    def start(self) -> int:
        if not self.require_macos():
            return 1
        return self._start()

    def _start(self) -> int:
        if not self.installed:
            missing = self.show(self.plist_path) if not self.plist_path.is_file() else self.show(self.app_path)
            self.say(f"[오류] 서비스가 설치되어 있지 않습니다({missing} 없음). 먼저 {COMMAND} install 을 실행하세요.")
            return 1
        self.warn_foreground()
        loaded, _state = self.launchd_state()
        if loaded:
            result = self.run("launchctl", "kickstart", self.target)
            if not result.ok:
                self.say(f"[오류] 서비스를 시작하지 못했습니다: {result.detail()}")
                return 1
            self.say("서비스가 이미 등록되어 있어 실행을 요청했습니다 (이미 돌고 있었다면 그대로 둡니다).")
        else:
            result = self.bootstrap()
            if not result.ok:
                self.say(f"[오류] 서비스를 시작하지 못했습니다: {result.detail()}")
                return 1
            self.say("서비스를 시작했습니다.")
        self.say(f"상태 보기: {COMMAND} status   (로그: {self.show(self.bot_log)})")
        return 0

    def restart(self) -> int:
        if not self.require_macos():
            return 1
        if not self.installed:
            return self._start()  # explains that install comes first
        code = self._stop(after="restart")
        if code != 0:
            return code
        return self._start()

    def uninstall(self) -> int:
        if not self.require_macos():
            return 1
        had_anything = self.plist_path.exists() or self.app_path.exists()
        code = self._stop(after="uninstall")
        for path in (self.plist_path, self.app_path):
            if path.exists() or path.is_symlink():
                _remove(path)
                self.say(f"지웠습니다: {self.show(path)}")
        if not had_anything:
            self.say("설치된 서비스 파일은 없었습니다.")
        self.say(f"로그는 남겨 두었습니다: {self.show(self.log_dir)} (필요 없으면 직접 지우세요)")
        if code == 0:
            self.say(f"서비스를 지웠습니다. 다시 설치하려면: {COMMAND} install")
        return code

    # -- status / logs

    def status(self) -> int:
        if not self.require_macos():
            return 1
        loaded, state = self.launchd_state()
        service_pids = self.pgrep(SERVICE_PROCESS_PATTERN)
        foreground_pids = self.pgrep(FOREGROUND_PROCESS_PATTERN)
        has_plist, has_app = self.plist_path.is_file(), self.app_path.is_dir()

        self.say(f"고뭉치 비서실 백그라운드 서비스 ({LABEL})")
        if has_plist and has_app:
            self.say(f"- 설치: 됨 (앱 {self.show(self.app_path)}, launchd 설정 {self.show(self.plist_path)})")
        elif has_plist or has_app:
            missing = self.show(self.app_path) if has_plist else self.show(self.plist_path)
            self.say(f"- 설치: 일부만 됨 ({missing} 없음) → {COMMAND} install 을 다시 실행하세요")
        else:
            self.say(f"- 설치: 안 됨 → {COMMAND} install")
        if loaded:
            self.say("- launchd 등록: 됨" + (f" (state = {state})" if state else ""))
        else:
            self.say("- launchd 등록: 안 됨")
        if service_pids:
            self.say(f"- 봇 프로세스: 실행 중 (PID {_pids(service_pids)})")
        else:
            self.say("- 봇 프로세스: 없음")
        if foreground_pids:
            self.say(
                f"- [경고] 터미널에서 직접 띄운 봇도 돌고 있습니다 (PID {_pids(foreground_pids)}). "
                "Slack 이벤트가 나뉘니 그 터미널 탭에서 Ctrl+C로 끄세요."
            )
        code_lines, code_warnings = self.running_code_lines(service_pids, foreground_pids)
        for line in code_lines:
            self.say(line)
        record = last_calendar_record(self.bot_log)
        if record:
            when, text = record
            at = f", {when} 기록" if when else ""
            self.say(f"- 캘린더 권한 ('{APP_DISPLAY_NAME}' 앱이 시작할 때 확인한 값{at}): {text}")
        else:
            self.say(f"- 캘린더 권한 ('{APP_DISPLAY_NAME}' 앱): 아직 기록 없음 (서비스가 시작하면 로그에 남습니다)")
        for line in self.morning_brief_lines():
            self.say(line)
        if code_warnings:
            self.say()
            for line in code_warnings:
                self.say(line)

        self.say()
        if self.bot_log.is_file():
            self.say(f"최근 로그 ({self.show(self.bot_log)}, 마지막 {STATUS_LOG_LINES}줄):")
            secrets = self.log_secrets()
            lines = tail_lines(self.bot_log, STATUS_LOG_LINES)
            for line in lines:
                self.say("  " + scrub(line, secrets))
            if not lines:
                self.say("  (비어 있음)")
        else:
            self.say(f"아직 로그가 없습니다 ({self.show(self.bot_log)}).")

        if has_plist and has_app and not loaded:
            self.say()
            self.say(f"서비스가 멈춰 있습니다. 켜려면: {COMMAND} start")
        elif loaded and not service_pids:
            self.say()
            self.say(
                "launchd에는 등록되어 있지만 봇 프로세스가 없습니다. 시작 중이거나 오류로 다시 시작을 기다리는 중일 수 있으니 "
                f"로그를 확인하세요: {COMMAND} logs (앱을 띄우는 단계의 오류는 {self.show(self.launchd_log)})"
            )
        return 0

    def logs(self, lines: int = DEFAULT_LOG_LINES, follow: bool = False) -> int:
        if not self.require_macos():
            return 1
        path = self.bot_log
        secrets = self.log_secrets()
        out = self.out or sys.stdout
        if path.is_file():
            for line in tail_lines(path, lines):
                out.write(scrub(line, secrets) + "\n")
            out.flush()
        else:
            self.say(f"아직 로그가 없습니다 ({self.show(path)}). 서비스를 설치했는지 확인하세요: {COMMAND} status")
            if not follow:
                return 1
        if not follow:
            return 0
        self.say(f"(새 로그를 기다립니다: {self.show(path)}. 그만 보려면 Ctrl+C)")
        try:
            self._follow(path, secrets)
        except KeyboardInterrupt:
            self.say()
        return 0

    def _follow(self, path: Path, secrets: list[str]) -> None:
        """Print lines appended to ``path`` until interrupted (like ``tail -F``)."""
        out = self.out or sys.stdout
        try:
            position = path.stat().st_size
        except OSError:
            position = 0
        pending = b""
        while True:
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            if size is not None:
                if size < position:  # truncated or replaced: start over
                    position, pending = 0, b""
                if size > position:
                    with path.open("rb") as handle:
                        handle.seek(position)
                        chunk = handle.read()
                        position = handle.tell()
                    *complete, pending = (pending + chunk).split(b"\n")
                    for raw in complete:
                        out.write(scrub(raw.decode("utf-8", "replace").rstrip("\r"), secrets) + "\n")
                    out.flush()
            self.sleep(POLL_SECONDS)


def _pids(pids: Sequence[int]) -> str:
    return ", ".join(str(pid) for pid in pids)


# ---------------------------------------------------------------- service run (inside the applet)


AdapterFactory = Callable[[Any], macos_calendar.CalendarAdapter]


def _line_buffered_output() -> None:
    """Flush every line so ``service logs`` shows what just happened."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(line_buffering=True)  # type: ignore[union-attr]


def calendar_preflight(
    say: Callable[[str], None],
    *,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    adapter_factory: AdapterFactory | None = None,
    timeout: float = SERVICE_REQUEST_TIMEOUT_SECONDS,
) -> str | None:
    """Log the applet's calendar permission and ask for it once if undecided.

    Returns the permission state, or None when it was not checked. Never
    raises and never blocks longer than ``timeout`` (plus a margin): the
    bots start whatever happens here.
    """
    cfg = config.load_calendar_config(env, platform=platform)
    if cfg.source != config.CALENDAR_SOURCE_MACOS:
        why = "ICS 주소를 읽음" if cfg.source == config.CALENDAR_SOURCE_ICS else "캘린더 설정 없음"
        say(f"{CALENDAR_LOG_KEY} 확인 안 함 (Mac 캘린더 앱을 읽지 않음: {why})")
        return None

    def label(status: str) -> str:
        return macos_calendar.STATUS_LABELS.get(status, status)

    def check() -> str | None:
        try:
            adapter = (adapter_factory or macos_calendar.default_adapter)(config.get_timezone(env))
        except (macos_calendar.EventKitUnavailable, ImportError):
            say(f"{CALENDAR_LOG_KEY} 확인 못 함 (EventKit을 불러오지 못했습니다). {macos_calendar.EVENTKIT_MISSING_HINT}")
            return None
        status = adapter.authorization_status()
        if status != macos_calendar.NOT_DETERMINED:
            say(f"{CALENDAR_LOG_KEY} {label(status)}")
        else:
            say(
                f"{CALENDAR_LOG_KEY} {label(status)} → 지금 요청합니다. '{APP_DISPLAY_NAME}' 확인 창에서 "
                f"'허용'을 눌러 주세요 (최대 {int(timeout)}초 기다립니다)."
            )
            granted = adapter.request_access(timeout=timeout)
            status = macos_calendar.GRANTED if granted else adapter.authorization_status()
            if status == macos_calendar.NOT_DETERMINED:
                say(
                    f"{CALENDAR_LOG_KEY} {label(status)} (확인 창의 응답을 받지 못했습니다. 봇은 그대로 시작합니다. "
                    f"다시 물으려면 {COMMAND} restart)"
                )
            else:
                say(f"{CALENDAR_LOG_KEY} {label(status)} (요청 결과)")
        if status not in (macos_calendar.GRANTED, macos_calendar.NOT_DETERMINED):
            say(f"[경고] {SERVICE_PERMISSION_HINT}")
        return status

    outcome: dict[str, Any] = {}

    def worker() -> None:
        try:
            outcome["status"] = check()
        except BaseException as exc:  # noqa: BLE001 - the bots start anyway
            outcome["error"] = exc

    # EventKit calls block; a worker thread bounds the whole check.
    thread = threading.Thread(target=worker, name="mungchi-calendar-check", daemon=True)
    thread.start()
    thread.join(timeout + PREFLIGHT_MARGIN_SECONDS)
    if thread.is_alive():
        say(f"{CALENDAR_LOG_KEY} 확인 못 함 (시간 초과). 봇은 그대로 시작합니다.")
        return None
    if "error" in outcome:
        say(f"{CALENDAR_LOG_KEY} 확인 못 함 ({safe_error(outcome['error'])}). 봇은 그대로 시작합니다.")
        return None
    return outcome.get("status")


def _run_slack() -> int:
    # Exactly what ``python -m mungchi slack`` runs.
    return main_module.main([main_module.SLACK_COMMAND])


def service_run(
    *,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    adapter_factory: AdapterFactory | None = None,
    slack_runner: Callable[[], int] | None = None,
    load_env: Callable[[], None] | None = None,
    log: TextIO | None = None,
    clock: Callable[[], datetime] | None = None,
    request_timeout: float = SERVICE_REQUEST_TIMEOUT_SECONDS,
    handle_sigterm: bool = True,
) -> int:
    """``service run``: what the applet starts. Logs, checks the calendar permission, runs the bots.

    stdout/stderr go to ``bot.log`` (``run-bot.sh`` redirects them). SIGTERM
    (``service stop``) ends the bots like Ctrl+C does in Terminal.
    """
    _line_buffered_output()
    stream = log or sys.stderr
    now = clock or datetime.now

    def say(message: str) -> None:
        print(f"{now():%Y-%m-%d %H:%M:%S} {SERVICE_LOG_TAG} {message}", file=stream, flush=True)

    previous: Any = None
    if handle_sigterm:
        with contextlib.suppress(ValueError):  # only possible in the main thread
            previous = signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        say(f"Slack 봇 서비스를 시작합니다 (PID {os.getpid()}, 폴더 {os.getcwd()}).")
        (load_env or main_module.load_env)()
        calendar_preflight(say, env=env, platform=platform, adapter_factory=adapter_factory, timeout=request_timeout)
        say("Slack 봇을 시작합니다 (python -m mungchi slack 과 같은 실행 경로).")
        code = (slack_runner or _run_slack)()
        say(f"Slack 봇이 끝났습니다 (종료 코드 {code}).")
        return code
    except KeyboardInterrupt:
        say("종료 신호를 받아 멈춥니다.")
        return 130
    except Exception as exc:  # noqa: BLE001 - one scrubbed line in the log, never a token
        say(f"[오류] 서비스를 실행하지 못했습니다: {safe_error(exc)}")
        return 1
    finally:
        if previous is not None:
            with contextlib.suppress(ValueError):
                signal.signal(signal.SIGTERM, previous)


# ---------------------------------------------------------------- CLI


def _line_count(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = -1
    if number < 0:
        raise argparse.ArgumentTypeError(f"0 이상의 정수여야 합니다: {value}")
    return number


ACTION_HELP = {
    "install": "서비스를 설치하고 바로 시작합니다 (앱 ~/Applications/MungchiBot.app + launchd 등록)",
    "uninstall": "서비스를 멈추고 앱과 launchd 설정을 지웁니다 (로그는 남김)",
    "start": "서비스를 시작합니다",
    "stop": "서비스를 멈춥니다 (다음 로그인 때 다시 켜짐)",
    "restart": "서비스를 다시 시작합니다 (.env를 고친 뒤 사용)",
    "status": "설치·실행 상태, 실행 중인 코드 버전, 서비스 앱의 캘린더 권한, 아침 브리핑 시각, 최근 로그 10줄을 보여 줍니다",
    "logs": "봇 로그를 보여 줍니다 (-f: 계속 보기, -n N: 마지막 N줄)",
    "run": "(내부용) 서비스 앱이 실행하는 명령입니다. 직접 실행하지 마세요",
}


def _service_formatter(prog: str) -> argparse.HelpFormatter:
    return KoreanHelpFormatter(prog, max_help_position=30)


def build_service_parser() -> argparse.ArgumentParser:
    # The actions are listed by hand: argparse wraps long subcommand names oddly.
    width = max(map(len, ACTION_HELP)) + 3
    action_list = "\n".join(f"  {name:<{width}}{text}" for name, text in ACTION_HELP.items())
    parser = KoreanArgumentParser(
        prog="mungchi service",
        description=(
            "macOS에서 Slack 봇(고뭉치·업뎃·일정)을 백그라운드 서비스로 돌립니다.\n"
            f"로그인하면 자동으로 켜지고, 봇이 죽으면 다시 켜집니다(최소 {THROTTLE_SECONDS}초 간격). "
            f"캘린더 권한은 '{APP_DISPLAY_NAME}' 앱이 받습니다.\n"
            "\n"
            "할 일 목록:\n"
            f"{action_list}"
        ),
        epilog=(
            "예시:\n"
            f"  {COMMAND} install     # 설치하고 시작 ('{APP_DISPLAY_NAME}' 캘린더 접근 → 허용)\n"
            f"  {COMMAND} status      # 상태와 최근 로그\n"
            f"  {COMMAND} logs -f     # 로그 계속 보기 (Ctrl+C로 그만 보기)\n"
            f"  {COMMAND} restart     # .env를 고친 뒤 다시 시작\n"
            f"  {COMMAND} stop        # 멈추기\n"
            f"  {COMMAND} uninstall   # 지우기\n"
            "\n"
            "터미널 탭에서 돌리던 봇(python -m mungchi slack)은 Ctrl+C로 끄고 설치하세요.\n"
            "로그: ~/Library/Logs/mungchi/bot.log. 자세한 내용은 README의 '백그라운드로 실행하기'를 보세요."
        ),
        formatter_class=_service_formatter,
        add_help=False,
    )
    actions = parser.add_subparsers(dest="action", title="인자", metavar="할 일", help="위 '할 일 목록' 가운데 하나")
    parser.add_argument_group("옵션").add_argument(
        "-h", "--help", action="help", help="이 도움말을 보여 주고 끝냅니다"
    )
    for name, text in ACTION_HELP.items():
        sub = actions.add_parser(name, description=text, formatter_class=_service_formatter, add_help=False)
        opts = sub.add_argument_group("옵션")
        opts.add_argument("-h", "--help", action="help", help="이 도움말을 보여 주고 끝냅니다")
        if name == "logs":
            opts.add_argument("-f", "--follow", action="store_true", help="새 로그를 계속 보여 줍니다 (Ctrl+C로 끝)")
            opts.add_argument(
                "-n",
                "--lines",
                type=_line_count,
                default=DEFAULT_LOG_LINES,
                metavar="N",
                help=f"마지막 N줄을 보여 줍니다 (기본 {DEFAULT_LOG_LINES})",
            )
    return parser


def service_main(argv: Sequence[str]) -> int:
    parser = build_service_parser()
    argv = list(argv)
    public = ", ".join(name for name in ACTION_HELP if name != "run")
    if argv and not argv[0].startswith("-") and argv[0] not in ACTION_HELP:
        parser.error(f"모르는 할 일입니다: {argv[0]} (쓸 수 있는 것: {public})")
    args = parser.parse_args(argv)
    if args.action is None:
        parser.error(f"할 일을 적으세요: {public}")
    if args.action == "run":
        return service_run()
    service = Service()
    try:
        if args.action == "logs":
            return service.logs(lines=args.lines, follow=args.follow)
        return getattr(service, args.action)()
    except KeyboardInterrupt:
        print("\n중단했습니다.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - a clean Korean message, never a token
        print(f"[오류] service {args.action}을(를) 실행하지 못했습니다: {safe_error(exc)}", file=sys.stderr)
        return 1
