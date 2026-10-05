from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from mungchi import config
from mungchi.state import MAX_SLACK_THREADS, StateStore, ThreadSessions, ensure_aware, resolve_since

NOW = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)


def test_since_hours_wins_over_stored_timestamp():
    stored = NOW - timedelta(days=3)
    since, basis = resolve_since(6, stored, NOW, lookback_days=7)
    assert since == NOW - timedelta(hours=6)
    assert basis == "since_hours"


def test_stored_timestamp_used_when_since_hours_is_zero():
    stored = NOW - timedelta(hours=30)
    since, basis = resolve_since(0, stored, NOW, lookback_days=7)
    assert since == stored
    assert basis == "last_checked"


def test_falls_back_to_lookback_days_without_state():
    since, basis = resolve_since(0, None, NOW, lookback_days=7)
    assert since == NOW - timedelta(days=7)
    assert basis == "lookback_days"


def test_future_stored_timestamp_is_ignored():
    since, basis = resolve_since(0, NOW + timedelta(hours=1), NOW, lookback_days=3)
    assert (since, basis) == (NOW - timedelta(days=3), "lookback_days")


def test_naive_datetimes_are_treated_as_utc():
    naive = datetime(2026, 10, 1, 12, 0)
    assert ensure_aware(naive) == datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    since, _ = resolve_since(0, naive, NOW, lookback_days=7)
    assert since.tzinfo is not None


def test_state_store_roundtrip_and_independent_sources(tmp_path):
    store = StateStore(tmp_path / "state.json")
    assert store.last_checked("dropbox") is None
    store.mark_checked("dropbox", NOW)
    store.mark_checked("overleaf:abc123", NOW - timedelta(hours=1))
    assert store.last_checked("dropbox") == NOW
    assert store.last_checked("overleaf:abc123") == NOW - timedelta(hours=1)
    data = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert set(data["last_checked"]) == {"dropbox", "overleaf:abc123"}


def test_corrupt_state_file_is_treated_as_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    store = StateStore(path)
    assert store.last_checked("dropbox") is None
    store.mark_checked("dropbox", NOW)
    assert store.last_checked("dropbox") == NOW


def test_state_path_and_lookback_are_env_overridable(tmp_path):
    env = {"MUNGCHI_STATE_FILE": str(tmp_path / "x.json"), "LOOKBACK_DAYS": "3"}
    assert config.get_state_path(env) == tmp_path / "x.json"
    assert config.get_lookback_days(env) == 3
    assert config.get_lookback_days({"LOOKBACK_DAYS": "abc"}) == 7
    assert config.get_state_path({}).name == ".mungchi_state.json"


# ---------------------------------------------------------------- Slack threads

SID_A = "aaaaaaaa-0000-0000-0000-000000000001"
SID_B = "bbbbbbbb-0000-0000-0000-000000000002"


def test_thread_sessions_roundtrip_across_instances(tmp_path):
    path = tmp_path / "threads.json"
    store = ThreadSessions(path)
    assert store.get("C1", "1.0") is None
    store.set("C1", "1.0", SID_A)
    store.set("D1", "2.0", SID_B)
    reopened = ThreadSessions(path)  # e.g. after a bot restart
    assert reopened.get("C1", "1.0") == SID_A
    assert reopened.get("D1", "2.0") == SID_B
    assert reopened.get("C1", "2.0") is None
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"threads": {"C1:1.0": SID_A, "D1:2.0": SID_B}}
    reopened.forget("C1", "1.0")
    assert ThreadSessions(path).get("C1", "1.0") is None


def test_thread_sessions_cap_keeps_most_recent(tmp_path):
    store = ThreadSessions(tmp_path / "threads.json", max_threads=3)
    for i in range(5):
        store.set("C1", f"{i}.0", f"session-{i:04d}")
    assert list(store.threads()) == ["C1:2.0", "C1:3.0", "C1:4.0"]
    # Updating an old thread makes it the most recent one.
    store.set("C1", "2.0", "session-new0")
    store.set("C1", "5.0", "session-0005")
    assert list(store.threads()) == ["C1:4.0", "C1:2.0", "C1:5.0"]
    assert MAX_SLACK_THREADS == 200


def test_thread_sessions_ignore_corrupt_or_unsafe_values(tmp_path):
    path = tmp_path / "threads.json"
    path.write_text("{not json", encoding="utf-8")
    store = ThreadSessions(path)
    assert store.get("C1", "1.0") is None
    store.set("C1", "1.0", "--bad value; rm -rf /")  # never stored or passed to --resume
    assert store.get("C1", "1.0") is None
    path.write_text(json.dumps({"threads": {"C1:1.0": "--flag", "C1:2.0": SID_A}}), encoding="utf-8")
    assert store.threads() == {"C1:2.0": SID_A}


def test_slack_threads_file_sits_next_to_state_file(tmp_path):
    env = {"MUNGCHI_STATE_FILE": str(tmp_path / "x" / "state.json")}
    assert config.get_slack_threads_path(env) == tmp_path / "x" / ".mungchi_slack_threads.json"
    assert config.get_slack_threads_path({}).name == ".mungchi_slack_threads.json"
