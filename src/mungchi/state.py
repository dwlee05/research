"""Persisted "last checked" timestamps per source (``.mungchi_state.json``)."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Tool handlers run in worker threads and may run concurrently
# (e.g. Dropbox and Overleaf checks in parallel), so serialize file updates.
_LOCK = threading.Lock()


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
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

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
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)


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


def describe_basis(basis: str, lookback_days: int) -> str:
    """Korean explanation of the window basis for the model to relay."""
    return {
        "since_hours": "요청한 시간 범위 기준",
        "last_checked": "마지막 확인 시각 이후",
        "lookback_days": f"저장된 확인 기록이 없어 최근 {lookback_days}일 기준",
    }.get(basis, basis)
