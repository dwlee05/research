"""Persisted state: "last checked" timestamps per source and calendar events
waiting for the user's confirmation (``.mungchi_state.json``), and the Slack
thread -> Agent SDK session map (``.mungchi_slack_threads.json``)."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .personas import MUNGCHI, PERSONAS

# Tool handlers run in worker threads and may run concurrently
# (e.g. two Slack bots checking Dropbox at once), so serialize file updates.
_LOCK = threading.Lock()

MAX_SLACK_THREADS = 200
# Calendar events proposed from a pasted note wait this long for "네" / "아니요".
PROPOSAL_TTL = timedelta(hours=24)
MAX_PENDING_PROPOSALS = 50
_PENDING_KEY = "pending_events"
# Session ids are UUIDs; anything else in the file is ignored rather than
# passed to the CLI as ``--resume``.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
    """Tiny JSON store.

    ``{"last_checked": {"<source>": "<iso8601>"},
    "credit_alert": {"renewal_date": "<renewal_date>", "alerted_at": "<iso8601>"},
    "last_brief_date": "<YYYY-MM-DD>",
    "running_version": "<abc1234>", "running_since": "<iso8601>",
    "pending_events": {"<conversation key>": {..., "created_at": "<iso8601>", "expires_at": "<iso8601>"}}}``.
    Every write keeps the other keys as they are.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        return _read_json(self.path)

    def last_checked(self, source: str) -> datetime | None:
        stamps = self.load().get("last_checked")
        return _parse_iso(stamps.get(source)) if isinstance(stamps, dict) else None

    def credit_alert_period(self) -> str | None:
        """The renewal date (period key) the low-credit alert was last sent for, if any."""
        record = self.load().get("credit_alert")
        period = record.get("renewal_date") if isinstance(record, dict) else None
        return period if isinstance(period, str) and period else None

    def mark_credit_alert(self, period: str, when: datetime) -> None:
        with _LOCK:
            data = self.load()
            data["credit_alert"] = {
                "renewal_date": period,
                "alerted_at": ensure_aware(when).astimezone(timezone.utc).isoformat(),
            }
            _write_json(self.path, data)

    def last_brief_date(self) -> str | None:
        """Local date (``YYYY-MM-DD``) the scheduled morning briefing was last started for, if any."""
        value = self.load().get("last_brief_date")
        return value if isinstance(value, str) and _DATE_RE.match(value) else None

    def mark_brief_date(self, day: str) -> None:
        """Record that today's scheduled briefing has started (written before posting, never twice a day)."""
        with _LOCK:
            data = self.load()
            data["last_brief_date"] = day
            _write_json(self.path, data)

    def running(self) -> tuple[str, datetime | None] | None:
        """``(code version, start time)`` the Slack bots last started with, if recorded."""
        data = self.load()
        version = data.get("running_version")
        if not isinstance(version, str) or not version.strip():
            return None
        return version.strip(), _parse_iso(data.get("running_since"))

    def mark_running(self, version: str, when: datetime) -> None:
        """Record which code the Slack bots started with (``service status`` compares it with the repository)."""
        with _LOCK:
            data = self.load()
            data["running_version"] = version
            data["running_since"] = ensure_aware(when).astimezone(timezone.utc).isoformat()
            _write_json(self.path, data)

    def mark_checked(self, source: str, when: datetime) -> None:
        with _LOCK:
            data = self.load()
            stamps = data.get("last_checked")
            if not isinstance(stamps, dict):
                stamps = {}
            stamps[source] = ensure_aware(when).astimezone(timezone.utc).isoformat()
            data["last_checked"] = stamps
            _write_json(self.path, data)

    # -- calendar events waiting for "네" / "아니요" (one proposal per conversation key)

    @staticmethod
    def _live_proposals(data: dict[str, Any], now: datetime) -> dict[str, dict[str, Any]]:
        """Unexpired, well-formed pending proposals, oldest first."""
        raw = data.get(_PENDING_KEY)
        if not isinstance(raw, dict):
            return {}
        now = ensure_aware(now)
        live: dict[str, dict[str, Any]] = {}
        for key, proposal in raw.items():
            if not (isinstance(key, str) and isinstance(proposal, dict)):
                continue
            expires = _parse_iso(proposal.get("expires_at"))
            if expires is None or expires <= now or not isinstance(proposal.get("events"), list):
                continue
            live[key] = proposal
        return live

    def pending_proposal(self, key: str, now: datetime) -> dict[str, Any] | None:
        """The proposal waiting under ``key``, or None (none, or expired)."""
        return self._live_proposals(self.load(), now).get(key) if key else None

    def save_pending_proposal(
        self, key: str, proposal: dict[str, Any], now: datetime, ttl: timedelta = PROPOSAL_TTL
    ) -> dict[str, Any]:
        """Store ``proposal`` under ``key``, replacing the one there; expired ones are dropped."""
        now = ensure_aware(now).astimezone(timezone.utc)
        stored = {**proposal, "created_at": now.isoformat(), "expires_at": (now + ttl).isoformat()}
        with _LOCK:
            data = self.load()
            live = self._live_proposals(data, now)
            live.pop(key, None)  # re-insert as the newest
            live[key] = stored
            while len(live) > MAX_PENDING_PROPOSALS:
                del live[next(iter(live))]
            data[_PENDING_KEY] = live
            _write_json(self.path, data)
        return stored

    def take_pending_proposal(self, key: str, now: datetime) -> dict[str, Any] | None:
        """Remove and return the proposal under ``key`` in one step (None if there is none or it expired).

        Two confirmations of one proposal can never both get it.
        """
        with _LOCK:
            data = self.load()
            raw = data.get(_PENDING_KEY)
            if not isinstance(raw, dict) or key not in raw:
                return None
            proposal = self._live_proposals(data, now).get(key)
            data[_PENDING_KEY] = {k: v for k, v in self._live_proposals(data, now).items() if k != key}
            _write_json(self.path, data)
        return proposal

    def clear_pending_proposal(self, key: str) -> bool:
        """Drop the proposal under ``key`` (expired or not). True if there was one."""
        with _LOCK:
            data = self.load()
            raw = data.get(_PENDING_KEY)
            if not isinstance(raw, dict) or key not in raw:
                return False
            del raw[key]
            data[_PENDING_KEY] = raw
            _write_json(self.path, data)
        return True


class ThreadSessions:
    """Slack thread -> Agent SDK session id, per persona (bot).

    File format: ``{"threads": {"<persona>:<channel>:<thread_ts>": "<session_id>"}}``.
    The persona is part of the key, so different bots answering in the same
    thread never resume each other's sessions. Entries written before there
    were several bots (``"<channel>:<thread_ts>"``) belong to 고뭉치 and are
    read as ``mungchi`` entries; the next write stores them in the new form.

    Entries are kept oldest first and capped at ``max_threads`` in total. The
    file is re-read on every access so the bots see threads started by a
    separate ``--brief --slack`` run without restarting.
    """

    def __init__(self, path: Path | str, max_threads: int = MAX_SLACK_THREADS):
        self.path = Path(path)
        self.max_threads = max_threads

    @staticmethod
    def key(persona: str, channel: str, thread_ts: str) -> str:
        if persona not in PERSONAS:
            raise ValueError(f"unknown persona: {persona!r}")
        return f"{persona}:{channel}:{thread_ts}"

    def threads(self) -> dict[str, str]:
        raw = _read_json(self.path).get("threads")
        if not isinstance(raw, dict):
            return {}
        threads: dict[str, str] = {}
        for key, value in raw.items():
            if not (isinstance(key, str) and isinstance(value, str) and _SESSION_ID_RE.match(value)):
                continue
            parts = key.split(":")
            if len(parts) == 2:  # legacy entry from the single-bot version
                key = self.key(MUNGCHI, *parts)
            elif len(parts) != 3 or parts[0] not in PERSONAS:
                continue
            threads[key] = value
        return threads

    def get(self, channel: str, thread_ts: str, *, persona: str) -> str | None:
        return self.threads().get(self.key(persona, channel, thread_ts))

    def set(self, channel: str, thread_ts: str, session_id: str, *, persona: str) -> None:
        if not _SESSION_ID_RE.match(session_id or ""):
            return
        key = self.key(persona, channel, thread_ts)
        with _LOCK:
            threads = self.threads()
            threads.pop(key, None)  # re-insert as the most recent thread
            threads[key] = session_id
            while len(threads) > self.max_threads:
                del threads[next(iter(threads))]
            _write_json(self.path, {"threads": threads})

    def forget(self, channel: str, thread_ts: str, *, persona: str) -> None:
        key = self.key(persona, channel, thread_ts)
        with _LOCK:
            threads = self.threads()
            if threads.pop(key, None) is not None:
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
