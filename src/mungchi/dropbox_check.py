"""``python -m mungchi --dropbox-check [--hours N]``: why 업뎃 did (or did not) report a Dropbox change.

Lists the same folder the ``check_dropbox_updates`` tool lists and sorts every
file with the tool's own ``classify_entry``, so what is printed here is
exactly what 업뎃 would include or leave out. Read-only: no Claude API call,
the stored "last checked" time is read but never moved, and no token is
printed (every line is scrubbed).
"""

from __future__ import annotations

import os
import sys
import unicodedata
from datetime import datetime
from typing import Any, Callable, Mapping, TextIO

from . import config
from .state import StateStore, ensure_aware, utcnow
from .tools import dropbox_tool
from .tools.common import scrub
from .tools.dropbox_tool import (
    EXCLUDED_BEFORE_WINDOW,
    EXCLUDED_MINE,
    EXCLUDED_UNKNOWN_MODIFIER,
    INCLUDED,
)

MAX_TABLE_ROWS = 50
RECENT_FILES = 10
# Paths longer than this are not padded (the row just gets longer).
MAX_PATH_WIDTH = 48
NO_INFO = "(정보 없음)"

DECISION_LABELS = {
    INCLUDED: "포함",
    EXCLUDED_MINE: "제외: 내가 수정",
    EXCLUDED_UNKNOWN_MODIFIER: "제외: 수정자 정보 없음(공유 폴더 아님)",
    EXCLUDED_BEFORE_WINDOW: "제외: 기간 이전 (기준 시각 이전)",
}
TABLE_HEADERS = ("경로", "수정 시각", "수정한 사람", "공유 폴더", "판정")

HEADER_TEXT = "Dropbox 변경 확인 진단 (읽기 전용: Claude API를 쓰지 않고, 마지막 확인 시각도 바꾸지 않습니다)"
NOT_FOUND_HINT = (
    "DROPBOX_ROOT_FOLDER에 Dropbox 맨 위부터의 전체 경로를 적었는지 확인하세요(예: /Research/20_연구-진행). "
    "Dropbox 팀 계정(팀 스페이스)을 쓰면 웹에서 보이는 경로와 이 프로그램이 보는 경로가 다를 수 있습니다: "
    "웹에서 내 이름 폴더 안에 있는 폴더는 그 이름을 빼고 적고(예: /홍길동/20_연구-진행 → /20_연구-진행), "
    "팀 스페이스(팀 폴더)에 있는 폴더는 지금은 찾지 못할 수 있습니다."
)
UNDETECTED_NOTE = (
    "참고: 삭제·이동·이름 바꾸기는 감지하지 않습니다. 옮기거나 이름만 바꾼 파일은 수정 시각이 그대로라 "
    "기간 안에 들어오지 않고, 지운 파일은 목록에 나오지 않습니다."
)


def _width(text: str) -> int:
    """Terminal width: Hangul and other wide characters take two columns."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(width - _width(text), 0)


def format_table(rows: list[tuple[str, ...]], headers: tuple[str, ...] = TABLE_HEADERS) -> list[str]:
    """Rows as ``a | b | c`` lines, columns padded to line up (the path column at most ``MAX_PATH_WIDTH``)."""
    widths = [max(_width(cell) for cell in column) for column in zip(headers, *rows)]
    widths[0] = min(widths[0], MAX_PATH_WIDTH)
    return [
        "  " + " | ".join(_pad(cell, width) for cell, width in zip(row, widths)).rstrip()
        for row in (headers, *rows)
    ]


def _display_name(account: Any) -> str:
    name = getattr(getattr(account, "name", None), "display_name", None)
    return unicodedata.normalize("NFC", name) if name else NO_INFO


def _basis_text(basis: str, hours: int, lookback_days: int) -> str:
    if basis == "since_hours":
        return f"--hours {hours} (최근 {hours}시간)"
    if basis == "last_check":
        return "마지막 확인 시각 (고뭉치·업뎃이 Dropbox를 마지막으로 확인한 때)"
    return f"확인 기록이 없어 최근 {lookback_days}일 (LOOKBACK_DAYS)"


def run_dropbox_check(
    env: Mapping[str, str] | None = None,
    *,
    hours: int | None = None,
    now: datetime | None = None,
    client_factory: Callable[[config.DropboxConfig], Any] | None = None,
    store: StateStore | None = None,
    out: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    secrets = config.secret_values(env)

    def say(line: str = "") -> None:
        print(scrub(line, secrets), file=out, flush=True)

    say(HEADER_TEXT)
    cfg = config.load_dropbox_config(env)
    if not cfg.configured:
        say("[오류] " + config.dropbox_hint(cfg.missing))
        return 1

    tz = config.get_timezone(env)
    now = ensure_aware(now or utcnow())
    store = store or StateStore(config.get_state_path(env))
    since, basis = dropbox_tool.resolve_window(hours or 0, store, now, env)
    root = dropbox_tool.normalize_root(cfg.root_folder)
    raw_root = (os.environ if env is None else env).get("DROPBOX_ROOT_FOLDER") or ""
    source = "DROPBOX_ROOT_FOLDER" if raw_root.strip() else "기본값"
    say(f"확인할 폴더: {root or '/'} ({source})")

    try:
        dbx = (client_factory or dropbox_tool.make_client)(cfg)
        account = dbx.users_get_current_account()
    except Exception as exc:  # noqa: BLE001 - a clean Korean message, never a token
        say("[오류] " + dropbox_tool.friendly_error(exc, secrets))
        return 1
    my_account_id = account.account_id
    my_name = _display_name(account)

    try:
        entries = dropbox_tool.list_all_entries(dbx, root)
    except Exception as exc:  # noqa: BLE001
        if dropbox_tool.is_path_not_found(exc):
            say("폴더: 찾을 수 없음")
            say("→ " + NOT_FOUND_HINT)
            say(f"계정: {my_name}")
            return 1
        say("[오류] " + dropbox_tool.friendly_error(exc, secrets))
        return 1

    classified = dropbox_tool.classify_files(entries, my_account_id, since)
    stats = dropbox_tool.tally(decision for _entry, decision in classified)
    included = stats["changed_in_window"] - stats["excluded_mine"] - stats["excluded_unknown_modifier"]
    say("폴더: 있음")
    say(f"계정: {my_name}")
    say(
        f"기간: {since.astimezone(tz):%Y-%m-%d %H:%M} ({config.get_timezone_name(env)}) 이후"
        f" — 기준: {_basis_text(basis, hours or 0, config.get_lookback_days(env))}"
    )
    say(f"훑어본 파일: {stats['scanned']}개 (하위 폴더 포함)")
    say(f"기간 안에 바뀐 파일: {stats['changed_in_window']}개")
    say(f"  - {DECISION_LABELS[INCLUDED]}: {included}개 (업뎃이 알려 주는 파일)")
    say(f"  - {DECISION_LABELS[EXCLUDED_MINE]}: {stats['excluded_mine']}개")
    say(f"  - {DECISION_LABELS[EXCLUDED_UNKNOWN_MODIFIER]}: {stats['excluded_unknown_modifier']}개")

    names: dict[str, str] = {my_account_id: my_name}

    def row(entry: Any, decision: str) -> tuple[str, ...]:
        modifier = dropbox_tool.modifier_of(entry)
        if modifier == dropbox_tool.UNKNOWN_MODIFIER:
            who = NO_INFO
        else:
            who = unicodedata.normalize("NFC", dropbox_tool.resolve_name(dbx, modifier, names))
        shared = "예" if getattr(entry, "sharing_info", None) is not None else "아니오"
        when = f"{ensure_aware(entry.server_modified).astimezone(tz):%m-%d %H:%M}"
        return (dropbox_tool.relative_path(entry, root), when, who, shared, DECISION_LABELS[decision])

    newest_first = sorted(
        classified,
        key=lambda item: (-ensure_aware(item[0].server_modified).timestamp(), dropbox_tool.relative_path(item[0], root)),
    )
    changed = [item for item in newest_first if item[1] != EXCLUDED_BEFORE_WINDOW]

    say()
    say(f"기간 안에 바뀐 파일 (최근 수정 순, 최대 {MAX_TABLE_ROWS}개):")
    if changed:
        for line in format_table([row(*item) for item in changed[:MAX_TABLE_ROWS]]):
            say(line)
        if len(changed) > MAX_TABLE_ROWS:
            say(f"  … 외 {len(changed) - MAX_TABLE_ROWS}개")
    else:
        say("  (없음)")

    say()
    say(f"기간과 상관없이 가장 최근에 바뀐 파일 {RECENT_FILES}개:")
    if newest_first:
        for line in format_table([row(*item) for item in newest_first[:RECENT_FILES]]):
            say(line)
    else:
        say("  (파일 없음)")

    say()
    say(UNDETECTED_NOTE)
    return 0
