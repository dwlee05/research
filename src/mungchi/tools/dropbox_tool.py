"""``check_dropbox_updates``: co-author changes under one Dropbox folder."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, tzinfo
from typing import Any, Callable, Mapping

import dropbox
from dropbox.exceptions import ApiError, AuthError
from dropbox.files import FileMetadata
from claude_agent_sdk import ToolAnnotations, tool

from .. import config
from ..state import StateStore, describe_basis, ensure_aware, resolve_since, utcnow
from .common import (
    MAX_RESULT_SIZE_CHARS,
    MAX_SINCE_HOURS,
    MAX_TEXT_FILE_BYTES,
    SINCE_HOURS_SCHEMA,
    OutputBudget,
    decode_text,
    int_arg,
    is_text_path,
    new_file_preview,
    safe_error,
    shrink_to_limit,
    to_local_iso,
    tool_result,
    unconfigured,
    unified_diff,
)

SOURCE_KEY = "dropbox"
UNKNOWN_MODIFIER = "unknown"
UNKNOWN_LABEL = "수정자 미상"
ROOT_GROUP = "(루트)"
MAX_LISTED_FILES = 200
MAX_DIFFED_FILES = 40

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


def relative_path(entry: Any, root: str) -> str:
    display = entry.path_display or entry.path_lower or entry.name
    lower = (entry.path_lower or display).lower()
    if root and lower.startswith(root.lower() + "/"):
        return display[len(root) + 1 :]
    return display.lstrip("/")


def top_level_group(rel_path: str) -> str:
    """Immediate subfolder of the root, or ``(루트)`` for files directly in it."""
    parts = rel_path.split("/")
    return parts[0] if len(parts) > 1 else ROOT_GROUP


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


def _download_text(dbx: Any, rev: str) -> str:
    _metadata, response = dbx.files_download(f"rev:{rev}")
    try:
        return decode_text(response.content)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def file_diff(dbx: Any, entry: FileMetadata, label: str, budget: OutputBudget) -> dict[str, Any]:
    """Diff the latest revision against the previous one (or preview a new file)."""
    if (entry.size or 0) > MAX_TEXT_FILE_BYTES:
        return {"diff": None, "diff_note": "200KB를 넘는 파일이라 diff 생략"}
    revisions = dbx.files_list_revisions(entry.path_lower, limit=2).entries
    revisions = sorted(revisions, key=lambda r: ensure_aware(r.server_modified), reverse=True)
    current_text = _download_text(dbx, entry.rev)
    previous = next((r for r in revisions if r.rev != entry.rev), None)
    if previous is None:
        return {"new_file": True, **budget.fit(label, new_file_preview(current_text))}
    if (previous.size or 0) > MAX_TEXT_FILE_BYTES:
        return {"diff": None, "diff_note": "이전 리비전이 200KB를 넘어 diff 생략"}
    diff = unified_diff(_download_text(dbx, previous.rev), current_text, label)
    if not diff:
        return {"diff": None, "diff_note": "내용 변화 없음(다시 저장만 됨)"}
    return {"compared_with": "직전 리비전", **budget.fit(label, diff)}


def collect_updates(
    dbx: Any,
    root: str,
    since: datetime,
    tz: tzinfo,
    budget: OutputBudget | None = None,
    name_cache: dict[str, str] | None = None,
) -> dict[str, Any]:
    budget = budget or OutputBudget()
    my_account_id = dbx.users_get_current_account().account_id

    changed: list[tuple[FileMetadata, str]] = []
    for entry in list_all_entries(dbx, root):
        if not isinstance(entry, FileMetadata):
            continue
        if ensure_aware(entry.server_modified) <= since:
            continue
        modifier = classify_modifier(entry, my_account_id)
        if modifier is None:
            continue
        changed.append((entry, modifier))

    changed.sort(key=lambda item: ensure_aware(item[0].server_modified), reverse=True)
    notes: list[str] = []
    if len(changed) > MAX_LISTED_FILES:
        notes.append(f"변경 파일 {len(changed)}개 중 최근 {MAX_LISTED_FILES}개만 표시")

    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    diffed = 0
    for entry, modifier in changed[:MAX_LISTED_FILES]:
        rel = relative_path(entry, root)
        item: dict[str, Any] = {
            "path": rel,
            "modified": to_local_iso(ensure_aware(entry.server_modified), tz),
            "size": entry.size,
        }
        if is_text_path(rel):
            if diffed >= MAX_DIFFED_FILES:
                item.update({"diff": None, "diff_note": f"diff는 최근 {MAX_DIFFED_FILES}개 파일까지만"})
            else:
                diffed += 1
                try:
                    item.update(file_diff(dbx, entry, rel, budget))
                except Exception as exc:  # noqa: BLE001 - one bad file must not sink the check
                    item.update({"diff": None, "diff_note": f"diff 실패: {safe_error(exc)}"})
        else:
            item["diff_note"] = "텍스트 파일이 아니라 diff 없음"
        name = resolve_name(dbx, modifier, name_cache)
        grouped[top_level_group(rel)][name].append(item)

    folders = [
        {
            "folder": folder,
            "coauthors": [
                {"name": name, "files": files} for name, files in sorted(by_author.items())
            ],
        }
        for folder, by_author in sorted(grouped.items())
    ]
    payload: dict[str, Any] = {
        "configured": True,
        "ok": True,
        "source": "dropbox",
        "root": root or "/",
        "changed_file_count": len(changed),
        "folders": folders,
    }
    if notes:
        payload["notes"] = notes
    truncation = budget.report()
    if truncation:
        payload["truncation"] = truncation
    return payload


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
        return f"Dropbox API 오류(폴더 경로 DROPBOX_ROOT_FOLDER 확인): {detail}"
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
    since, basis = resolve_since(since_hours, store.last_checked(SOURCE_KEY), now, lookback_days)
    tz = config.get_timezone(env)
    secrets = config.secret_values(env)
    window = {
        "since": to_local_iso(since, tz),
        "until": to_local_iso(now, tz),
        "basis": describe_basis(basis, lookback_days),
    }
    try:
        dbx = (client_factory or make_client)(cfg)
        payload = collect_updates(dbx, normalize_root(cfg.root_folder), since, tz)
    except Exception as exc:  # noqa: BLE001 - reported to the model, never raised
        return {
            "configured": True,
            "ok": False,
            "source": "dropbox",
            "window": window,
            "error": _friendly_error(exc, secrets),
        }
    store.mark_checked(SOURCE_KEY, now)
    payload["window"] = window
    return shrink_to_limit(payload)


@tool(
    "check_dropbox_updates",
    (
        "DROPBOX_ROOT_FOLDER 아래 하위 폴더들에서 공저자(나 제외)가 수정한 파일을 확인한다. "
        "하위 폴더 → 공저자별로 묶어 파일 경로, 수정 시각, 텍스트 파일(.tex .bib .md 등)의 "
        "직전 리비전 대비 diff(새 파일은 앞부분)를 JSON으로 돌려준다. 읽기 전용. "
        "configured=false면 설정이 없는 것이니 재시도하지 말 것."
    ),
    SINCE_HOURS_SCHEMA,
    annotations=ToolAnnotations(readOnlyHint=True, maxResultSizeChars=MAX_RESULT_SIZE_CHARS),
)
async def check_dropbox_updates(args: dict[str, Any]) -> dict[str, Any]:
    since_hours = int_arg(args.get("since_hours"), 0, 0, MAX_SINCE_HOURS)
    try:
        payload = await asyncio.to_thread(run_check, since_hours)
    except Exception as exc:  # noqa: BLE001 - last line of defence
        payload = {"configured": True, "ok": False, "source": "dropbox", "error": safe_error(exc)}
    return tool_result(payload)
