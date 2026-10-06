"""``check_dropbox_updates``: which files co-authors changed under one Dropbox folder.

List-only by design: only file metadata (path, time, last modifier) is read.
File contents, revisions and diffs are never fetched, which keeps each check
to a few tokens per file. The user opens the files themselves.
"""

from __future__ import annotations

import asyncio
import unicodedata
from datetime import datetime, tzinfo
from typing import Any, Callable, Mapping
from urllib.parse import quote

import dropbox
from dropbox.exceptions import ApiError, AuthError
from dropbox.files import FileMetadata
from claude_agent_sdk import ToolAnnotations, tool

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

SOURCE_KEY = "dropbox"
UNKNOWN_MODIFIER = "unknown"
UNKNOWN_LABEL = "확인 불가"
ROOT_GROUP = "(루트)"
# Files listed by name in one result; beyond this only per-subfolder and
# per-person counts are returned.
MAX_LISTED_FILES = 60
DROPBOX_HOME_URL = "https://www.dropbox.com/home"
TIME_FORMAT = "%Y-%m-%d %H:%M"

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


def classify_modifier(entry: Any, my_account_id: str) -> str | None:
    """Who last modified ``entry``.

    Returns an account id, ``"unknown"`` when Dropbox does not say, or
    ``None`` when the change must be skipped: it was mine, or the file is not
    in a shared folder (so nobody else can have edited it).
    """
    sharing = getattr(entry, "sharing_info", None)
    if sharing is None:
        return None
    modified_by = getattr(sharing, "modified_by", None)
    if not modified_by:
        return UNKNOWN_MODIFIER
    if modified_by == my_account_id:
        return None
    return modified_by


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
    """
    my_account_id = dbx.users_get_current_account().account_id

    changed: list[tuple[datetime, str, str, str]] = []  # (modified, subfolder, path, modifier)
    for entry in list_all_entries(dbx, root):
        if not isinstance(entry, FileMetadata):
            continue
        modified = ensure_aware(entry.server_modified)
        if modified <= since:
            continue
        modifier = classify_modifier(entry, my_account_id)
        if modifier is None:
            continue
        subfolder, path = split_group(relative_path(entry, root))
        changed.append((modified, subfolder, path, modifier))

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


def _friendly_error(exc: Exception, secrets: list[str]) -> str:
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
) -> dict[str, Any]:
    cfg = config.load_dropbox_config(env)
    if not cfg.configured:
        return unconfigured(cfg.missing, config.dropbox_hint(cfg.missing))

    now = ensure_aware(now or utcnow())
    store = store or StateStore(config.get_state_path(env))
    lookback_days = config.get_lookback_days(env)
    since, _basis = resolve_since(since_hours, store.last_checked(SOURCE_KEY), now, lookback_days)
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
            "error": _friendly_error(exc, config.secret_values(env)),
        }
    store.mark_checked(SOURCE_KEY, now)
    return payload


@tool(
    "check_dropbox_updates",
    (
        "DROPBOX_ROOT_FOLDER(기본 /20_연구-진행) 아래에서 공저자(나 제외)가 수정한 파일 목록만 확인한다. "
        "하위 폴더 → 사람별로 파일 경로와 수정 시각, 하위 폴더 링크를 짧은 JSON으로 돌려준다. "
        f"파일 내용·diff는 읽지 않는다. 최근 {MAX_LISTED_FILES}개를 넘는 파일은 개수(omitted)만 준다. "
        "읽기 전용. configured=false면 설정이 없는 것이니 재시도하지 말 것."
    ),
    SINCE_HOURS_SCHEMA,
    annotations=ToolAnnotations(readOnlyHint=True, maxResultSizeChars=MAX_RESULT_SIZE_CHARS),
)
async def check_dropbox_updates(args: dict[str, Any]) -> dict[str, Any]:
    since_hours = int_arg(args.get("since_hours"), 0, 0, MAX_SINCE_HOURS)
    try:
        payload = await asyncio.to_thread(run_check, since_hours)
    except Exception as exc:  # noqa: BLE001 - last line of defence
        payload = {"configured": True, "ok": False, "error": safe_error(exc)}
    return tool_result(payload)
