from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from mungchi import config
from mungchi.state import MAX_SLACK_CHANNELS, MAX_SLACK_THREADS, StateStore, ThreadSessions, ensure_aware, resolve_since

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
    store.mark_checked("other:abc123", NOW - timedelta(hours=1))
    assert store.last_checked("dropbox") == NOW
    assert store.last_checked("other:abc123") == NOW - timedelta(hours=1)
    data = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert set(data["last_checked"]) == {"dropbox", "other:abc123"}


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
    assert config.get_lookback_days({"LOOKBACK_DAYS": "abc"}) == 1
    assert config.get_state_path({}).name == ".mungchi_state.json"
    # Relative to another folder (``service status`` looking at the service's repository).
    base = tmp_path / "repo"
    assert config.get_state_path({}, base_dir=base) == base / ".mungchi_state.json"
    assert config.get_state_path({"MUNGCHI_STATE_FILE": "state/s.json"}, base_dir=base) == base / "state" / "s.json"
    assert config.get_state_path(env, base_dir=base) == tmp_path / "x.json"


def test_lookback_days_defaults_to_one_day():
    """Only the first briefing (no checkpoint) uses it; a daily briefing starts with 24 hours."""
    assert config.DEFAULT_LOOKBACK_DAYS == 1
    assert config.get_lookback_days({}) == 1
    assert config.get_lookback_days({"LOOKBACK_DAYS": "0"}) == 1


def test_last_brief_date_round_trips_and_keeps_other_keys(tmp_path):
    store = StateStore(tmp_path / "state.json")
    assert store.last_brief_date() is None
    store.mark_checked("dropbox", NOW)
    store.mark_brief_date("2026-10-08")
    assert store.last_brief_date() == "2026-10-08"
    assert store.last_checked("dropbox") == NOW
    (tmp_path / "state.json").write_text('{"last_brief_date": "어제"}', encoding="utf-8")
    assert store.last_brief_date() is None  # anything but YYYY-MM-DD is ignored


# ---------------------------------------------------------------- Slack threads

SID_A = "aaaaaaaa-0000-0000-0000-000000000001"
SID_B = "bbbbbbbb-0000-0000-0000-000000000002"


def test_thread_sessions_roundtrip_across_instances(tmp_path):
    path = tmp_path / "threads.json"
    store = ThreadSessions(path)
    assert store.get("C1", "1.0", persona="mungchi") is None
    store.set("C1", "1.0", SID_A, persona="mungchi")
    store.set("D1", "2.0", SID_B, persona="update")
    reopened = ThreadSessions(path)  # e.g. after a bot restart
    assert reopened.get("C1", "1.0", persona="mungchi") == SID_A
    assert reopened.get("D1", "2.0", persona="update") == SID_B
    assert reopened.get("C1", "2.0", persona="mungchi") is None
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"threads": {"mungchi:C1:1.0": SID_A, "update:D1:2.0": SID_B}}
    reopened.forget("C1", "1.0", persona="mungchi")
    assert ThreadSessions(path).get("C1", "1.0", persona="mungchi") is None


def test_thread_sessions_are_isolated_per_persona(tmp_path):
    store = ThreadSessions(tmp_path / "threads.json")
    store.set("C1", "1.0", SID_A, persona="mungchi")
    store.set("C1", "1.0", SID_B, persona="update")
    # Same channel and thread, different bot -> different session (or none).
    assert store.get("C1", "1.0", persona="mungchi") == SID_A
    assert store.get("C1", "1.0", persona="update") == SID_B
    assert store.get("C1", "1.0", persona="schedule") is None
    store.forget("C1", "1.0", persona="update")
    assert store.get("C1", "1.0", persona="mungchi") == SID_A
    with pytest.raises(ValueError):
        store.get("C1", "1.0", persona="nobody")
    with pytest.raises(TypeError):
        store.get("C1", "1.0")  # the persona is never implied


def test_legacy_entries_are_migrated_to_mungchi(tmp_path):
    path = tmp_path / "threads.json"
    path.write_text(
        json.dumps({"threads": {"C1:1.0": SID_A, "update:C1:2.0": SID_B, "nobody:C1:3.0": SID_A, "a:b:c:d": SID_A}}),
        encoding="utf-8",
    )
    store = ThreadSessions(path)
    assert store.threads() == {"mungchi:C1:1.0": SID_A, "update:C1:2.0": SID_B}
    assert store.get("C1", "1.0", persona="mungchi") == SID_A  # a briefing thread from the single-bot version
    assert store.get("C1", "1.0", persona="update") is None
    store.set("D1", "9.0", SID_B, persona="schedule")  # the next write stores the new form
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"threads": {"mungchi:C1:1.0": SID_A, "update:C1:2.0": SID_B, "schedule:D1:9.0": SID_B}}


def test_thread_sessions_cap_keeps_most_recent(tmp_path):
    store = ThreadSessions(tmp_path / "threads.json", max_threads=3)
    for i in range(5):
        store.set("C1", f"{i}.0", f"session-{i:04d}", persona="mungchi" if i % 2 else "schedule")
    assert list(store.threads()) == ["schedule:C1:2.0", "mungchi:C1:3.0", "schedule:C1:4.0"]
    # Updating an old thread makes it the most recent one.
    store.set("C1", "2.0", "session-new0", persona="schedule")
    store.set("C1", "5.0", "session-0005", persona="update")
    assert list(store.threads()) == ["schedule:C1:4.0", "schedule:C1:2.0", "update:C1:5.0"]
    assert MAX_SLACK_THREADS == 200


def test_thread_sessions_cap_counts_migrated_legacy_entries(tmp_path):
    path = tmp_path / "threads.json"
    path.write_text(json.dumps({"threads": {f"C1:{i}.0": f"session-{i:04d}" for i in range(3)}}), encoding="utf-8")
    store = ThreadSessions(path, max_threads=3)
    store.set("C1", "9.0", "session-0009", persona="update")
    assert list(store.threads()) == ["mungchi:C1:1.0", "mungchi:C1:2.0", "update:C1:9.0"]


def test_thread_sessions_ignore_corrupt_or_unsafe_values(tmp_path):
    path = tmp_path / "threads.json"
    path.write_text("{not json", encoding="utf-8")
    store = ThreadSessions(path)
    assert store.get("C1", "1.0", persona="mungchi") is None
    store.set("C1", "1.0", "--bad value; rm -rf /", persona="mungchi")  # never stored or passed to --resume
    assert store.get("C1", "1.0", persona="mungchi") is None
    path.write_text(json.dumps({"threads": {"C1:1.0": "--flag", "C1:2.0": SID_A}}), encoding="utf-8")
    assert store.threads() == {"mungchi:C1:2.0": SID_A}


def test_channel_sessions_live_next_to_the_threads_and_survive_a_restart(tmp_path):
    path = tmp_path / "threads.json"
    store = ThreadSessions(path)
    assert store.channel_session("C1", persona="update") is None
    store.set("C1", "1.0", SID_A, persona="mungchi")
    store.set_channel_session("C1", SID_B, NOW, persona="update")
    reopened = ThreadSessions(path)  # e.g. after a bot restart
    assert reopened.channel_session("C1", persona="update") == (SID_B, NOW)
    assert reopened.channel_session("C1", persona="schedule") is None and reopened.channel_session("C2", persona="update") is None
    assert reopened.get("C1", "1.0", persona="mungchi") == SID_A
    # Writing a thread keeps the channel conversations, and the other way round.
    store.set("C1", "2.0", SID_A, persona="update")
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "threads": {"mungchi:C1:1.0": SID_A, "update:C1:2.0": SID_A},
        "channels": {"update:C1": {"session_id": SID_B, "at": "2026-10-05T00:00:00+00:00"}},
    }
    store.forget("C1", "1.0", persona="mungchi")
    assert store.channel_session("C1", persona="update") == (SID_B, NOW)
    store.forget_channel_session("C1", persona="update")
    assert store.channel_session("C1", persona="update") is None and store.get("C1", "2.0", persona="update") == SID_A
    assert json.loads(path.read_text(encoding="utf-8")) == {"threads": {"update:C1:2.0": SID_A}}


def test_channel_sessions_ignore_bad_values_and_are_capped(tmp_path):
    path = tmp_path / "threads.json"
    at = NOW.isoformat()
    channels = {
        "update:C1": {"session_id": "--flag", "at": at},
        "schedule:C1": {"session_id": SID_A, "at": "어제"},
        "nobody:C1": {"session_id": SID_A, "at": at},
        "C1": {"session_id": SID_A, "at": at},
        "mungchi:C1": {"session_id": SID_B, "at": at},
    }
    path.write_text(json.dumps({"channels": channels}), encoding="utf-8")
    store = ThreadSessions(path)
    assert [store.channel_session("C1", persona=p) for p in ("update", "schedule", "mungchi")] == [None, None, (SID_B, NOW)]
    store.set_channel_session("C1", "--bad value; rm -rf /", NOW, persona="update")  # never stored or passed to --resume
    assert store.channel_session("C1", persona="update") is None
    with pytest.raises(ValueError):
        store.channel_session("C1", persona="nobody")
    for i in range(MAX_SLACK_CHANNELS + 5):
        store.set_channel_session(f"C{i:03d}", SID_A, NOW, persona="update")
    kept = json.loads(path.read_text(encoding="utf-8"))["channels"]
    assert len(kept) == MAX_SLACK_CHANNELS and list(kept)[-1] == f"update:C{MAX_SLACK_CHANNELS + 4:03d}"


def test_slack_threads_file_sits_next_to_state_file(tmp_path):
    env = {"MUNGCHI_STATE_FILE": str(tmp_path / "x" / "state.json")}
    assert config.get_slack_threads_path(env) == tmp_path / "x" / ".mungchi_slack_threads.json"
    assert config.get_slack_threads_path({}).name == ".mungchi_slack_threads.json"


def test_recent_greetings_keep_the_last_seven_and_the_other_keys(tmp_path):
    store = StateStore(tmp_path / "state.json")
    assert store.recent_greetings() == []
    store.mark_brief_date("2026-10-07")
    store.mark_greeting("2026-10-01", "  좋은 아침이에요! 10월 1일(목)  ")
    assert store.recent_greetings() == [("2026-10-01", "좋은 아침이에요! 10월 1일(목)")]
    for day in range(2, 10):
        store.mark_greeting(f"2026-10-{day:02d}", f"인사 {day}")
    # Capped at seven, oldest first: 10/01 and 10/02 dropped out.
    assert store.recent_greetings() == [(f"2026-10-{day:02d}", f"인사 {day}") for day in range(3, 10)]
    assert store.last_brief_date() == "2026-10-07"
    data = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert len(data["recent_greetings"]) == 7 and data["recent_greetings"][-1] == {"date": "2026-10-09", "text": "인사 9"}
    assert "last_greeting" not in data


def test_recent_greetings_ignore_junk(tmp_path):
    store = StateStore(tmp_path / "state.json")
    for junk in ([{"date": "어제", "text": "안녕"}, {"date": "2026-10-08", "text": "  "}, "인사", None, 3], "인사", None, {"a": 1}):
        (tmp_path / "state.json").write_text(json.dumps({"recent_greetings": junk}, ensure_ascii=False), encoding="utf-8")
        assert store.recent_greetings() == []
    good = [{"date": "2026-10-08", "text": "좋은 아침이에요!"}, {"date": "어제", "text": "안녕"}]
    (tmp_path / "state.json").write_text(json.dumps({"recent_greetings": good}, ensure_ascii=False), encoding="utf-8")
    assert store.recent_greetings() == [("2026-10-08", "좋은 아침이에요!")]


def test_a_legacy_last_greeting_is_migrated_into_the_list(tmp_path):
    path = tmp_path / "state.json"
    legacy = "똑똑! 🚪 2026년 10월 7일(수) 아침 브리핑입니다~"
    path.write_text(
        json.dumps({"last_greeting": {"date": "2026-10-07", "text": legacy}, "last_brief_date": "2026-10-07"}, ensure_ascii=False),
        encoding="utf-8",
    )
    store = StateStore(path)
    assert store.recent_greetings() == [("2026-10-07", legacy)]  # read as a list of one
    store.mark_greeting("2026-10-08", "좋은 아침이에요! 10월 8일 목요일 브리핑 시작할게요")
    assert store.recent_greetings() == [("2026-10-07", legacy), ("2026-10-08", "좋은 아침이에요! 10월 8일 목요일 브리핑 시작할게요")]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "last_greeting" not in data and data["last_brief_date"] == "2026-10-07"  # moved, nothing else touched
    for junk in ({"date": "어제", "text": "안녕"}, {"date": "2026-10-08", "text": "  "}, "인사", None):
        path.write_text(json.dumps({"last_greeting": junk}, ensure_ascii=False), encoding="utf-8")
        assert store.recent_greetings() == []
