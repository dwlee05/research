"""Persisted state: "last checked" timestamps per source (``.mungchi_state.json``)
and the Slack thread -> Agent SDK session map (``.mungchi_slack_threads.json``)."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Tool handlers run in worker threads and may run concurrently
# (e.g. Dropbox and Overleaf checks in parallel), so serialize file updates.
_LOCK = threading.Lock()

MAX_SLACK_THREADS = 200
# Session ids are UUIDs; anything else in the file is ignored rather than
# passed to the CLI as ``--resume``.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware(dt: datetime) -> datetime:
    """Treat naive datetimes as UTC (Dropbox ``server_modified`` is naive UTC)."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return ensure_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


class StateStore:
    """Tiny JSON store: ``{"last_checked": {"<source>": "<iso8601>"}}``."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        return _read_json(self.path)

    def last_checked(self, source: str) -> datetime | None:
        return _parse_iso(self.load().get("last_checked", {}).get(source))

    def mark_checked(self, source: str, when: datetime) -> None:
        with _LOCK:
            data = self.load()
            stamps = data.get("last_checked")
            if not isinstance(stamps, dict):
                stamps = {}
            stamps[source] = ensure_aware(when).astimezone(timezone.utc).isoformat()
            data["last_checked"] = stamps
            _write_json(self.path, data)


class ThreadSessions:
    """Slack thread -> Agent SDK session id: ``{"threads": {"<channel>:<thread_ts>": "<session_id>"}}``.

    Entries are kept oldest first and capped at ``max_threads``. The file is
    re-read on every access so the bot sees threads started by a cron
    ``--brief --slack`` run without restarting.
    """

    def __init__(self, path: Path | str, max_threads: int = MAX_SLACK_THREADS):
        self.path = Path(path)
        self.max_threads = max_threads

    @staticmethod
    def key(channel: str, thread_ts: str) -> str:
        return f"{channel}:{thread_ts}"

    def threads(self) -> dict[str, str]:
        raw = _read_json(self.path).get("threads")
        if not isinstance(raw, dict):
            return {}
        return {
            key: value
            for key, value in raw.items()
            if isinstance(key, str) and isinstance(value, str) and _SESSION_ID_RE.match(value)
        }

    def get(self, channel: str, thread_ts: str) -> str | None:
        return self.threads().get(self.key(channel, thread_ts))

    def set(self, channel: str, thread_ts: str, session_id: str) -> None:
        if not _SESSION_ID_RE.match(session_id or ""):
            return
        key = self.key(channel, thread_ts)
        with _LOCK:
            threads = self.threads()
            threads.pop(key, None)  # re-insert as the most recent thread
            threads[key] = session_id
            while len(threads) > self.max_threads:
                del threads[next(iter(threads))]
            _write_json(self.path, {"threads": threads})

    def forget(self, channel: str, thread_ts: str) -> None:
        with _LOCK:
            threads = self.threads()
            if threads.pop(self.key(channel, thread_ts), None) is not None:
                _write_json(self.path, {"threads": threads})


def resolve_since(
    since_hours: int,
    last_checked: datetime | None,
    now: datetime,
    lookback_days: int,
) -> tuple[datetime, str]:
    """Pick the start of the check window.

    Returns ``(since, basis)`` where ``basis`` explains the choice:
    ``"since_hours"`` (explicit), ``"last_checked"`` (stored) or ``"lookback_days"`` (fallback).
    """
    now = ensure_aware(now)
    if since_hours and since_hours > 0:
        return now - timedelta(hours=since_hours), "since_hours"
    if last_checked is not None:
        last_checked = ensure_aware(last_checked)
        if last_checked <= now:
            return last_checked, "last_checked"
    return now - timedelta(days=lookback_days), "lookback_days"
