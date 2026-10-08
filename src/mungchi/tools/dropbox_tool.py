"""``check_dropbox_updates``: which files co-authors changed under one Dropbox folder.

List-only by design: only file metadata (path, time, last modifier) is read.
File contents, revisions and diffs are never fetched, which keeps each check
to a few tokens per file. The user opens the files themselves.

Which window a check looks at is fixed per run, never by the model:

* a briefing run (``build_options(briefing=True)``, only 업뎃's report run
  in the relay briefing, ``briefing.run_report``: ``--brief``, the morning
  briefing, a briefing asked for in Slack) looks at the time since the stored briefing checkpoint
  (``LOOKBACK_DAYS`` without one) and moves the checkpoint after a successful
  check;
* every other run (Slack questions, ``--agent update``, one-shot questions)
  looks at the last 24 hours and never touches the state file;
* an explicit ``since_hours`` always wins and never moves the checkpoint.
"""

from __future__ import annotations

import asyncio
import fnmatch
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, tzinfo
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote

import dropbox
from dropbox.exceptions import ApiError, AuthError
from dropbox.files import FileMetadata
from claude_agent_sdk import SdkMcpTool, ToolAnnotations

from .. import config
from ..state import StateStore, ensure_aware, resolve_since, utcnow
from .common import (
    MAX_RESULT_SIZE_CHARS,
    MAX_SINCE_HOURS,
    SINCE_HOURS_SCHEMA,
    int_arg,
    safe_error,
    to_local_iso,
    tool_result,
    unconfigured,
)

# The briefing checkpoint is stored as ``last_checked.dropbox`` in the state file.
SOURCE_KEY = "dropbox"
TOOL_NAME = "check_dropbox_updates"
# Window of an ad-hoc check (no period given, not a briefing run).
AD_HOC_WINDOW_HOURS = 24
UNKNOWN_MODIFIER = "unknown"
UNKNOWN_LABEL = "확인 불가"
ROOT_GROUP = "(루트)"
# Files listed by name in one result; beyond this only per-subfolder and
# per-person counts are returned.
MAX_LISTED_FILES = 60
DROPBOX_HOME_URL = "https://www.dropbox.com/home"
TIME_FORMAT = "%Y-%m-%d %H:%M"

# What happens to each file in the folder: exactly one of these. The tool and
# ``--dropbox-check`` both use ``classify_entry``, so the diagnosis always
# matches what 업뎃 reports.
INCLUDED = "included"  # changed in the window by someone else (or by an unknown person in a shared folder)
EXCLUDED_TEMP = "excluded_temp"  # editor/office/OS scratch file (TEMP_FILE_PATTERNS), whenever it changed
EXCLUDED_MINE = "excluded_mine"
EXCLUDED_UNKNOWN_MODIFIER = "excluded_unknown_modifier"  # no sharing_info: not in a shared folder
EXCLUDED_BEFORE_WINDOW = "excluded_before_window"
DECISIONS = (INCLUDED, EXCLUDED_TEMP, EXCLUDED_MINE, EXCLUDED_UNKNOWN_MODIFIER, EXCLUDED_BEFORE_WINDOW)

# Temporary and lock files that are never co-author work, as case-insensitive
# globs on the file name: Office owner files (~$draft.docx) and other "~"
# files, LibreOffice locks, macOS/Windows folder files, Stata (.stswp) and
# editor swap files. They are taken out before any other decision.
TEMP_FILE_PATTERNS = (
    "~*",
    ".~lock.*",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    "Icon\r",
    "*.stswp",
    "*.tmp",
    "*.temp",
    "*.swp",
    "*.swo",
)

# ``since_basis`` in the tool result: why the window starts where it does.
BASIS_BRIEFING_CHECKPOINT = "briefing_checkpoint"  # briefing run: since the last briefing
BASIS_LOOKBACK_DEFAULT = "lookback_default"  # briefing run without a checkpoint: LOOKBACK_DAYS
BASIS_DEFAULT_24H = "default_24h"  # any other run without a period: the last 24 hours
BASIS_SINCE_HOURS = "since_hours"  # a period was asked for

# Display names are stable, so cache them for the lifetime of the process.
_NAME_CACHE: dict[str, str] = {}


def normalize_root(root: str) -> str:
    """Dropbox API paths: "" for the whole Dropbox, otherwise "/a/b" without trailing slash."""
    root = (root or "").strip()
    if root in ("", "/"):
        return ""
    if not root.startswith("/"):
        root = "/" + root
    return root.rstrip("/")


def _nfc(text: str) -> str:
    # Korean names may arrive decomposed (NFD, e.g. from macOS); compare composed.
    return unicodedata.normalize("NFC", text)


def relative_path(entry: Any, root: str) -> str:
    display = _nfc(entry.path_display or entry.path_lower or entry.name)
    if root:
        prefix = _nfc(root) + "/"
        if display.lower().startswith(prefix.lower()):
            return display[len(prefix) :]
    return display.lstrip("/")


def split_group(rel_path: str) -> tuple[str, str]:
    """``(immediate subfolder of the root, path inside it)``; ``(루트)`` for files directly in the root."""
    head, sep, rest = rel_path.partition("/")
    return (head, rest) if sep else (ROOT_GROUP, rel_path)


def folder_link(root: str, subfolder: str) -> str:
    """Dropbox web link to ``root/subfolder`` (or the root itself for ``(루트)``)."""
    path = root if subfolder == ROOT_GROUP else f"{root}/{subfolder}"
    return DROPBOX_HOME_URL + quote(_nfc(path), safe="/")


def format_time(dt: datetime, tz: tzinfo) -> str:
    return ensure_aware(dt).astimezone(tz).strftime(TIME_FORMAT)


def is_temp_file(name: str | None) -> bool:
    """True for a temporary or lock file (``TEMP_FILE_PATTERNS``, case-insensitive)."""
    folded = (name or "").casefold()
    return bool(folded) and any(fnmatch.fnmatchcase(folded, pattern.casefold()) for pattern in TEMP_FILE_PATTERNS)


def _modifier_decision(entry: Any, my_account_id: str) -> str:
    """The decision for a file changed inside the window (see ``classify_entry``)."""
    sharing = getattr(entry, "sharing_info", None)
    if sharing is None:
        # Not in a shared folder: Dropbox does not say who modified it.
        return EXCLUDED_UNKNOWN_MODIFIER
    modified_by = getattr(sharing, "modified_by", None)
    if modified_by and modified_by == my_account_id:
        return EXCLUDED_MINE
    # Someone else, or a shared file without ``modified_by`` (reported as "확인 불가").
    return INCLUDED


def classify_entry(entry: Any, my_account_id: str, since: datetime) -> str:
    """Exactly one decision for a file: ``included`` or why it is left out.

    * ``excluded_temp``: a temporary or lock file (checked first, whenever it changed);
    * ``excluded_before_window``: last modified at or before ``since``;
    * ``excluded_unknown_modifier``: no ``sharing_info`` (not in a shared folder);
    * ``excluded_mine``: last modified by me;
    * ``included``: modified by someone else, or in a shared folder whose
      ``modified_by`` is empty (reported with the "확인 불가" modifier).
    """
    if is_temp_file(getattr(entry, "name", None)):
        return EXCLUDED_TEMP
    if ensure_aware(entry.server_modified) <= since:
        return EXCLUDED_BEFORE_WINDOW
    return _modifier_decision(entry, my_account_id)


def modifier_of(entry: Any) -> str:
    """Account id of the last modifier, or ``"unknown"`` when Dropbox does not say."""
    return getattr(getattr(entry, "sharing_info", None), "modified_by", None) or UNKNOWN_MODIFIER


def classify_modifier(entry: Any, my_account_id: str) -> str | None:
    """Who last modified ``entry``, ignoring the time window.

    Returns an account id, ``"unknown"`` when Dropbox does not say, or
    ``None`` when the change must be skipped: it was mine, or the file is not
    in a shared folder (so nobody else can have edited it).
    """
    if _modifier_decision(entry, my_account_id) != INCLUDED:
        return None
    return modifier_of(entry)


def classify_files(entries: Iterable[Any], my_account_id: str, since: datetime) -> list[tuple[Any, str]]:
    """``(file, decision)`` for every file in ``entries``; folders and deleted entries are skipped."""
    return [
        (entry, classify_entry(entry, my_account_id, since)) for entry in entries if isinstance(entry, FileMetadata)
    ]


def tally(decisions: Iterable[str]) -> dict[str, int]:
    """The compact ``stats`` object for a list of decisions.

    Temporary files are taken out first, so ``scanned`` = ``changed_in_window``
    + before the window + ``excluded_temp``, and ``changed_in_window`` =
    included + ``excluded_mine`` + ``excluded_unknown_modifier``.
    """
    counts = Counter(decisions)
    scanned = sum(counts.values())
    return {
        "scanned": scanned,
        "changed_in_window": scanned - counts[EXCLUDED_BEFORE_WINDOW] - counts[EXCLUDED_TEMP],
        "excluded_mine": counts[EXCLUDED_MINE],
        "excluded_unknown_modifier": counts[EXCLUDED_UNKNOWN_MODIFIER],
        "excluded_temp": counts[EXCLUDED_TEMP],
    }


def resolve_name(dbx: Any, account_id: str, cache: dict[str, str] | None = None) -> str:
    if account_id == UNKNOWN_MODIFIER:
        return UNKNOWN_LABEL
    cache = _NAME_CACHE if cache is None else cache
    if account_id not in cache:
        try:
            cache[account_id] = dbx.users_get_account(account_id).name.display_name
        except Exception:  # noqa: BLE001 - fall back to the id, never fail the check
            cache[account_id] = account_id
    return cache[account_id]


def list_all_entries(dbx: Any, root: str) -> list[Any]:
    result = dbx.files_list_folder(root, recursive=True)
    entries = list(result.entries)
    while result.has_more:
        result = dbx.files_list_folder_continue(result.cursor)
        entries.extend(result.entries)
    return entries


def collect_updates(
    dbx: Any,
    root: str,
    since: datetime,
    tz: tzinfo,
    name_cache: dict[str, str] | None = None,
    max_listed: int = MAX_LISTED_FILES,
) -> dict[str, Any]:
    """Co-author changes since ``since``, grouped by subfolder then person.

    Groups, people and files are ordered newest first. Only the newest
    ``max_listed`` files are listed; older ones are only counted (``omitted``).
    ``stats`` counts every file by its ``classify_entry`` decision.
    """
    my_account_id = dbx.users_get_current_account().account_id
    classified = classify_files(list_all_entries(dbx, root), my_account_id, since)

    changed: list[tuple[datetime, str, str, str]] = []  # (modified, subfolder, path, modifier)
    for entry, decision in classified:
        if decision != INCLUDED:
            continue
        subfolder, path = split_group(relative_path(entry, root))
        changed.append((ensure_aware(entry.server_modified), subfolder, path, modifier_of(entry)))

    changed.sort(key=lambda item: (-item[0].timestamp(), item[1], item[2]))

    # Insertion order follows ``changed``, so groups and people come out
    # ordered by their most recent modification.
    groups: dict[str, dict[str, Any]] = {}
    people: dict[tuple[str, str], dict[str, Any]] = {}
    for index, (modified, subfolder, path, modifier) in enumerate(changed):
        group = groups.get(subfolder)
        if group is None:
            group = {"subfolder": subfolder, "link": folder_link(root, subfolder), "by": []}
            groups[subfolder] = group
        name = resolve_name(dbx, modifier, name_cache)
        person = people.get((subfolder, name))
        if person is None:
            person = {"name": name, "files": []}
            people[(subfolder, name)] = person
            group["by"].append(person)
        if index < max_listed:
            person["files"].append({"path": path, "modified": format_time(modified, tz)})
        else:
            person["omitted"] = person.get("omitted", 0) + 1
            group["omitted"] = group.get("omitted", 0) + 1

    return {
        "configured": True,
        "folder": root or "/",
        "since": to_local_iso(since, tz),
        "total_files": len(changed),
        "stats": tally(decision for _entry, decision in classified),
        "groups": list(groups.values()),
        "omitted": max(len(changed) - max_listed, 0),
    }


def make_client(cfg: config.DropboxConfig) -> dropbox.Dropbox:
    if cfg.refresh_token and cfg.app_key and cfg.app_secret:
        return dropbox.Dropbox(
            oauth2_refresh_token=cfg.refresh_token,
            app_key=cfg.app_key,
            app_secret=cfg.app_secret,
            timeout=60,
        )
    return dropbox.Dropbox(oauth2_access_token=cfg.access_token, timeout=60)


def is_path_not_found(exc: BaseException) -> bool:
    """True when listing failed because the folder does not exist (``path/not_found``)."""
    error = getattr(exc, "error", None) if isinstance(exc, ApiError) else None
    try:
        return bool(error.is_path() and error.get_path().is_not_found())
    except AttributeError:
        return False


def resolve_window(
    since_hours: int,
    store: StateStore,
    now: datetime,
    env: Mapping[str, str] | None = None,
    *,
    briefing: bool = False,
) -> tuple[datetime, str]:
    """``(since, since_basis)``: the start of the check window and why it starts there.

    * ``since_hours`` > 0: ``"since_hours"``, for any run;
    * a briefing run: ``"briefing_checkpoint"`` (the stored checkpoint), or
      ``"lookback_default"`` (no checkpoint yet: ``LOOKBACK_DAYS`` days);
    * any other run: ``"default_24h"`` (the last 24 hours).

    Reading the stored checkpoint never changes it.
    """
    now = ensure_aware(now)
    if since_hours and since_hours > 0:
        return now - timedelta(hours=since_hours), BASIS_SINCE_HOURS
    if not briefing:
        return now - timedelta(hours=AD_HOC_WINDOW_HOURS), BASIS_DEFAULT_24H
    since, basis = resolve_since(0, store.last_checked(SOURCE_KEY), now, config.get_lookback_days(env))
    return since, BASIS_BRIEFING_CHECKPOINT if basis == "last_checked" else BASIS_LOOKBACK_DEFAULT


def friendly_error(exc: Exception, secrets: list[str]) -> str:
    detail = safe_error(exc, secrets)
    if isinstance(exc, AuthError):
        return f"Dropbox 인증 실패(토큰 만료·권한(scope) 부족 가능): {detail}"
    if isinstance(exc, ApiError):
        return (
            "Dropbox API 오류(폴더 경로 확인: DROPBOX_ROOT_FOLDER, 기본 "
            f"{config.DEFAULT_DROPBOX_ROOT_FOLDER}. 다른 폴더 안에 있으면 전체 경로를 적어야 함): {detail}"
        )
    return f"Dropbox 확인 실패: {detail}"


def run_check(
    since_hours: int = 0,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
    client_factory: Callable[[config.DropboxConfig], Any] | None = None,
    store: StateStore | None = None,
    *,
    briefing: bool = False,
) -> dict[str, Any]:
    """One check. Only a briefing run without ``since_hours`` moves the checkpoint, and only on success."""
    cfg = config.load_dropbox_config(env)
    if not cfg.configured:
        return unconfigured(cfg.missing, config.dropbox_hint(cfg.missing))

    now = ensure_aware(now or utcnow())
    store = store or StateStore(config.get_state_path(env))
    since, since_basis = resolve_window(since_hours, store, now, env, briefing=briefing)
    tz = config.get_timezone(env)
    root = normalize_root(cfg.root_folder)
    try:
        dbx = (client_factory or make_client)(cfg)
        payload = collect_updates(dbx, root, since, tz)
    except Exception as exc:  # noqa: BLE001 - reported to the model, never raised
        return {
            "configured": True,
            "ok": False,
            "folder": root or "/",
            "since": to_local_iso(since, tz),
            "error": friendly_error(exc, config.secret_values(env)),
        }
    payload["since_basis"] = since_basis
    if briefing and since_basis != BASIS_SINCE_HOURS:
        store.mark_checked(SOURCE_KEY, now)
    return payload


TOOL_DESCRIPTION = (
    "DROPBOX_ROOT_FOLDER(기본 /20_연구-진행)와 그 하위 폴더 전체에서 공저자(나 제외)가 수정한 파일 목록만 확인한다. "
    "하위 폴더 → 사람별로 파일 경로와 수정 시각, 하위 폴더 링크를 짧은 JSON으로 돌려준다. "
    f"파일 내용·diff는 읽지 않는다. 최근 {MAX_LISTED_FILES}개를 넘는 파일은 개수(omitted)만 준다. "
    "임시·잠금 파일(~$…, .~lock.…, .DS_Store, *.tmp, *.swp 등)은 처음부터 뺀다. "
    "since_basis는 기간의 기준이다: default_24h(기간 없이 물어 최근 24시간), since_hours(지정한 시간), "
    "briefing_checkpoint(브리핑 실행: 지난 브리핑 이후), lookback_default(브리핑 실행인데 기록이 없어 "
    "LOOKBACK_DAYS일). 어느 기준인지는 실행 방식이 정하고, since_hours를 주면 그것이 우선한다. "
    "stats는 훑어본 파일 수(scanned), 기간 안에 바뀐 파일 수(changed_in_window, 임시 파일 제외), 그중 내가 수정해서 "
    "뺀 수(excluded_mine), 수정자 정보가 없어서(공유 폴더가 아닌 곳) 뺀 수(excluded_unknown_modifier), 기간과 "
    "상관없이 임시·잠금 파일이라 뺀 수(excluded_temp)다. 브리핑 실행만 브리핑 기준 시각을 지금으로 바꾸고, "
    "그 밖의 확인은 아무것도 바꾸지 않는다. 이동·이름 바꾸기·삭제는 감지하지 않는다. "
    "읽기 전용. configured=false면 설정이 없는 것이니 재시도하지 말 것."
)


def make_check_dropbox_updates(*, briefing: bool = False) -> SdkMcpTool[Any]:
    """A ``check_dropbox_updates`` tool whose run mode is fixed when it is built.

    ``build_options(briefing=True)`` (only 업뎃's report in ``briefing.run_report``) builds a
    briefing tool: it reads and moves the briefing checkpoint. Every other
    run gets an ad-hoc tool (last 24 hours, state untouched). Each run's
    options build their own tool object, so concurrent turns in one process
    (e.g. two Slack bots) never share a mode, and the model cannot switch it:
    it is not a tool argument.
    """

    async def check_dropbox_updates(args: dict[str, Any]) -> dict[str, Any]:
        since_hours = int_arg(args.get("since_hours"), 0, 0, MAX_SINCE_HOURS)
        try:
            payload = await asyncio.to_thread(run_check, since_hours, briefing=briefing)
        except Exception as exc:  # noqa: BLE001 - last line of defence
            payload = {"configured": True, "ok": False, "error": safe_error(exc)}
        return tool_result(payload)

    return SdkMcpTool(
        name=TOOL_NAME,
        description=TOOL_DESCRIPTION,
        input_schema=SINCE_HOURS_SCHEMA,
        handler=check_dropbox_updates,
        annotations=ToolAnnotations(readOnlyHint=True, maxResultSizeChars=MAX_RESULT_SIZE_CHARS),
    )


# The ad-hoc tool: what every run except a briefing gets.
check_dropbox_updates = make_check_dropbox_updates()
