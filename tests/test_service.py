"""``python -m mungchi service``: the macOS background service, tested on any OS.

No macOS command is ever run: every external command goes through a fake
runner that answers like osacompile / codesign / launchctl / pgrep / pkill
would and records what the files looked like at that moment. The generated
files (AppleScript, run-bot.sh, Info.plist, LaunchAgent) are checked as data;
run-bot.sh is also executed with bash against a fake interpreter to prove its
quoting.
"""

from __future__ import annotations

import io
import os
import plistlib
import re
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from mungchi import config, service, slack_bot
from mungchi import main as main_module
from mungchi.main import build_parser, main
from mungchi.service import (
    APP_DISPLAY_NAME,
    CALENDAR_USAGE_TEXT,
    FOREGROUND_PROCESS_PATTERN,
    LABEL,
    NOT_MAC_TEXT,
    SERVICE_PROCESS_PATTERN,
    CommandResult,
    Service,
    applescript_string,
    applet_info,
    applet_source,
    detect_repo_dir,
    launch_agent,
    run_bot_script,
    service_run,
    tail_lines,
)
from mungchi.tools.macos_calendar import DENIED, GRANTED, NOT_DETERMINED, EventKitUnavailable

UID = 501
DOMAIN = f"gui/{UID}"
TARGET = f"gui/{UID}/{LABEL}"
PGREP_SERVICE = ["pgrep", "-u", str(UID), "-f", "mungchi service run"]
PGREP_SLACK = ["pgrep", "-u", str(UID), "-f", "mungchi slack"]
PKILL_TERM = ["pkill", "-TERM", "-u", str(UID), "-f", "mungchi service run"]
# Assembled so no token-shaped literal is committed.
BOT_TOKEN = "-".join(["xoxb", "1234567890", "0987654321", "AbCdEfGhIjKlMnOpQrStUvWx"])
APP_TOKEN = "-".join(["xapp", "1", "A0123", "1234567890", "abcdef0123456789"])
GOOD_ENV = f"SLACK_BOT_TOKEN={BOT_TOKEN}\nSLACK_APP_TOKEN={APP_TOKEN}\nSLACK_ALLOWED_USER_IDS=U0123ABCD\n"

# What osacompile writes into a fresh applet's Info.plist (trimmed).
OSACOMPILE_INFO = {
    "CFBundleExecutable": "applet",
    "CFBundleIdentifier": "com.apple.ScriptEditor.id.MungchiBot",
    "CFBundleName": "MungchiBot",
    "CFBundlePackageType": "APPL",
    "CFBundleSignature": "aplt",
    "LSRequiresCarbon": True,
}


# ---------------------------------------------------------------- fakes


class FakeRunner:
    """Answers like macOS and records each command with what the bundle looked like then."""

    def __init__(self, *, loaded=False, service_pids=(), foreground_pids=(), stubborn=False):
        self.calls: list[list[str]] = []
        self.loaded = loaded
        self.service_pids = list(service_pids)
        self.foreground_pids = list(foreground_pids)
        self.stubborn = stubborn  # survives SIGTERM
        self.codesign_rc = 0
        self.osacompile_rc = 0
        self.bootstrap_results: list[CommandResult] = []
        self.bootout_result: CommandResult | None = None
        self.applescript: str | None = None
        self.seen: dict[str, dict] = {}

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        handler = getattr(self, "_" + args[0], None)
        return handler(args) if handler else CommandResult(0)

    def _osacompile(self, args):
        assert args[1] == "-o"
        app, source = Path(args[2]), Path(args[3])
        self.applescript = source.read_text(encoding="utf-8")
        self.seen["osacompile"] = {"app_existed": app.exists()}
        if self.osacompile_rc:
            return CommandResult(self.osacompile_rc, "", "syntax error: 예상치 못한 줄 끝")
        (app / "Contents" / "MacOS").mkdir(parents=True)
        (app / "Contents" / "MacOS" / "applet").write_bytes(b"\xcf\xfa\xed\xfe")
        (app / "Contents" / "Resources" / "Scripts").mkdir(parents=True)
        (app / "Contents" / "Resources" / "Scripts" / "main.scpt").write_bytes(b"FasdUAS")
        with (app / "Contents" / "Info.plist").open("wb") as handle:
            plistlib.dump(OSACOMPILE_INFO, handle, fmt=plistlib.FMT_BINARY)
        return CommandResult(0)

    def _codesign(self, args):
        app = Path(args[-1])
        script = app / "Contents" / "Resources" / "run-bot.sh"
        with (app / "Contents" / "Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
        self.seen["codesign"] = {
            "script": script.read_text(encoding="utf-8") if script.exists() else None,
            "mode": script.stat().st_mode & 0o777 if script.exists() else None,
            "info": info,
        }
        return CommandResult(self.codesign_rc, "", "" if self.codesign_rc == 0 else "errSecInternalComponent")

    def _launchctl(self, args):
        verb = args[1]
        if verb == "bootout":
            if self.bootout_result is not None:
                return self.bootout_result
            if not self.loaded:
                return CommandResult(3, "", "Boot-out failed: 3: No such process")
            self.loaded = False
            return CommandResult(0)
        if verb == "bootstrap":
            plist = Path(args[3])
            self.seen.setdefault("bootstrap", {"plist": plistlib.loads(plist.read_bytes()) if plist.exists() else None})
            result = self.bootstrap_results.pop(0) if self.bootstrap_results else CommandResult(0)
            if result.ok:
                self.loaded = True
            return result
        if verb == "print":
            if self.loaded:
                return CommandResult(0, f"{LABEL} = {{\n\tactive count = 1\n\tstate = running\n\tpid = 777\n}}\n")
            return CommandResult(113, "", f'Bad request.\nCould not find service "{LABEL}" in domain for user gui: {UID}')
        if verb == "kickstart":
            return CommandResult(0)
        raise AssertionError(f"unexpected launchctl call: {args}")

    def _pgrep(self, args):
        assert args[1:4] == ["-u", str(UID), "-f"]  # only this user's processes
        pids = {SERVICE_PROCESS_PATTERN: self.service_pids, FOREGROUND_PROCESS_PATTERN: self.foreground_pids}[args[4]]
        if not pids:
            return CommandResult(1)
        return CommandResult(0, "".join(f"{pid}\n" for pid in pids))

    def _pkill(self, args):
        assert args[2:] == ["-u", str(UID), "-f", SERVICE_PROCESS_PATTERN]
        if args[1] == "-KILL" or not self.stubborn:
            self.service_pids = []
        return CommandResult(0)


class Sleeps(list):
    def __call__(self, seconds):
        self.append(seconds)


@pytest.fixture
def paths(tmp_path):
    """A home and a repository whose paths have spaces, Korean and a quote."""
    home = tmp_path / "Users" / "홍 길동"
    repo = tmp_path / "연구 저장소 it's"
    home.mkdir(parents=True)
    repo.mkdir()
    (repo / ".env").write_text(GOOD_ENV, encoding="utf-8")
    return home, repo


def make_service(paths, runner, **overrides):
    home, repo = paths
    options = dict(
        runner=runner,
        home=home,
        uid=UID,
        platform="darwin",
        out=io.StringIO(),
        sleep=Sleeps(),
        python=str(repo / ".venv" / "bin" / "python"),
        prefix=str(repo / ".venv"),
        base_prefix="/Library/Frameworks/Python.framework/Versions/3.14",
        repo_dir=repo,
        environ={},
        stop_wait_seconds=1.0,
    )
    options.update(overrides)
    return Service(**options)


def output(svc: Service) -> str:
    return svc.out.getvalue()


def install_fully(paths, runner=None) -> Service:
    """A service that has been installed (files on disk), for the control tests."""
    svc = make_service(paths, runner or FakeRunner())
    assert svc.install() == 0
    return svc


# ---------------------------------------------------------------- pure generators


def parse_applescript_literal(literal: str) -> str:
    """Read an AppleScript string literal back (the inverse of ``applescript_string``)."""
    assert literal[0] == literal[-1] == '"'
    chars, i = [], 1
    while i < len(literal) - 1:
        char = literal[i]
        if char == "\\":
            following = literal[i + 1]
            chars.append({"n": "\n", "r": "\r", "t": "\t"}.get(following, following))
            i += 2
        else:
            assert char != '"', "unescaped quote ends the literal early"
            chars.append(char)
            i += 1
    return "".join(chars)


@pytest.mark.parametrize(
    "path",
    [
        "/Users/gildong/Applications/MungchiBot.app/Contents/Resources/run-bot.sh",
        "/Users/홍 길동/Applications/MungchiBot.app/Contents/Resources/run-bot.sh",
        '/Users/a "quoted" name/Applications/Mungchi Bot.app/run-bot.sh',
        "/Users/back\\slash/it's/run-bot.sh",
        "/Users/tab\there/new\nline/run-bot.sh",
    ],
)
def test_applet_source_embeds_the_path_as_a_safe_applescript_literal(path):
    source = applet_source(path)
    lines = source.splitlines()
    assert lines[0] == "try" and lines[-1] == "end try" and len(lines) == 3
    match = re.fullmatch(r"    do shell script quoted form of (\".*\")", lines[1])
    assert match, lines[1]
    assert parse_applescript_literal(match.group(1)) == path
    assert source.endswith("\n")


def test_applescript_string_escapes_only_what_it_must():
    assert applescript_string("/Users/홍길동/run-bot.sh") == '"/Users/홍길동/run-bot.sh"'
    assert applescript_string('a"b\\c') == '"a\\"b\\\\c"'
    assert applescript_string("it's") == '"it\'s"'  # the shell quoting is done by `quoted form of`


def test_run_bot_script_content_and_quoting():
    repo = "/Users/홍 길동/연구 저장소 it's"
    python = "/Users/홍 길동/연구 저장소 it's/.venv/bin/python"
    text = run_bot_script(repo, python)
    lines = text.splitlines()
    assert lines[0] == "#!/bin/bash"
    assert 'mkdir -p "$LOG_DIR"' in lines
    cd_line = next(line for line in lines if line.startswith("cd "))
    assert shlex.split(cd_line)[:2] == ["cd", repo]
    exec_line = lines[-1]
    assert shlex.split(exec_line) == [
        "exec", python, "-m", "mungchi", "service", "run", ">>", "$LOG_DIR/bot.log", "2>&1"
    ]
    assert lines.index(cd_line) > lines.index('mkdir -p "$LOG_DIR"')  # a failing cd still reaches the log
    assert 'LOG_DIR="$HOME/Library/Logs/mungchi"' in lines


def test_run_bot_script_really_runs_with_bash(tmp_path):
    """Hostile characters in both paths: nothing is expanded, the fake python sees the right cwd and args."""
    repo = tmp_path / "연구 저장소 it's $HOME `touch PWNED1` $(touch PWNED2) \"q\" \\b"
    repo.mkdir()
    bin_dir = tmp_path / "가짜 python's dir"
    bin_dir.mkdir()
    python = bin_dir / "python"
    python.write_text('#!/bin/bash\necho "cwd=$(pwd -P)"\nprintf "arg=%s\\n" "$@"\necho "에러 출력" >&2\n', encoding="utf-8")
    python.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    script = tmp_path / "run-bot.sh"
    script.write_text(run_bot_script(repo, python), encoding="utf-8")
    script.chmod(0o755)

    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    done = subprocess.run([str(script)], env=env, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    log = (home / "Library" / "Logs" / "mungchi" / "bot.log").read_text(encoding="utf-8")
    assert f"cwd={os.path.realpath(repo)}\n" in log
    assert "arg=-m\narg=mungchi\narg=service\narg=run\n" in log
    assert "에러 출력" in log  # stderr goes to the log too
    # Nothing in the path was expanded or executed (the script's cwd starts as tmp_path, then repo).
    for where in (tmp_path, repo, home):
        assert not (where / "PWNED1").exists() and not (where / "PWNED2").exists()

    # A repository that is gone: exit 1 and say so in the log.
    missing = tmp_path / "없는 폴더"
    script.write_text(run_bot_script(missing, python), encoding="utf-8")
    done = subprocess.run([str(script)], env=env, capture_output=True, text=True, timeout=30)
    assert done.returncode == 1
    log = (home / "Library" / "Logs" / "mungchi" / "bot.log").read_text(encoding="utf-8")
    assert f"[서비스] 저장소 폴더로 이동하지 못했습니다: {missing}" in log


def test_applet_info_sets_identity_and_calendar_usage_texts():
    info = applet_info(OSACOMPILE_INFO)
    assert info["CFBundleIdentifier"] == "local.mungchi.bot"
    assert info["CFBundleName"] == "MungchiBot"
    assert info["CFBundleDisplayName"] == "비서실 고뭉치"
    assert info["LSUIElement"] is True and info["NSAppSleepDisabled"] is True
    assert info["NSCalendarsUsageDescription"] == CALENDAR_USAGE_TEXT
    assert info["NSCalendarsFullAccessUsageDescription"] == CALENDAR_USAGE_TEXT
    assert "일정" in CALENDAR_USAGE_TEXT and "캘린더를 읽습니다" in CALENDAR_USAGE_TEXT
    # osacompile's own keys stay (the applet must still launch), and the input is untouched.
    assert info["CFBundleExecutable"] == "applet" and info["CFBundlePackageType"] == "APPL"
    assert OSACOMPILE_INFO["CFBundleIdentifier"].startswith("com.apple.")
    assert plistlib.loads(plistlib.dumps(info)) == info


def test_launch_agent_plist_round_trips():
    app = "/Users/홍 길동/Applications/MungchiBot.app"
    log = "/Users/홍 길동/Library/Logs/mungchi/launchd.log"
    agent = plistlib.loads(plistlib.dumps(launch_agent(app, log)))
    assert agent == {
        "Label": "local.mungchi.bot",
        "ProgramArguments": ["/usr/bin/open", "-W", "-g", app],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


# ---------------------------------------------------------------- install


def test_install_runs_the_exact_command_sequence_and_writes_every_file(paths):
    home, repo = paths
    runner = FakeRunner()
    svc = make_service(paths, runner)
    assert svc.install() == 0

    app = home / "Applications" / "MungchiBot.app"
    plist = home / "Library" / "LaunchAgents" / "local.mungchi.bot.plist"
    calls = runner.calls
    assert [call[:2] for call in calls] == [
        ["pgrep", "-u"],
        ["osacompile", "-o"],
        ["codesign", "--force"],
        ["launchctl", "bootout"],
        ["pgrep", "-u"],
        ["launchctl", "bootstrap"],
    ]
    assert calls[0] == PGREP_SLACK
    assert calls[1][2] == str(app) and calls[1][3].endswith(".applescript")
    assert calls[2] == ["codesign", "--force", "--deep", "--sign", "-", str(app)]
    assert calls[3] == ["launchctl", "bootout", TARGET]
    assert calls[4] == PGREP_SERVICE
    assert calls[5] == ["launchctl", "bootstrap", DOMAIN, str(plist)]

    # osacompile got the applet source pointing at the script inside the bundle.
    script = app / "Contents" / "Resources" / "run-bot.sh"
    assert runner.applescript == applet_source(script)
    assert not Path(calls[1][3]).exists()  # the temporary source is cleaned up
    # The bundle was complete (script + Info.plist edits) when it was signed.
    signed = runner.seen["codesign"]
    assert signed["script"] == run_bot_script(repo, repo / ".venv" / "bin" / "python")
    assert signed["mode"] == 0o755
    assert signed["info"]["CFBundleIdentifier"] == LABEL and signed["info"]["LSUIElement"] is True
    assert signed["info"]["NSCalendarsFullAccessUsageDescription"] == CALENDAR_USAGE_TEXT
    # The LaunchAgent existed when it was bootstrapped, with absolute log paths.
    agent = runner.seen["bootstrap"]["plist"]
    assert agent == launch_agent(app, home / "Library" / "Logs" / "mungchi" / "launchd.log")
    assert Path(agent["StandardOutPath"]).is_absolute()
    assert plist.stat().st_mode & 0o777 == 0o644
    assert (home / "Library" / "Logs" / "mungchi").is_dir()

    text = output(svc)
    assert "설치를 마쳤습니다" in text and "로그인하면 자동으로 시작" in text
    assert "~/Library/Logs/mungchi/bot.log" in text
    assert f"★ 곧 '{APP_DISPLAY_NAME}'의 캘린더 접근 확인 창이 뜹니다. '허용'을 눌러 주세요" in text
    assert "터미널에 줬던 캘린더 권한은 이 앱으로 넘어가지 않아서" in text
    for action in ("status", "logs -f", "restart", "stop", "uninstall"):
        assert f"python -m mungchi service {action}" in text
    assert ".env를 고친 뒤 다시 시작" in text
    assert "디스플레이가 꺼져 있을 때 자동으로 잠자기 방지" in text and "에너지" in text
    assert "[경고]" not in text and "[오류]" not in text
    assert BOT_TOKEN not in text and APP_TOKEN not in text


def test_install_with_ics_calendar_does_not_promise_a_dialog(paths):
    home, repo = paths
    (repo / ".env").write_text(GOOD_ENV + "CALENDAR_ICS_URLS=https://example.com/cal.ics\n", encoding="utf-8")
    svc = make_service(paths, FakeRunner())
    assert svc.install() == 0
    text = output(svc)
    assert "★" not in text and "캘린더 확인 창은 뜨지 않습니다" in text


def test_install_continues_with_a_warning_when_codesign_fails(paths):
    runner = FakeRunner()
    runner.codesign_rc = 1
    svc = make_service(paths, runner)
    assert svc.install() == 0
    assert ["launchctl", "bootstrap", DOMAIN, str(svc.plist_path)] in runner.calls
    text = output(svc)
    assert "[경고] 앱 서명(codesign)에 실패했습니다: errSecInternalComponent" in text
    assert "설치를 마쳤습니다" in text


def test_install_stops_when_osacompile_fails(paths):
    runner = FakeRunner()
    runner.osacompile_rc = 1
    svc = make_service(paths, runner)
    assert svc.install() == 1
    assert [call[0] for call in runner.calls] == ["pgrep", "osacompile"]
    assert "[오류] 앱을 만들지 못했습니다(osacompile): syntax error" in output(svc)
    assert not svc.plist_path.exists()


def test_install_refuses_outside_a_virtualenv(paths):
    runner = FakeRunner()
    svc = make_service(paths, runner, prefix="/usr", base_prefix="/usr")
    assert svc.install() == 1
    assert runner.calls == []
    assert "[오류] 가상환경 안에서 실행해야" in output(svc) and "source .venv/bin/activate" in output(svc)


def test_install_refuses_without_dotenv(paths):
    _home, repo = paths
    (repo / ".env").unlink()
    runner = FakeRunner()
    svc = make_service(paths, runner)
    assert svc.install() == 1
    assert runner.calls == []
    assert f"[오류] 저장소 폴더({repo})에 .env 파일이 없습니다" in output(svc)
    assert "cp .env.example .env" in output(svc)


def test_install_refuses_an_invalid_slack_config_like_the_slack_command(paths):
    _home, repo = paths
    (repo / ".env").write_text(f"SLACK_BOT_TOKEN={BOT_TOKEN}\nSLACK_ALLOWED_USER_IDS=\n", encoding="utf-8")
    runner = FakeRunner()
    svc = make_service(paths, runner)
    assert svc.install() == 1
    assert runner.calls == []
    text = output(svc)
    assert "[오류] .env의 Slack 설정으로는 봇을 시작할 수 없어서 설치하지 않습니다." in text
    expected = config.slack_bot_problems(config.load_slack_config({"SLACK_BOT_TOKEN": BOT_TOKEN}))
    assert expected and all(f"- {problem}" in text for problem in expected)
    assert "SLACK_APP_TOKEN" in text and "SLACK_ALLOWED_USER_IDS" in text
    assert BOT_TOKEN not in text


def test_install_reads_slack_settings_from_dotenv_not_the_shell(paths):
    """Exported values never reach the service, so they do not count; they are pointed out instead."""
    _home, repo = paths
    (repo / ".env").write_text("SLACK_ALLOWED_USER_IDS=U0123ABCD\n", encoding="utf-8")
    shell = {"SLACK_BOT_TOKEN": BOT_TOKEN, "SLACK_APP_TOKEN": APP_TOKEN}
    svc = make_service(paths, FakeRunner(), environ=shell)
    assert svc.install() == 1
    assert "Slack 봇 토큰이 하나도 없습니다" in output(svc)

    (repo / ".env").write_text(GOOD_ENV, encoding="utf-8")
    secret_cert = "/opt/certs/cacert.pem"
    svc = make_service(paths, FakeRunner(), environ={"SSL_CERT_FILE": secret_cert, "ANTHROPIC_AUTH_TOKEN": "gw-secret-123456"})
    assert svc.install() == 0
    text = output(svc)
    assert "[참고] 다음 값은 지금 터미널에만 있고 .env에는 없습니다" in text
    assert "ANTHROPIC_AUTH_TOKEN, SSL_CERT_FILE" in text
    assert "gw-secret-123456" not in text and secret_cert not in text


def test_install_warns_about_a_foreground_bot(paths):
    runner = FakeRunner(foreground_pids=[4321])
    svc = make_service(paths, runner)
    assert svc.install() == 0
    text = output(svc)
    assert "[경고] 터미널에서 직접 띄운 Slack 봇(python -m mungchi slack)이 돌고 있습니다 (PID 4321)" in text
    assert "Ctrl+C" in text and "이벤트가 두 프로세스로 나뉘어" in text
    assert runner.calls[-1][:2] == ["launchctl", "bootstrap"]  # installed anyway


def test_reinstall_replaces_the_app_and_stops_the_old_bot_first(paths):
    home, _repo = paths
    app = home / "Applications" / "MungchiBot.app"
    (app / "Contents" / "Resources").mkdir(parents=True)
    (app / "Contents" / "Resources" / "stale.txt").write_text("old", encoding="utf-8")
    runner = FakeRunner(loaded=True, service_pids=[999])
    svc = make_service(paths, runner)
    assert svc.install() == 0
    assert runner.seen["osacompile"]["app_existed"] is False  # removed before compiling
    assert not (app / "Contents" / "Resources" / "stale.txt").exists()
    assert runner.calls[3:] == [
        ["launchctl", "bootout", TARGET],
        PGREP_SERVICE,
        PKILL_TERM,
        PGREP_SERVICE,
        ["launchctl", "bootstrap", DOMAIN, str(svc.plist_path)],
    ]
    assert "예전 앱을 지우고 새로 만듭니다: ~/Applications/MungchiBot.app" in output(svc)


def test_install_creates_missing_folders(paths):
    home, _repo = paths
    assert not (home / "Applications").exists() and not (home / "Library").exists()
    install_fully(paths)
    assert (home / "Applications" / "MungchiBot.app" / "Contents" / "Info.plist").is_file()
    assert (home / "Library" / "LaunchAgents" / "local.mungchi.bot.plist").is_file()


def test_bootstrap_is_retried_once_after_a_failure(paths):
    runner = FakeRunner()
    runner.bootstrap_results = [CommandResult(5, "", "Bootstrap failed: 5: Input/output error"), CommandResult(0)]
    svc = make_service(paths, runner)
    assert svc.install() == 0
    tail = [call[:2] for call in runner.calls[-3:]]
    assert tail == [["launchctl", "bootstrap"], ["launchctl", "print"], ["launchctl", "bootstrap"]]
    assert svc.sleep == [2.0]

    runner = FakeRunner()
    runner.bootstrap_results = [CommandResult(5, "", "Bootstrap failed: 5: Input/output error")] * 2
    svc = make_service(paths, runner)
    assert svc.install() == 1
    assert "[오류] launchd에 서비스를 등록하지 못했습니다: Bootstrap failed: 5: Input/output error" in output(svc)


# ---------------------------------------------------------------- stop / start / restart / uninstall


def test_stop_boots_out_and_terminates_the_bot(paths):
    install_fully(paths)
    runner = FakeRunner(loaded=True, service_pids=[1234])
    svc = make_service(paths, runner)
    assert svc.stop() == 0
    assert runner.calls == [
        ["launchctl", "bootout", TARGET],
        PGREP_SERVICE,
        PKILL_TERM,
        PGREP_SERVICE,
    ]
    text = output(svc)
    assert "launchd에서 서비스를 내렸습니다 (local.mungchi.bot)." in text
    assert "봇 프로세스를 끝냈습니다 (PID 1234)." in text
    assert "다음에 로그인하면 다시 켜집니다" in text and "python -m mungchi service start" in text


def test_stop_when_not_loaded_is_not_an_error(paths):
    runner = FakeRunner()
    svc = make_service(paths, runner)
    assert svc.stop() == 0
    assert runner.calls == [["launchctl", "bootout", TARGET], PGREP_SERVICE]
    text = output(svc)
    assert "launchd에 등록된 서비스가 없었습니다" in text and "실행 중인 봇 프로세스는 없었습니다" in text
    assert "서비스가 설치되어 있지 않습니다." in text


def test_stop_escalates_to_sigkill(paths):
    runner = FakeRunner(loaded=True, service_pids=[55], stubborn=True)
    svc = make_service(paths, runner, stop_wait_seconds=1.0)
    assert svc.stop() == 0
    assert [call[1] for call in runner.calls if call[0] == "pkill"] == ["-TERM", "-KILL"]
    assert runner.calls.count(PGREP_SERVICE) == 4  # found, 2 waits, after KILL
    assert svc.sleep == [0.5, 0.5, 0.5]
    assert "봇 프로세스를 끝냈습니다 (PID 55)." in output(svc)


def test_stop_reports_a_process_that_survives_sigkill(paths):
    class Immortal(FakeRunner):
        def _pkill(self, args):
            return CommandResult(0)

    runner = Immortal(service_pids=[66])
    svc = make_service(paths, runner, stop_wait_seconds=0.5)
    assert svc.stop() == 1
    assert "[오류] 봇 프로세스가 아직 남아 있습니다 (PID 66)" in output(svc)


def test_stop_survives_an_unexpected_bootout_error(paths):
    runner = FakeRunner(service_pids=[7])
    runner.bootout_result = CommandResult(5, "", "Boot-out failed: 5: Input/output error")
    svc = make_service(paths, runner)
    assert svc.stop() == 0
    text = output(svc)
    assert "[경고] launchd에서 서비스를 내리지 못했습니다: Boot-out failed: 5" in text
    assert "봇 프로세스를 끝냈습니다 (PID 7)." in text  # still terminated


def test_start_when_not_installed_points_to_install(paths):
    runner = FakeRunner()
    svc = make_service(paths, runner)
    assert svc.start() == 1
    assert runner.calls == []
    assert "[오류] 서비스가 설치되어 있지 않습니다" in output(svc)
    assert "python -m mungchi service install" in output(svc)


def test_start_bootstraps_when_not_loaded(paths):
    install_fully(paths)
    runner = FakeRunner()
    svc = make_service(paths, runner)
    assert svc.start() == 0
    assert runner.calls == [
        PGREP_SLACK,
        ["launchctl", "print", TARGET],
        ["launchctl", "bootstrap", DOMAIN, str(svc.plist_path)],
    ]
    assert "서비스를 시작했습니다." in output(svc)


def test_start_kickstarts_when_already_loaded(paths):
    install_fully(paths)
    runner = FakeRunner(loaded=True)
    svc = make_service(paths, runner)
    assert svc.start() == 0
    assert runner.calls[1:] == [["launchctl", "print", TARGET], ["launchctl", "kickstart", TARGET]]
    assert "실행을 요청했습니다" in output(svc)


def test_start_needs_the_app_too(paths):
    svc = install_fully(paths)
    service._remove(svc.app_path)
    runner = FakeRunner()
    again = make_service(paths, runner)
    assert again.start() == 1 and runner.calls == []
    assert "~/Applications/MungchiBot.app 없음" in output(again)


def test_restart_is_stop_then_start(paths):
    install_fully(paths)
    runner = FakeRunner(loaded=True, service_pids=[1234])
    svc = make_service(paths, runner)
    assert svc.restart() == 0
    assert runner.calls == [
        ["launchctl", "bootout", TARGET],
        PGREP_SERVICE,
        PKILL_TERM,
        PGREP_SERVICE,
        PGREP_SLACK,
        ["launchctl", "print", TARGET],
        ["launchctl", "bootstrap", DOMAIN, str(svc.plist_path)],
    ]
    text = output(svc)
    assert "봇 프로세스를 끝냈습니다 (PID 1234)." in text and "서비스를 시작했습니다." in text
    assert "다음에 로그인하면" not in text  # that note is for a plain stop


def test_restart_when_not_installed(paths):
    runner = FakeRunner()
    svc = make_service(paths, runner)
    assert svc.restart() == 1
    assert runner.calls == []
    assert "먼저 python -m mungchi service install" in output(svc)


def test_uninstall_stops_and_removes_files_but_keeps_logs(paths):
    svc = install_fully(paths)
    svc.bot_log.write_text("2026-10-06 08:00:00 [서비스] 시작\n", encoding="utf-8")
    runner = FakeRunner(loaded=True, service_pids=[1234])
    again = make_service(paths, runner)
    assert again.uninstall() == 0
    assert [call[:2] for call in runner.calls] == [
        ["launchctl", "bootout"],
        ["pgrep", "-u"],
        ["pkill", "-TERM"],
        ["pgrep", "-u"],
    ]
    assert not again.plist_path.exists() and not again.app_path.exists()
    assert again.bot_log.is_file()
    text = output(again)
    assert "지웠습니다: ~/Library/LaunchAgents/local.mungchi.bot.plist" in text
    assert "지웠습니다: ~/Applications/MungchiBot.app" in text
    assert "로그는 남겨 두었습니다: ~/Library/Logs/mungchi" in text


def test_uninstall_when_nothing_is_installed(paths):
    runner = FakeRunner()
    svc = make_service(paths, runner)
    assert svc.uninstall() == 0
    assert "설치된 서비스 파일은 없었습니다." in output(svc)


# ---------------------------------------------------------------- status / logs


LOG_LINES = [
    "2026-10-06 08:00:00 [서비스] Slack 봇 서비스를 시작합니다 (PID 1234, 폴더 /Users/홍 길동/research).",
    "2026-10-06 08:00:01 [서비스] 캘린더 접근 권한: 아직 정하지 않음 → 지금 요청합니다.",
    "2026-10-06 08:00:09 [서비스] 캘린더 접근 권한: 허용됨(전체 접근) (요청 결과)",
    "2026-10-06 08:00:09 [서비스] Slack 봇을 시작합니다 (python -m mungchi slack 과 같은 실행 경로).",
    *[f"2026-10-06 09:{i:02d}:00,000 INFO mungchi.slack: 고뭉치: 스레드 C1:{i} 답변 완료" for i in range(12)],
    f"2026-10-06 10:00:00,000 ERROR mungchi.slack: 실수로 남은 토큰 {BOT_TOKEN}",
]


def write_log(svc: Service, lines=LOG_LINES) -> None:
    svc.log_dir.mkdir(parents=True, exist_ok=True)
    svc.bot_log.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_status_when_running(paths):
    install_fully(paths)
    runner = FakeRunner(loaded=True, service_pids=[1234])
    svc = make_service(paths, runner)
    write_log(svc)
    assert svc.status() == 0
    assert runner.calls == [
        ["launchctl", "print", TARGET],
        PGREP_SERVICE,
        PGREP_SLACK,
    ]
    text = output(svc)
    assert "- 설치: 됨 (앱 ~/Applications/MungchiBot.app" in text
    assert "- launchd 등록: 됨 (state = running)" in text
    assert "- 봇 프로세스: 실행 중 (PID 1234)" in text
    # The applet's own permission, from the log, not the Terminal's.
    assert "- 캘린더 권한 ('비서실 고뭉치' 앱이 시작할 때 확인한 값, 2026-10-06 08:00:09 기록): 허용됨(전체 접근) (요청 결과)" in text
    assert "최근 로그 (~/Library/Logs/mungchi/bot.log, 마지막 10줄):" in text
    shown = text.split("마지막 10줄):\n", 1)[1].splitlines()
    assert len(shown) == 10 and shown[-1].startswith("  2026-10-06 10:00:00")
    assert BOT_TOKEN not in text and "***" in text  # scrubbed again on display


def test_status_when_not_installed_and_with_a_foreground_bot(paths):
    runner = FakeRunner(foreground_pids=[4321])
    svc = make_service(paths, runner)
    assert svc.status() == 0
    text = output(svc)
    assert "- 설치: 안 됨 → python -m mungchi service install" in text
    assert "- launchd 등록: 안 됨" in text and "- 봇 프로세스: 없음" in text
    assert "터미널에서 직접 띄운 봇도 돌고 있습니다 (PID 4321)" in text
    assert "아직 기록 없음" in text and "아직 로그가 없습니다" in text


def test_status_hints_for_stopped_and_crash_looping_services(paths):
    install_fully(paths)
    svc = make_service(paths, FakeRunner())
    svc.status()
    assert "서비스가 멈춰 있습니다. 켜려면: python -m mungchi service start" in output(svc)

    svc = make_service(paths, FakeRunner(loaded=True))
    svc.status()
    assert "launchd에는 등록되어 있지만 봇 프로세스가 없습니다" in output(svc)
    assert "~/Library/Logs/mungchi/launchd.log" in output(svc)


def test_logs_prints_the_last_lines_scrubbed(paths):
    runner = FakeRunner()
    svc = make_service(paths, runner)
    write_log(svc)
    assert svc.logs(lines=3) == 0
    lines = output(svc).splitlines()
    assert len(lines) == 3 and lines[0].endswith("C1:10 답변 완료")
    assert BOT_TOKEN not in output(svc)
    assert runner.calls == []  # no external command needed

    svc = make_service(paths, runner)
    assert svc.logs() == 0
    assert len(output(svc).splitlines()) == len(LOG_LINES)  # default 50 > lines in the file


def test_logs_without_a_log_file(paths):
    svc = make_service(paths, FakeRunner())
    assert svc.logs() == 1
    assert "아직 로그가 없습니다 (~/Library/Logs/mungchi/bot.log)" in output(svc)


def test_logs_follow_prints_new_lines_until_ctrl_c(paths):
    svc = make_service(paths, FakeRunner())
    write_log(svc, ["첫 줄"])
    ticks = []

    def sleep(seconds):
        ticks.append(seconds)
        with svc.bot_log.open("ab") as handle:
            if len(ticks) == 1:
                handle.write("새 줄 하나\n반쯤 쓴 ".encode())
            elif len(ticks) == 2:
                handle.write(f"줄 {BOT_TOKEN}\n".encode())
            else:
                raise KeyboardInterrupt

    svc.sleep = sleep
    assert svc.logs(lines=5, follow=True) == 0
    text = output(svc)
    assert text.startswith("첫 줄\n")
    assert "새 줄 하나\n" in text and "반쯤 쓴 줄 ***\n" in text
    assert BOT_TOKEN not in text


def test_tail_lines_reads_backwards_in_blocks(tmp_path):
    path = tmp_path / "bot.log"
    path.write_text("".join(f"{i}번째 줄 한글\n" for i in range(1000)), encoding="utf-8")
    assert tail_lines(path, 3, block_size=7) == ["997번째 줄 한글", "998번째 줄 한글", "999번째 줄 한글"]
    assert tail_lines(path, 0) == []
    assert len(tail_lines(path, 5000)) == 1000
    path.write_text("끝에 줄바꿈 없음", encoding="utf-8")
    assert tail_lines(path, 2) == ["끝에 줄바꿈 없음"]


# ---------------------------------------------------------------- service run


class FakeAdapter:
    def __init__(self, status=GRANTED, grant=True):
        self.status = status
        self.grant = grant
        self.calls: list = []

    def authorization_status(self):
        self.calls.append("status")
        return self.status

    def request_access(self, timeout=60):
        self.calls.append(("request", timeout))
        self.status = GRANTED if self.grant else DENIED
        return self.grant


def run_service(adapter_factory, env=None, platform="darwin", slack_code=0):
    log = io.StringIO()
    events: list[str] = []

    def slack_runner():
        events.append("slack")
        events.append(f"sigterm={signal.getsignal(signal.SIGTERM) is signal.default_int_handler}")
        return slack_code

    code = service_run(
        env={"TIMEZONE": "Asia/Seoul", **(env or {})},
        platform=platform,
        adapter_factory=adapter_factory,
        slack_runner=slack_runner,
        load_env=lambda: events.append("load_env"),
        log=log,
        clock=lambda: datetime(2026, 10, 6, 8, 0, 0),
    )
    return code, log.getvalue(), events


def test_run_asks_for_calendar_access_once_then_starts_the_bots():
    adapter = FakeAdapter(status=NOT_DETERMINED)
    before = signal.getsignal(signal.SIGTERM)
    code, log, events = run_service(lambda tz: adapter)
    assert code == 0
    assert adapter.calls == ["status", ("request", 300)]
    assert events == ["load_env", "slack", "sigterm=True"]  # SIGTERM stops the bots like Ctrl+C
    assert signal.getsignal(signal.SIGTERM) is before  # and is restored afterwards
    lines = log.splitlines()
    assert lines[0].startswith("2026-10-06 08:00:00 [서비스] Slack 봇 서비스를 시작합니다 (PID ")
    assert "[서비스] 캘린더 접근 권한: 아직 정하지 않음 → 지금 요청합니다. '비서실 고뭉치' 확인 창에서 '허용'을 눌러 주세요" in log
    assert "[서비스] 캘린더 접근 권한: 허용됨(전체 접근) (요청 결과)" in log
    assert log.index("요청 결과") < log.index("Slack 봇을 시작합니다")
    assert lines[-1] == "2026-10-06 08:00:00 [서비스] Slack 봇이 끝났습니다 (종료 코드 0)."


def test_run_does_not_ask_when_already_granted():
    adapter = FakeAdapter(status=GRANTED)
    code, log, events = run_service(lambda tz: adapter)
    assert code == 0 and adapter.calls == ["status"] and "slack" in events
    assert "[서비스] 캘린더 접근 권한: 허용됨(전체 접근)\n" in log


def test_run_logs_a_refused_permission_with_the_service_fix_and_continues():
    adapter = FakeAdapter(status=NOT_DETERMINED, grant=False)
    code, log, events = run_service(lambda tz: adapter)
    assert code == 0 and "slack" in events
    assert "캘린더 접근 권한: 거부됨 (요청 결과)" in log
    assert "[경고] Mac 캘린더 앱을 읽을 수 없습니다" in log and "'비서실 고뭉치'(또는 MungchiBot)" in log
    assert "python -m mungchi service restart" in log

    adapter = FakeAdapter(status=DENIED)
    code, log, events = run_service(lambda tz: adapter)
    assert adapter.calls == ["status"] and "slack" in events and "[경고]" in log


@pytest.mark.parametrize("error", [ImportError("No module named 'EventKit'"), EventKitUnavailable("no pyobjc")])
def test_run_continues_when_eventkit_cannot_be_imported(error):
    def factory(tz):
        raise error

    code, log, events = run_service(factory)
    assert code == 0 and events[-2:] == ["slack", "sigterm=True"]
    assert "캘린더 접근 권한: 확인 못 함 (EventKit을 불러오지 못했습니다)" in log


def test_run_continues_when_the_calendar_check_crashes():
    class Broken(FakeAdapter):
        def authorization_status(self):
            raise RuntimeError("EKErrorDomain 1")

    code, log, events = run_service(lambda tz: Broken())
    assert code == 0 and "slack" in events
    assert "캘린더 접근 권한: 확인 못 함 (RuntimeError: EKErrorDomain 1). 봇은 그대로 시작합니다." in log


def test_run_skips_the_calendar_app_when_it_is_not_the_source():
    def factory(tz):
        raise AssertionError("EventKit must not be touched")

    code, log, _events = run_service(factory, env={"CALENDAR_ICS_URLS": "https://example.com/a.ics"})
    assert code == 0 and "캘린더 접근 권한: 확인 안 함 (Mac 캘린더 앱을 읽지 않음: ICS 주소를 읽음)" in log
    code, log, _events = run_service(factory, platform="linux")
    assert code == 0 and "캘린더 설정 없음" in log


def test_run_returns_the_bots_exit_code():
    code, log, _events = run_service(lambda tz: FakeAdapter(), slack_code=1)
    assert code == 1 and "[서비스] Slack 봇이 끝났습니다 (종료 코드 1)." in log


def test_a_real_sigterm_stops_the_service_like_ctrl_c():
    """``service stop`` sends SIGTERM; it must end the bots cleanly, not kill the interpreter mid-write."""

    def slack_runner():
        assert signal.getsignal(signal.SIGTERM) is signal.default_int_handler  # else the signal would kill pytest
        os.kill(os.getpid(), signal.SIGTERM)
        for _ in range(100):  # the handler raises KeyboardInterrupt in the main thread almost at once
            time.sleep(0.05)
        pytest.fail("SIGTERM did not interrupt the bots")

    log = io.StringIO()
    code = service_run(env={}, platform="linux", slack_runner=slack_runner, load_env=lambda: None, log=log)
    assert code == 130
    assert log.getvalue().splitlines()[-1].endswith("[서비스] 종료 신호를 받아 멈춥니다.")

    # Before the bots start (e.g. while .env is read): stop without starting them.
    def interrupted_env():
        raise KeyboardInterrupt

    log = io.StringIO()
    code = service_run(
        env={}, platform="linux", slack_runner=lambda: pytest.fail("the bots must not start"),
        load_env=interrupted_env, log=log,
    )
    assert code == 130 and "Slack 봇을 시작합니다" not in log.getvalue()


def test_run_never_waits_forever_for_the_calendar_check(monkeypatch):
    """``request_access`` gets the 300 s limit; a check that hangs anyway is abandoned."""
    import threading

    release = threading.Event()

    class Hanging(FakeAdapter):
        def authorization_status(self):
            release.wait(5)
            return GRANTED

    monkeypatch.setattr(service, "PREFLIGHT_MARGIN_SECONDS", 0.0)
    log = io.StringIO()
    started = []
    code = service_run(
        env={}, platform="darwin", adapter_factory=lambda tz: Hanging(), slack_runner=lambda: started.append(1) or 0,
        load_env=lambda: None, log=log, request_timeout=0.1,
    )
    release.set()
    assert code == 0 and started == [1]
    assert "캘린더 접근 권한: 확인 못 함 (시간 초과). 봇은 그대로 시작합니다." in log.getvalue()


def test_run_uses_exactly_the_slack_command_path(monkeypatch):
    """With no injected runner, ``service run`` goes through ``main(["slack"])``."""
    seen = []
    monkeypatch.setattr(slack_bot, "run_bot_cli", lambda: seen.append("run_bot_cli") or 0)
    monkeypatch.setattr(main_module, "load_env", lambda: seen.append("load_env"))
    log = io.StringIO()
    assert service_run(env={}, platform="linux", log=log) == 0
    assert seen == ["load_env", "load_env", "run_bot_cli"]  # once by run, once by the slack command itself


def test_run_turns_unexpected_errors_into_one_scrubbed_line(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", BOT_TOKEN)

    def broken_env():
        raise OSError(f"cannot read .env with {BOT_TOKEN}")

    log = io.StringIO()
    assert service_run(env={}, platform="linux", load_env=broken_env, slack_runner=lambda: 0, log=log) == 1
    assert "[오류] 서비스를 실행하지 못했습니다: OSError" in log.getvalue() and BOT_TOKEN not in log.getvalue()


# ---------------------------------------------------------------- CLI


class RecordingService:
    instances: list["RecordingService"] = []

    def __init__(self, **kwargs):
        self.calls: list = []
        RecordingService.instances.append(self)

    def __getattr__(self, name):
        def action(**kwargs):
            self.calls.append((name, kwargs))
            return 0

        return action


@pytest.fixture
def recorded(monkeypatch):
    RecordingService.instances = []
    monkeypatch.setattr(service, "Service", RecordingService)
    return RecordingService.instances


@pytest.mark.parametrize("action", ["install", "uninstall", "start", "stop", "restart", "status"])
def test_cli_dispatches_each_action(recorded, action):
    assert main(["service", action]) == 0
    assert recorded[-1].calls == [(action, {})]


def test_cli_logs_options(recorded):
    assert main(["service", "logs"]) == 0
    assert main(["service", "logs", "-f", "-n", "5"]) == 0
    assert main(["service", "logs", "--lines", "0", "--follow"]) == 0
    assert [svc.calls for svc in recorded] == [
        [("logs", {"lines": 50, "follow": False})],
        [("logs", {"lines": 5, "follow": True})],
        [("logs", {"lines": 0, "follow": True})],
    ]


def test_cli_run_goes_to_service_run_on_any_platform(monkeypatch, recorded):
    calls = []
    monkeypatch.setattr(service, "service_run", lambda: calls.append("run") or 7)
    assert main(["service", "run"]) == 7
    assert calls == ["run"] and recorded == []


def test_cli_service_argument_errors_are_korean(capsys, recorded):
    for argv in (["service"], ["service", "bogus"], ["service", "logs", "-n", "-1"], ["--agent", "update", "service"]):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "할 일을 적으세요: install, uninstall, start, stop, restart, status, logs" in err
    assert "모르는 할 일입니다: bogus" in err
    assert "0 이상의 정수여야 합니다: -1" in err
    assert "service 명령은 맨 앞에 쓰고 다른 옵션과 함께 쓸 수 없습니다" in err
    assert recorded == []


def test_questions_that_merely_mention_service_are_still_questions(monkeypatch):
    asked = []

    async def fake_run_once(prompt, persona="mungchi"):
        asked.append(prompt)
        return 0

    monkeypatch.setattr(main_module, "run_once", fake_run_once)
    assert main(["service 상태 알려줘"]) == 0
    assert main(["서비스 service"]) == 0
    assert asked == ["service 상태 알려줘", "서비스 service"]


def test_help_texts_list_the_service_commands(capsys):
    help_text = build_parser().format_help()
    assert "python -m mungchi service install" in help_text and "python -m mungchi service status" in help_text
    assert "맨 앞에 service 한 단어를 쓰면 질문이 아니라 백그라운드 서비스 명령" in help_text
    assert "install | uninstall | start | stop | restart | status | logs [-f] [-n N]" in help_text
    assert "service run은 서비스가 내부에서 쓰는 명령" in help_text
    assert "slack 한 단어" in help_text  # the slack note is unchanged

    with pytest.raises(SystemExit) as exc:
        main(["service", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("사용법: mungchi service")
    for action in ("install", "uninstall", "start", "stop", "restart", "status", "logs", "run"):
        assert f"\n  {action} " in out
    assert "(내부용) 서비스 앱이 실행하는 명령입니다" in out
    assert "options:" not in out and "positional arguments" not in out

    with pytest.raises(SystemExit):
        main(["service", "logs", "--help"])
    out = capsys.readouterr().out
    assert out.startswith("사용법: mungchi service logs") and "사용법: 사용법" not in out
    assert "-f, --follow" in out and "-n N, --lines N" in out


@pytest.mark.parametrize("action", ["install", "uninstall", "start", "stop", "restart", "status", "logs"])
def test_every_action_but_run_refuses_off_macos(monkeypatch, capsys, action):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(config, "current_platform", lambda: sys.platform)  # the real lookup, on the patched value
    monkeypatch.setattr(service, "run_command", lambda args, timeout=120: pytest.fail(f"ran {args}"))
    assert main(["service", action]) == 1
    assert NOT_MAC_TEXT in capsys.readouterr().out
    assert "macOS에서만" in NOT_MAC_TEXT


def test_existing_cli_parsing_is_unchanged():
    parser = build_parser()
    assert parser.parse_args(["slack"]).question == "slack"
    assert parser.parse_args(["--brief", "--slack"]).slack is True
    assert parser.parse_args(["오늘 일정?"]).question == "오늘 일정?"
    assert parser.parse_args(["--agent", "schedule", "service 질문"]).question == "service 질문"


# ---------------------------------------------------------------- repository detection and imports


def test_detect_repo_dir_prefers_the_package_location_then_cwd(tmp_path):
    repo = tmp_path / "내 research"
    (repo / "src" / "mungchi").mkdir(parents=True)
    (repo / "pyproject.toml").write_text('[project]\nname = "mungchi"\n', encoding="utf-8")
    module = repo / "src" / "mungchi" / "service.py"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert detect_repo_dir(module, cwd=elsewhere) == repo

    other = tmp_path / "other"
    (other / "sub").mkdir(parents=True)
    (other / "pyproject.toml").write_text('[project]\nname = "something-else"\n', encoding="utf-8")
    site = tmp_path / "site-packages" / "mungchi" / "service.py"
    assert detect_repo_dir(site, cwd=repo / "src") == repo  # found from the working directory upwards
    assert detect_repo_dir(site, cwd=other / "sub") == other / "sub"  # not our project: plain cwd


def test_this_checkout_is_detected():
    assert (detect_repo_dir() / "src" / "mungchi" / "service.py").is_file()


def test_importing_the_service_never_imports_eventkit():
    code = (
        "import sys\n"
        "import mungchi.service\n"
        "loaded = [m for m in ('EventKit', 'Foundation', 'objc', 'AppKit') if m in sys.modules]\n"
        "sys.exit(1 if loaded else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
