"""Persisted state: "last checked" timestamps per source and calendar events
waiting for the user's confirmation (``.mungchi_state.json``), and the Slack
thread -> Agent SDK session map with each bot's top-level conversation in the
briefing channel (``.mungchi_slack_threads.json``)."""

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
# Top-level conversations (one per bot and channel) kept in the same file.
MAX_SLACK_CHANNELS = 50
# Calendar events proposed from a pasted note wait this long for "네" / "아니요".
PROPOSAL_TTL = timedelta(hours=24)
MAX_PENDING_PROPOSALS = 50
_PENDING_KEY = "pending_events"
# Session ids are UUIDs; anything else in the file is ignored rather than
# passed to the CLI as ``--resume``.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 고뭉치's morning-briefing greetings kept so the next ones differ (about a week).
MAX_RECENT_GREETINGS = 7
# Before the list there was one greeting under this key; it is read as one of the list and dropped on the next write.
_LEGACY_GREETING_KEY = "last_greeting"


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


def _greeting_entry(record: Any) -> tuple[str, str] | None:
    """``(date, text)`` of a stored greeting, or None for anything malformed."""
    if not isinstance(record, dict):
        return None
    day, text = record.get("date"), record.get("text")
    if not (isinstance(day, str) and _DATE_RE.match(day) and isinstance(text, str) and text.strip()):
        return None
    return day, text.strip()


def _recent_greetings(data: dict[str, Any]) -> list[tuple[str, str]]:
    """The stored greetings oldest first, at most ``MAX_RECENT_GREETINGS``; a legacy ``last_greeting`` is one of them."""
    raw = data.get("recent_greetings")
    greetings = [entry for entry in map(_greeting_entry, raw if isinstance(raw, list) else []) if entry]
    legacy = _greeting_entry(data.get(_LEGACY_GREETING_KEY))
    if legacy is not None and legacy not in greetings:
        greetings.append(legacy)
    greetings.sort(key=lambda item: item[0])  # by date; same-day ones keep their order
    return greetings[-MAX_RECENT_GREETINGS:]


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
    "recent_greetings": [{"date": "<YYYY-MM-DD>", "text": "<고뭉치's morning greeting>"}, ...] (last 7, oldest first),
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

    def recent_greetings(self) -> list[tuple[str, str]]:
        """``[(local date, text), ...]`` of the last ``MAX_RECENT_GREETINGS`` morning-briefing greetings, oldest first.

        A file from before the list (one ``last_greeting``) reads as a list of that one greeting.
        """
        return _recent_greetings(self.load())

    def mark_greeting(self, day: str, text: str) -> None:
        """Add today's greeting to the recent ones (the oldest drops out after seven), so the next ones differ.

        A legacy ``last_greeting`` is moved into the list and its key removed.
        """
        with _LOCK:
            data = self.load()
            greetings = _recent_greetings(data) + [(day, text.strip())]
            greetings.sort(key=lambda item: item[0])  # by date; same-day ones keep their order
            data["recent_greetings"] = [{"date": d, "text": t} for d, t in greetings[-MAX_RECENT_GREETINGS:]]
            data.pop(_LEGACY_GREETING_KEY, None)
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

    def take_pending_proposal(self, key: str, now: datetime, proposal_id: str | None = None) -> dict[str, Any] | None:
        """Remove and return the proposal under ``key`` in one step (None if there is none or it expired).

        Two confirmations of one proposal can never both get it. With
        ``proposal_id`` only that very proposal is taken: a newer one under the
        same key (or none) returns None and is left as it is.
        """
        with _LOCK:
            data = self.load()
            raw = data.get(_PENDING_KEY)
            if not isinstance(raw, dict) or key not in raw:
                return None
            proposal = self._live_proposals(data, now).get(key)
            if proposal_id is not None and (proposal is None or proposal.get("id") != proposal_id):
                return None
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

    The same file also keeps each bot's top-level conversation in a channel
    (``"channels": {"<persona>:<channel>": {"session_id": ..., "at": "<iso8601>"}}``):
    the session of the bot's last top-level reply there and when it finished.
    Whether it is still fresh enough to continue is the caller's decision.
    """

    def __init__(self, path: Path | str, max_threads: int = MAX_SLACK_THREADS):
        self.path = Path(path)
        self.max_threads = max_threads

    @staticmethod
    def key(persona: str, channel: str, thread_ts: str) -> str:
        if persona not in PERSONAS:
            raise ValueError(f"unknown persona: {persona!r}")
        return f"{persona}:{channel}:{thread_ts}"

    @staticmethod
    def channel_key(persona: str, channel: str) -> str:
        if persona not in PERSONAS:
            raise ValueError(f"unknown persona: {persona!r}")
        return f"{persona}:{channel}"

    def threads(self) -> dict[str, str]:
        return self._threads(_read_json(self.path))

    def _threads(self, data: dict[str, Any]) -> dict[str, str]:
        raw = data.get("threads")
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

    @staticmethod
    def _channels(data: dict[str, Any]) -> dict[str, dict[str, str]]:
        """Well-formed top-level conversations, oldest first (as stored)."""
        raw = data.get("channels")
        if not isinstance(raw, dict):
            return {}
        channels: dict[str, dict[str, str]] = {}
        for key, value in raw.items():
            if not (isinstance(key, str) and isinstance(value, dict)):
                continue
            parts = key.split(":")
            session_id, at = value.get("session_id"), value.get("at")
            if len(parts) != 2 or parts[0] not in PERSONAS or not parts[1]:
                continue
            if not (isinstance(session_id, str) and _SESSION_ID_RE.match(session_id)) or _parse_iso(at) is None:
                continue
            channels[key] = {"session_id": session_id, "at": at}
        return channels

    def _write(self, threads: dict[str, str], channels: dict[str, dict[str, str]]) -> None:
        data: dict[str, Any] = {"threads": threads}
        if channels:
            data["channels"] = channels
        _write_json(self.path, data)

    def get(self, channel: str, thread_ts: str, *, persona: str) -> str | None:
        return self.threads().get(self.key(persona, channel, thread_ts))

    def set(self, channel: str, thread_ts: str, session_id: str, *, persona: str) -> None:
        if not _SESSION_ID_RE.match(session_id or ""):
            return
        key = self.key(persona, channel, thread_ts)
        with _LOCK:
            data = _read_json(self.path)
            threads = self._threads(data)
            threads.pop(key, None)  # re-insert as the most recent thread
            threads[key] = session_id
            while len(threads) > self.max_threads:
                del threads[next(iter(threads))]
            self._write(threads, self._channels(data))

    def forget(self, channel: str, thread_ts: str, *, persona: str) -> None:
        key = self.key(persona, channel, thread_ts)
        with _LOCK:
            data = _read_json(self.path)
            threads = self._threads(data)
            if threads.pop(key, None) is not None:
                self._write(threads, self._channels(data))

    # -- each bot's top-level conversation in a channel

    def channel_session(self, channel: str, *, persona: str) -> tuple[str, datetime] | None:
        """``(session id, when its last top-level reply finished)`` of ``persona`` in ``channel``, or None."""
        entry = self._channels(_read_json(self.path)).get(self.channel_key(persona, channel))
        if entry is None:
            return None
        at = _parse_iso(entry["at"])
        return (entry["session_id"], at) if at is not None else None

    def set_channel_session(self, channel: str, session_id: str, when: datetime, *, persona: str) -> None:
        """Remember ``session_id`` as ``persona``'s top-level conversation in ``channel``, last active at ``when``."""
        if not _SESSION_ID_RE.match(session_id or ""):
            return
        key = self.channel_key(persona, channel)
        stamp = ensure_aware(when).astimezone(timezone.utc).isoformat()
        with _LOCK:
            data = _read_json(self.path)
            channels = self._channels(data)
            channels.pop(key, None)  # re-insert as the most recent one
            channels[key] = {"session_id": session_id, "at": stamp}
            while len(channels) > MAX_SLACK_CHANNELS:
                del channels[next(iter(channels))]
            self._write(self._threads(data), channels)

    def forget_channel_session(self, channel: str, *, persona: str) -> None:
        key = self.channel_key(persona, channel)
        with _LOCK:
            data = _read_json(self.path)
            channels = self._channels(data)
            if channels.pop(key, None) is not None:
                self._write(self._threads(data), channels)


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
