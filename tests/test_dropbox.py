from __future__ import annotations

import asyncio
import io
import json
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from dropbox.exceptions import ApiError
from dropbox.files import FileMetadata, FileSharingInfo, FolderMetadata, ListFolderError, LookupError

from mungchi import config, dropbox_check
from mungchi.dropbox_check import run_dropbox_check
from mungchi.main import build_parser, main
from mungchi.state import StateStore
from mungchi.tools import dropbox_tool
from mungchi.tools.dropbox_tool import (
    EXCLUDED_BEFORE_WINDOW,
    EXCLUDED_MINE,
    EXCLUDED_TEMP,
    EXCLUDED_UNKNOWN_MODIFIER,
    INCLUDED,
    MAX_LISTED_FILES,
    TEMP_FILE_PATTERNS,
    UNKNOWN_MODIFIER,
    check_dropbox_updates,
    classify_entry,
    classify_modifier,
    collect_updates,
    folder_link,
    is_temp_file,
    make_check_dropbox_updates,
    normalize_root,
    relative_path,
    run_check,
    split_group,
    tally,
)

ME = "dbid:" + "A" * 35
KIM = "dbid:" + "B" * 35
PARK = "dbid:" + "C" * 35
MY_NAME = "나연구"
ROOT = "/20_연구-진행"
# "/20_연구-진행" percent-encoded from its UTF-8 bytes.
ROOT_LINK = "https://www.dropbox.com/home/20_%EC%97%B0%EA%B5%AC-%EC%A7%84%ED%96%89"
TOKEN = "sl.fake-token-value-0123456789abcdef"
SINCE = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
SEOUL = ZoneInfo("Asia/Seoul")
_rev_counter = iter(range(0x100000000, 0x1FFFFFFFF))

# Every key the compact result may contain; sizes, ids, revs and diffs are gone.
ALLOWED_KEYS = {
    "configured", "folder", "since", "since_basis", "total_files", "stats", "groups", "omitted",
    "subfolder", "link", "by", "name", "files", "path", "modified",
    "scanned", "changed_in_window", "excluded_mine", "excluded_unknown_modifier", "excluded_temp",
}


def rev() -> str:
    return f"{next(_rev_counter):09x}"


def fm(path, modified, *, modified_by=None, shared=True, size=100):
    """A real Dropbox FileMetadata; ``server_modified`` is naive UTC like the API."""
    sharing = None
    if shared:
        sharing = FileSharingInfo(read_only=False, parent_shared_folder_id="1234", modified_by=modified_by)
    return FileMetadata(
        name=path.rsplit("/", 1)[-1],
        id="id:" + "x" * 10,
        client_modified=modified,
        server_modified=modified,
        rev=rev(),
        size=size,
        path_lower=path.lower(),
        path_display=path,
        sharing_info=sharing,
    )


class FakeDropbox:
    """Metadata-only fake. Any content endpoint (or any other call) fails the test.

    Forbidden calls are also recorded, because ``run_check`` turns exceptions
    into an error payload instead of raising.
    """

    def __init__(self, entries, names=None, page_size=2):
        self.forbidden: list[str] = []
        self.entries = entries
        self.names = names or {}
        self.page_size = page_size
        self.account_lookups = 0
        self.listed_paths: list[str] = []

    def __getattr__(self, name):
        self.forbidden.append(name)
        raise AssertionError(f"unexpected Dropbox call: {name}")

    def files_download(self, *args, **kwargs):
        self.forbidden.append("files_download")
        raise AssertionError("files_download must never be called")

    def files_list_revisions(self, *args, **kwargs):
        self.forbidden.append("files_list_revisions")
        raise AssertionError("files_list_revisions must never be called")

    def users_get_current_account(self):
        return SimpleNamespace(account_id=ME, name=SimpleNamespace(display_name=MY_NAME))

    def _page(self, start):
        chunk = self.entries[start : start + self.page_size]
        more = start + self.page_size < len(self.entries)
        return SimpleNamespace(entries=chunk, has_more=more, cursor=str(start + self.page_size))

    def files_list_folder(self, path, recursive=False):
        assert recursive is True
        self.listed_paths.append(path)
        return self._page(0)

    def files_list_folder_continue(self, cursor):
        return self._page(int(cursor))

    def users_get_account(self, account_id):
        self.account_lookups += 1
        return SimpleNamespace(name=SimpleNamespace(display_name=self.names[account_id]))


def at(day, hour=9, minute=0):
    return datetime(2026, 10, day, hour, minute)  # naive UTC, as returned by Dropbox


def all_keys(node):
    if isinstance(node, dict):
        yield from node
        for value in node.values():
            yield from all_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from all_keys(item)


def env_with(**extra):
    return {"DROPBOX_ACCESS_TOKEN": TOKEN, **extra}


# ---------------------------------------------------------------- helpers


def test_classify_modifier_excludes_me_and_unshared_files():
    assert classify_modifier(fm("/r/a.tex", at(2), modified_by=KIM), ME) == KIM
    assert classify_modifier(fm("/r/a.tex", at(2), modified_by=ME), ME) is None
    assert classify_modifier(fm("/r/a.tex", at(2), modified_by=None), ME) == UNKNOWN_MODIFIER
    assert classify_modifier(fm("/r/a.tex", at(2), shared=False), ME) is None


def test_classify_entry_gives_exactly_one_decision():
    # In the window (after SINCE = 10-01 00:00 UTC), in a shared folder:
    assert classify_entry(fm("/r/a.tex", at(2), modified_by=KIM), ME, SINCE) == INCLUDED
    assert classify_entry(fm("/r/a.tex", at(2), modified_by=ME), ME, SINCE) == EXCLUDED_MINE
    # Shared but Dropbox does not say who: still reported, as "확인 불가" (the existing rule).
    assert classify_entry(fm("/r/a.tex", at(2), modified_by=None), ME, SINCE) == INCLUDED
    # Not in a shared folder (no sharing_info): nobody can tell who modified it.
    assert classify_entry(fm("/r/a.tex", at(2), shared=False), ME, SINCE) == EXCLUDED_UNKNOWN_MODIFIER
    # At or before the start of the window, whoever modified it.
    for kwargs in ({"modified_by": KIM}, {"modified_by": ME}, {"modified_by": None}, {"shared": False}):
        assert classify_entry(fm("/r/a.tex", datetime(2026, 9, 30), **kwargs), ME, SINCE) == EXCLUDED_BEFORE_WINDOW
        assert classify_entry(fm("/r/a.tex", at(1, 0), **kwargs), ME, SINCE) == EXCLUDED_BEFORE_WINDOW


def test_tally_counts_decisions():
    decisions = [INCLUDED, INCLUDED, EXCLUDED_MINE, EXCLUDED_UNKNOWN_MODIFIER, EXCLUDED_BEFORE_WINDOW, EXCLUDED_TEMP]
    assert tally(decisions) == {
        "scanned": 6,
        "changed_in_window": 4,  # temporary files are taken out before the window
        "excluded_mine": 1,
        "excluded_unknown_modifier": 1,
        "excluded_temp": 1,
    }
    assert tally([]) == {
        "scanned": 0,
        "changed_in_window": 0,
        "excluded_mine": 0,
        "excluded_unknown_modifier": 0,
        "excluded_temp": 0,
    }


def test_normalize_root_handles_korean_hyphen_and_slashes():
    assert normalize_root("20_연구-진행") == ROOT
    assert normalize_root("/20_연구-진행/") == ROOT
    assert normalize_root("20_연구-진행/") == ROOT
    assert normalize_root("Research/20_연구-진행/") == "/Research/20_연구-진행"
    assert normalize_root("") == ""
    assert normalize_root("/") == ""


def test_split_group_uses_immediate_subfolder():
    assert split_group("논문A/sections/intro.tex") == ("논문A", "sections/intro.tex")
    assert split_group("README.md") == ("(루트)", "README.md")


def test_relative_path_matches_decomposed_korean_names():
    # macOS can hand Dropbox NFD (decomposed) Hangul; it must still group under the root.
    entry = fm(unicodedata.normalize("NFD", f"{ROOT}/논문A/a.tex"), at(3), modified_by=KIM)
    assert relative_path(entry, ROOT) == "논문A/a.tex"


def test_folder_link_encodes_korean_and_keeps_slashes():
    assert folder_link(ROOT, "논문 A") == ROOT_LINK + "/%EB%85%BC%EB%AC%B8%20A"
    assert folder_link(ROOT, "Paper-B") == ROOT_LINK + "/Paper-B"
    assert folder_link(ROOT, "(루트)") == ROOT_LINK
    assert folder_link("/Research/20_연구-진행", "Paper-B") == (
        "https://www.dropbox.com/home/Research/20_%EC%97%B0%EA%B5%AC-%EC%A7%84%ED%96%89/Paper-B"
    )
    assert folder_link("", "(루트)") == "https://www.dropbox.com/home"


# ---------------------------------------------------------------- output


def test_collect_updates_output_shape_order_and_time_format():
    entries = [
        FolderMetadata(name="논문A", path_lower=f"{ROOT}/논문a", path_display=f"{ROOT}/논문A", id="id:folder00"),
        fm(f"{ROOT}/논문A/sections/intro.tex", at(3, 9), modified_by=KIM),
        fm(f"{ROOT}/논문A/fig1.png", at(2, 15), modified_by=KIM),  # 00:00 next day in Seoul
        fm(f"{ROOT}/논문A/mine.tex", at(4), modified_by=ME),  # my own change
        fm(f"{ROOT}/논문A/old.tex", datetime(2026, 9, 20), modified_by=KIM),  # before the window
        fm(f"{ROOT}/논문A/private.tex", at(4), shared=False),  # outside a shared folder
        fm(f"{ROOT}/Paper-B/refs.bib", at(4, 1), modified_by=PARK),
        fm(f"{ROOT}/Paper-B/data/table.csv", at(3, 12), modified_by=KIM),
        fm(f"{ROOT}/notes.txt", at(2, 3), modified_by=None),  # shared, modifier unknown
    ]
    dbx = FakeDropbox(entries, names={KIM: "김공저", PARK: "박공저"})
    payload = collect_updates(dbx, ROOT, SINCE, SEOUL, name_cache={})

    assert payload == {
        "configured": True,
        "folder": ROOT,
        "since": "2026-10-01T09:00+09:00",
        "total_files": 5,
        # 8 files (the folder is not counted); old.tex is before the window.
        "stats": {
            "scanned": 8,
            "changed_in_window": 7,
            "excluded_mine": 1,
            "excluded_unknown_modifier": 1,
            "excluded_temp": 0,
        },
        "groups": [
            {
                "subfolder": "Paper-B",
                "link": ROOT_LINK + "/Paper-B",
                "by": [
                    {"name": "박공저", "files": [{"path": "refs.bib", "modified": "2026-10-04 10:00"}]},
                    {"name": "김공저", "files": [{"path": "data/table.csv", "modified": "2026-10-03 21:00"}]},
                ],
            },
            {
                "subfolder": "논문A",
                "link": ROOT_LINK + "/%EB%85%BC%EB%AC%B8A",
                "by": [
                    {
                        "name": "김공저",
                        "files": [
                            {"path": "sections/intro.tex", "modified": "2026-10-03 18:00"},
                            {"path": "fig1.png", "modified": "2026-10-03 00:00"},
                        ],
                    }
                ],
            },
            {
                "subfolder": "(루트)",
                "link": ROOT_LINK,
                "by": [{"name": "확인 불가", "files": [{"path": "notes.txt", "modified": "2026-10-02 12:00"}]}],
            },
        ],
        "omitted": 0,
    }
    assert set(all_keys(payload)) <= ALLOWED_KEYS
    # Display names are looked up once per account, and nothing but metadata is read.
    assert dbx.account_lookups == 2
    assert dbx.forbidden == []


def test_cap_lists_newest_files_and_counts_the_rest():
    def minute(m):
        return datetime(2026, 10, 2) + timedelta(minutes=m)

    entries = (
        # 15 oldest files: only counted.
        [fm(f"{ROOT}/논문A/old{i}.tex", minute(i), modified_by=PARK) for i in range(10)]
        + [fm(f"{ROOT}/Old/f{i}.txt", minute(10 + i), modified_by=KIM) for i in range(5)]
        # 60 newest files: listed.
        + [fm(f"{ROOT}/논문A/new{i}.tex", minute(100 + i), modified_by=KIM) for i in range(40)]
        + [fm(f"{ROOT}/Paper-B/b{i}.bib", minute(140 + i), modified_by=PARK) for i in range(20)]
    )
    dbx = FakeDropbox(entries, names={KIM: "김공저", PARK: "박공저"}, page_size=10)
    payload = collect_updates(dbx, ROOT, SINCE, SEOUL, name_cache={})

    assert MAX_LISTED_FILES == 60
    assert payload["total_files"] == 75
    assert payload["omitted"] == 15
    groups = {g["subfolder"]: g for g in payload["groups"]}
    assert [g["subfolder"] for g in payload["groups"]] == ["Paper-B", "논문A", "Old"]

    listed = [f for g in payload["groups"] for p in g["by"] for f in p["files"]]
    assert len(listed) == 60
    assert all(f["path"].startswith(("new", "b")) for f in listed)

    paper_b = groups["Paper-B"]
    assert "omitted" not in paper_b
    assert [(p["name"], len(p["files"]), p.get("omitted")) for p in paper_b["by"]] == [("박공저", 20, None)]
    assert paper_b["by"][0]["files"][0]["path"] == "b19.bib"  # newest first

    paper_a = groups["논문A"]
    assert paper_a["omitted"] == 10
    assert [(p["name"], len(p["files"]), p.get("omitted")) for p in paper_a["by"]] == [
        ("김공저", 40, None),
        ("박공저", 0, 10),
    ]

    old = groups["Old"]
    assert old["omitted"] == 5
    assert old["by"] == [{"name": "김공저", "files": [], "omitted": 5}]
    assert sum(g.get("omitted", 0) for g in payload["groups"]) == payload["omitted"]
    assert dbx.forbidden == []


# ---------------------------------------------------------------- run_check / config


def test_default_root_folder_when_unset_or_empty(tmp_path):
    for env in (env_with(), env_with(DROPBOX_ROOT_FOLDER=""), env_with(DROPBOX_ROOT_FOLDER="   ")):
        cfg = config.load_dropbox_config(env)
        assert cfg.configured and cfg.missing == []
        assert cfg.root_folder == "/20_연구-진행"

    dbx = FakeDropbox([fm(f"{ROOT}/논문A/a.tex", at(4), modified_by=KIM)], names={KIM: "김공저"})
    store = StateStore(tmp_path / "state.json")
    payload = run_check(env=env_with(), now=NOW, client_factory=lambda cfg: dbx, store=store)
    assert dbx.listed_paths == ["/20_연구-진행"]
    assert payload["folder"] == "/20_연구-진행"
    assert payload["groups"][0]["subfolder"] == "논문A"


@pytest.mark.parametrize("raw", ["20_연구-진행", "/20_연구-진행/", "20_연구-진행/"])
def test_configured_root_is_normalized(raw, tmp_path):
    dbx = FakeDropbox([fm(f"{ROOT}/논문A/a.tex", at(4), modified_by=KIM)], names={KIM: "김공저"})
    store = StateStore(tmp_path / "state.json")
    env = env_with(DROPBOX_ROOT_FOLDER=raw)
    payload = run_check(env=env, now=NOW, client_factory=lambda cfg: dbx, store=store)
    assert dbx.listed_paths == [ROOT]
    assert payload["folder"] == ROOT
    assert payload["groups"][0]["by"][0]["files"][0]["path"] == "a.tex"


def test_run_check_unconfigured_returns_hint_and_does_not_raise():
    payload = run_check()
    assert payload["configured"] is False
    assert payload["missing"] == ["DROPBOX_ACCESS_TOKEN"]
    assert "DROPBOX_ACCESS_TOKEN" in payload["hint"]
    assert "files.content.read" not in payload["hint"]
    assert "/20_연구-진행" in payload["hint"]
    assert "\n" not in payload["hint"]


def test_run_check_reports_missing_part_of_refresh_trio():
    payload = run_check(env={"DROPBOX_REFRESH_TOKEN": "r" * 20})
    assert payload["configured"] is False
    assert payload["missing"] == ["DROPBOX_APP_KEY", "DROPBOX_APP_SECRET"]


def test_tool_handler_unconfigured_returns_json():
    result = asyncio.run(check_dropbox_updates.handler({}))
    data = json.loads(result["content"][0]["text"])
    assert data["configured"] is False
    assert data["missing"] == ["DROPBOX_ACCESS_TOKEN"]


def test_tool_handler_returns_compact_list_only_json(monkeypatch):
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", TOKEN)
    dbx = FakeDropbox([fm(f"{ROOT}/논문A/a.tex", at(4), modified_by=KIM, size=123_456)], names={KIM: "김공저"})
    monkeypatch.setattr(dropbox_tool, "make_client", lambda cfg: dbx)
    monkeypatch.setattr(dropbox_tool, "utcnow", lambda: NOW)
    result = asyncio.run(check_dropbox_updates.handler({}))
    text = result["content"][0]["text"]
    data = json.loads(text)
    assert set(data) == {"configured", "folder", "since", "since_basis", "total_files", "stats", "groups", "omitted"}
    assert set(all_keys(data)) <= ALLOWED_KEYS
    assert data["since_basis"] == "default_24h"
    assert data["groups"][0]["by"] == [{"name": "김공저", "files": [{"path": "a.tex", "modified": "2026-10-04 18:00"}]}]
    assert "논문A" in text and "123456" not in text
    assert dbx.forbidden == []


CHECKPOINT = datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc)  # 10-02 09:00 in Seoul, 72 hours before NOW


def checkpoint_entries():
    return [
        fm(f"{ROOT}/논문A/fig.png", at(1, 12), modified_by=KIM),  # before the checkpoint
        fm(f"{ROOT}/논문A/fig2.png", at(3), modified_by=KIM),  # after the checkpoint, before the last 24 hours
        fm(f"{ROOT}/논문A/fig3.png", at(4, 6), modified_by=KIM),  # in the last 24 hours
    ]


def test_ad_hoc_run_uses_the_last_24_hours_and_never_writes_state(tmp_path):
    state_file = tmp_path / "state.json"
    dbx = FakeDropbox(checkpoint_entries(), names={KIM: "김공저"})
    payload = run_check(env=env_with(), now=NOW, client_factory=lambda cfg: dbx, store=StateStore(state_file))
    assert payload["since_basis"] == "default_24h" and payload["since"] == "2026-10-04T09:00+09:00"
    assert [f["path"] for g in payload["groups"] for p in g["by"] for f in p["files"]] == ["fig3.png"]
    assert not state_file.exists()  # nothing written, not even a file

    # A stored briefing checkpoint is ignored and left alone.
    store = StateStore(state_file)
    store.mark_checked("dropbox", CHECKPOINT)
    payload = run_check(env=env_with(), now=NOW, client_factory=lambda cfg: dbx, store=store)
    assert payload["since_basis"] == "default_24h" and payload["total_files"] == 1
    assert store.last_checked("dropbox") == CHECKPOINT


def test_briefing_run_uses_the_checkpoint_and_moves_it(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.mark_checked("dropbox", CHECKPOINT)
    dbx = FakeDropbox(checkpoint_entries(), names={KIM: "김공저"})
    payload = run_check(env=env_with(), now=NOW, client_factory=lambda cfg: dbx, store=store, briefing=True)
    assert payload["since_basis"] == "briefing_checkpoint" and payload["since"] == "2026-10-02T09:00+09:00"
    assert payload["total_files"] == 2
    assert store.last_checked("dropbox") == NOW

    # The next briefing starts where this one stopped.
    again = run_check(env=env_with(), now=NOW, client_factory=lambda cfg: dbx, store=store, briefing=True)
    assert again["since_basis"] == "briefing_checkpoint" and again["total_files"] == 0


def test_first_briefing_falls_back_to_lookback_days(tmp_path):
    store = StateStore(tmp_path / "state.json")
    dbx = FakeDropbox(checkpoint_entries(), names={KIM: "김공저"})
    env = env_with(LOOKBACK_DAYS="3")
    payload = run_check(env=env, now=NOW, client_factory=lambda cfg: dbx, store=store, briefing=True)
    assert payload["since_basis"] == "lookback_default" and payload["since"] == "2026-10-02T09:00+09:00"
    assert store.last_checked("dropbox") == NOW


def test_since_hours_overrides_and_never_moves_the_checkpoint(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.mark_checked("dropbox", CHECKPOINT)
    dbx = FakeDropbox(checkpoint_entries(), names={KIM: "김공저"})
    for briefing in (False, True):
        payload = run_check(
            since_hours=24 * 7, env=env_with(), now=NOW, client_factory=lambda cfg: dbx, store=store, briefing=briefing
        )
        assert payload["since_basis"] == "since_hours" and payload["since"] == "2026-09-28T09:00+09:00"
        assert payload["total_files"] == 3
        assert store.last_checked("dropbox") == CHECKPOINT


def test_failed_briefing_check_keeps_the_checkpoint(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.mark_checked("dropbox", CHECKPOINT)

    def boom(cfg):
        raise RuntimeError("network down")

    payload = run_check(env=env_with(), now=NOW, client_factory=boom, store=store, briefing=True)
    assert payload["ok"] is False
    assert store.last_checked("dropbox") == CHECKPOINT


def test_run_check_reports_since_basis_and_stats(tmp_path):
    entries = [
        fm(f"{ROOT}/논문A/mine.tex", at(4), modified_by=ME),
        fm(f"{ROOT}/개인/notes.txt", at(4), shared=False),
        fm(f"{ROOT}/논문A/~$draft.docx", at(4), modified_by=KIM),
        fm(f"{ROOT}/논문A/old.tex", datetime(2026, 9, 1), modified_by=KIM),
    ]
    store = StateStore(tmp_path / "state.json")

    def check(**kwargs):
        dbx = FakeDropbox(entries, names={KIM: "김공저"})
        return run_check(env=env_with(), now=NOW, client_factory=lambda cfg: dbx, store=store, **kwargs)

    ad_hoc = check()
    assert ad_hoc["since_basis"] == "default_24h" and ad_hoc["total_files"] == 0 and ad_hoc["groups"] == []
    assert ad_hoc["stats"] == {
        "scanned": 4,
        "changed_in_window": 2,
        "excluded_mine": 1,
        "excluded_unknown_modifier": 1,
        "excluded_temp": 1,
    }

    first = check(briefing=True)  # no checkpoint yet: LOOKBACK_DAYS (default 1 day)
    assert first["since_basis"] == "lookback_default" and first["since"] == "2026-10-04T09:00+09:00"
    second = check(briefing=True)  # the first briefing moved the checkpoint to NOW
    assert second["since_basis"] == "briefing_checkpoint" and second["since"] == "2026-10-05T09:00+09:00"
    assert second["stats"] == {
        "scanned": 4,
        "changed_in_window": 0,
        "excluded_mine": 0,
        "excluded_unknown_modifier": 0,
        "excluded_temp": 1,
    }

    asked = check(since_hours=24 * 90)
    assert asked["since_basis"] == "since_hours" and asked["since"] == "2026-07-07T09:00+09:00"
    assert asked["stats"]["changed_in_window"] == 3 and asked["total_files"] == 1


def test_run_check_error_is_scrubbed_and_state_untouched(tmp_path):
    token = "sl.secret-token-value-0123456789abcdef"
    env = {"DROPBOX_ACCESS_TOKEN": token, "DROPBOX_ROOT_FOLDER": ROOT}
    store = StateStore(tmp_path / "state.json")

    def boom(cfg):
        raise RuntimeError(f"401 Unauthorized: invalid token {cfg.access_token}")

    payload = run_check(env=env, client_factory=boom, store=store)
    assert payload["ok"] is False
    assert payload["folder"] == ROOT
    assert token not in json.dumps(payload, ensure_ascii=False)
    assert "Dropbox" in payload["error"]
    assert store.last_checked("dropbox") is None


def test_make_client_prefers_refresh_token_trio():
    cfg = dropbox_tool.config.load_dropbox_config(
        {
            "DROPBOX_ACCESS_TOKEN": "a" * 20,
            "DROPBOX_REFRESH_TOKEN": "r" * 20,
            "DROPBOX_APP_KEY": "k" * 10,
            "DROPBOX_APP_SECRET": "s" * 10,
            "DROPBOX_ROOT_FOLDER": ROOT,
        }
    )
    client = dropbox_tool.make_client(cfg)
    assert client._oauth2_refresh_token == "r" * 20  # no network call is made here


# ---------------------------------------------------------------- --dropbox-check

LAST_CHECK = datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc)  # 10-03 09:00 in Seoul: the briefing checkpoint, NOW - 48h


def diagnosis_entries():
    return [
        FolderMetadata(name="논문A", path_lower=f"{ROOT}/논문a", path_display=f"{ROOT}/논문A", id="id:folder00"),
        fm(f"{ROOT}/논문A/~$intro.docx", at(4, 4), modified_by=KIM),  # 10-04 13:00 Seoul, Word owner file
        fm(f"{ROOT}/논문A/intro.tex", at(4, 1), modified_by=KIM),  # 10-04 10:00 Seoul
        fm(f"{ROOT}/논문A/mine.tex", at(4, 2), modified_by=ME),  # 10-04 11:00
        fm(f"{ROOT}/개인/notes.txt", at(4, 3), shared=False),  # 10-04 12:00
        fm(f"{ROOT}/Paper-B/refs.bib", at(3, 5), modified_by=None),  # 10-03 14:00, shared, modifier unknown
        fm(f"{ROOT}/논문A/old.tex", at(2, 9), modified_by=PARK),  # 10-02 18:00, before the checkpoint
        fm(f"{ROOT}/.DS_Store", at(2, 1), shared=False),  # 10-02 10:00
        *[fm(f"{ROOT}/Archive/a{i}.txt", at(1, i), modified_by=KIM) for i in range(1, 9)],  # 10-01 10:00..17:00
    ]


def run_diagnosis(entries, tmp_path, *, hours=None, last_check=LAST_CHECK, env=None, dbx=None):
    store = StateStore(tmp_path / "state.json")
    if last_check is not None:
        store.mark_checked("dropbox", last_check)
    dbx = dbx or FakeDropbox(entries, names={KIM: "김공저", PARK: "박공저"})
    out = io.StringIO()
    code = run_dropbox_check(
        env or env_with(), hours=hours, now=NOW, client_factory=lambda cfg: dbx, store=store, out=out
    )
    return code, out.getvalue(), store


def table_after(text, title):
    """Cells of the table printed under the line starting with ``title`` (header row first)."""
    lines = text.split(title, 1)[1].split("\n\n", 1)[0].splitlines()[1:]
    return [tuple(cell.strip() for cell in line.split(" | ")) for line in lines]


HEADER = ("경로", "수정 시각", "수정한 사람", "공유 폴더", "판정")
TEMP_ROW = ("논문A/~$intro.docx", "10-04 13:00", "김공저", "예", "제외: 임시 파일")


def test_dropbox_check_explains_every_file_without_moving_the_checkpoint(tmp_path):
    state_file = tmp_path / "state.json"
    code, out, store = run_diagnosis(diagnosis_entries(), tmp_path, hours=48)
    assert code == 0
    assert out.startswith("Dropbox 변경 확인 진단 (읽기 전용")
    assert "브리핑 기준 시각도 바꾸지 않습니다" in out.splitlines()[0]
    for line in (
        "확인할 폴더: /20_연구-진행 (기본값)",
        "폴더: 있음",
        "계정: 나연구",
        "기간: 2026-10-03 09:00 (Asia/Seoul) 이후 — 기준: --hours 48 (최근 48시간)",
        "훑어본 파일: 15개 (하위 폴더 포함)",
        "  - 제외: 임시 파일: 2개 (~$·.~lock.로 시작하는 파일, .DS_Store, *.tmp 등. 기간과 상관없이 먼저 뺌)",
        "기간 안에 바뀐 파일: 4개",
        "  - 포함: 2개 (업뎃이 알려 주는 파일)",
        "  - 제외: 박사님이 수정하신 파일: 1개",
        "  - 제외: 수정자 정보 없음(공유 폴더 아님): 1개",
    ):
        assert line + "\n" in out
    assert "\n브리핑 기준 시각:" not in out  # only without --hours

    changed = [
        ("개인/notes.txt", "10-04 12:00", "(정보 없음)", "아니오", "제외: 수정자 정보 없음(공유 폴더 아님)"),
        ("논문A/mine.tex", "10-04 11:00", "나연구", "예", "제외: 박사님이 수정하신 파일"),
        ("논문A/intro.tex", "10-04 10:00", "김공저", "예", "포함"),
        ("Paper-B/refs.bib", "10-03 14:00", "(정보 없음)", "예", "포함"),
    ]
    # The temporary file changed in the window, so it is listed with its own label.
    assert table_after(out, "기간 안에 바뀐 파일 (최근 수정 순") == [HEADER, TEMP_ROW, *changed]

    recent = table_after(out, "기간과 상관없이 가장 최근에 바뀐 파일 10개")
    assert recent[0] == HEADER and len(recent) == 11
    assert recent[1:6] == [TEMP_ROW, *changed]
    assert recent[6] == ("논문A/old.tex", "10-02 18:00", "박공저", "예", "제외: 기간 이전 (기준 시각 이전)")
    assert recent[7] == (".DS_Store", "10-02 10:00", "(정보 없음)", "아니오", "제외: 임시 파일")
    assert [row[0] for row in recent[8:]] == [f"Archive/a{i}.txt" for i in (8, 7, 6)]

    assert out.rstrip().splitlines()[-1].startswith("참고: 삭제·이동·이름 바꾸기는 감지하지 않습니다.")
    # Read-only: the checkpoint is where it was, and no token is printed.
    assert store.last_checked("dropbox") == LAST_CHECK
    assert json.loads(state_file.read_text(encoding="utf-8")) == {"last_checked": {"dropbox": LAST_CHECK.isoformat()}}
    assert TOKEN not in out


def test_dropbox_check_without_hours_shows_24h_and_the_briefing_checkpoint(tmp_path):
    state_file = tmp_path / "state.json"
    code, out, store = run_diagnosis(diagnosis_entries(), tmp_path)
    assert code == 0
    lines = out.splitlines()
    period = lines.index("기간: 2026-10-04 09:00 (Asia/Seoul) 이후 — 기준: 최근 24시간 (기간 없이 업뎃·고뭉치에게 물을 때와 같음)")
    assert lines[period + 1 : period + 3] == [
        "브리핑 기준 시각: 2026-10-03 09:00 (Asia/Seoul) 이후 — --brief가 Dropbox를 마지막으로 확인한 때 "
        "(이 시각은 브리핑만 바꿉니다)",
        "  - 지금 브리핑하면 업뎃이 알려 줄 파일: 2개",  # intro.tex and refs.bib
    ]
    assert "기간 안에 바뀐 파일: 3개" in lines and "  - 포함: 1개 (업뎃이 알려 주는 파일)" in lines
    assert [row[0] for row in table_after(out, "기간 안에 바뀐 파일 (최근 수정 순")[1:]] == [
        "논문A/~$intro.docx",
        "개인/notes.txt",
        "논문A/mine.tex",
        "논문A/intro.tex",
    ]
    assert store.last_checked("dropbox") == LAST_CHECK
    assert json.loads(state_file.read_text(encoding="utf-8")) == {"last_checked": {"dropbox": LAST_CHECK.isoformat()}}

    fresh = tmp_path / "fresh"
    code, out, _store = run_diagnosis(diagnosis_entries(), fresh, last_check=None, env=env_with(LOOKBACK_DAYS="3"))
    assert code == 0
    assert (
        "브리핑 기준 시각: 기록 없음 — 다음 브리핑은 최근 3일(LOOKBACK_DAYS)을 봅니다 (이 시각은 브리핑만 바꿉니다)\n"
        "  - 지금 브리핑하면 업뎃이 알려 줄 파일: 3개\n"  # old.tex (10-02 18:00) is inside 3 days
    ) in out
    assert not (fresh / "state.json").exists()  # nothing written


def test_dropbox_check_matches_what_the_tool_reports(tmp_path):
    entries = diagnosis_entries()

    def reported(payload):
        return {f"{g['subfolder']}/{f['path']}" for g in payload["groups"] for p in g["by"] for f in p["files"]}

    def included(out):
        return {row[0] for row in table_after(out, "기간 안에 바뀐 파일 (최근 수정 순")[1:] if row[4] == "포함"}

    # Without --hours: what 업뎃 reports when asked without a period (last 24 hours).
    _code, out, _store = run_diagnosis(entries, tmp_path / "a")
    dbx = FakeDropbox(entries, names={KIM: "김공저", PARK: "박공저"})
    payload = run_check(env=env_with(), now=NOW, client_factory=lambda cfg: dbx, store=StateStore(tmp_path / "x.json"))
    assert reported(payload) == included(out) == {"논문A/intro.tex"}

    # --hours 48 covers the same window as a briefing from the 48-hour-old checkpoint.
    _code, out, _store = run_diagnosis(entries, tmp_path / "b", hours=48)
    tool_store = StateStore(tmp_path / "tool-state.json")
    tool_store.mark_checked("dropbox", LAST_CHECK)
    dbx = FakeDropbox(entries, names={KIM: "김공저", PARK: "박공저"})
    payload = run_check(env=env_with(), now=NOW, client_factory=lambda cfg: dbx, store=tool_store, briefing=True)
    assert reported(payload) == included(out) == {"논문A/intro.tex", "Paper-B/refs.bib"}
    stats = payload["stats"]
    assert f"훑어본 파일: {stats['scanned']}개" in out
    assert f"기간 안에 바뀐 파일: {stats['changed_in_window']}개" in out
    assert f"제외: 박사님이 수정하신 파일: {stats['excluded_mine']}개" in out
    assert f"제외: 임시 파일: {stats['excluded_temp']}개" in out


def test_dropbox_check_window_from_hours(tmp_path):
    code, out, store = run_diagnosis(diagnosis_entries(), tmp_path, hours=72)
    assert code == 0
    assert "기간: 2026-10-02 09:00 (Asia/Seoul) 이후 — 기준: --hours 72 (최근 72시간)" in out
    assert "기간 안에 바뀐 파일: 5개" in out  # old.tex (10-02 18:00) is now inside; .DS_Store (10-02 10:00) is not counted
    assert store.last_checked("dropbox") == LAST_CHECK


class MissingFolderDropbox(FakeDropbox):
    def files_list_folder(self, path, recursive=False):
        raise ApiError("req-1", ListFolderError.path(LookupError.not_found), None, None)


def test_dropbox_check_folder_not_found_hint(tmp_path):
    env = env_with(DROPBOX_ROOT_FOLDER="20_연구-진행/")
    code, out, store = run_diagnosis([], tmp_path, env=env, dbx=MissingFolderDropbox([]))
    assert code == 1
    assert "확인할 폴더: /20_연구-진행 (DROPBOX_ROOT_FOLDER)" in out
    assert "폴더: 찾을 수 없음" in out
    assert "전체 경로" in out and "팀 스페이스" in out
    assert "계정: 나연구" in out
    assert "훑어본 파일" not in out
    assert store.last_checked("dropbox") == LAST_CHECK
    assert TOKEN not in out


def test_dropbox_check_unconfigured_or_failing_never_prints_tokens(tmp_path):
    out = io.StringIO()
    assert run_dropbox_check({}, now=NOW, out=out) == 1
    assert "[오류] Dropbox 설정 누락: DROPBOX_ACCESS_TOKEN" in out.getvalue()

    def boom(cfg):
        raise RuntimeError(f"401 Unauthorized: invalid token {cfg.access_token}")

    out = io.StringIO()
    store = StateStore(tmp_path / "state.json")
    assert run_dropbox_check(env_with(), now=NOW, client_factory=boom, store=store, out=out) == 1
    assert "[오류] Dropbox 확인 실패" in out.getvalue()
    assert TOKEN not in out.getvalue()
    assert store.last_checked("dropbox") is None


def test_cli_dropbox_check_dispatch_help_and_conflicts(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(dropbox_check, "run_dropbox_check", lambda hours=None: calls.append(hours) or 0)
    assert main(["--dropbox-check"]) == 0
    assert main(["--dropbox-check", "--hours", "72"]) == 0
    assert calls == [None, 72]

    help_text = build_parser().format_help()
    assert "--dropbox-check" in help_text and "--hours N" in help_text
    assert "python -m mungchi --dropbox-check --hours 72" in help_text

    for argv in (
        ["--dropbox-check", "질문"],
        ["--dropbox-check", "--brief"],
        ["--dropbox-check", "--agent", "update"],
        ["--dropbox-check", "--calendar-setup"],
        ["--hours", "72"],
        ["--dropbox-check", "--hours", "0"],
    ):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--dropbox-check는 질문이나 다른 옵션" in err
    assert "--hours는 --dropbox-check와 함께 써야 합니다" in err
    assert "--hours에는 1 이상의 정수" in err


# ---------------------------------------------------------------- temporary and lock files


@pytest.mark.parametrize(
    "name",
    [
        "~$draft.docx",
        "~$슬라이드.pptx",
        "~WRL0001.tmp",
        "~draft.tex",
        ".~lock.data.xlsx#",
        ".DS_Store",
        ".ds_store",
        "Thumbs.db",
        "desktop.ini",
        "Desktop.INI",
        "Icon\r",
        "analysis.do.stswp",
        "model.STSWP",
        "upload.tmp",
        "cache.temp",
        ".main.tex.swp",
        ".main.tex.swo",
    ],
)
def test_temp_and_lock_files_are_recognised(name):
    assert is_temp_file(name)


@pytest.mark.parametrize(
    "name",
    ["draft.docx", "main.tex", "data.xlsx", "Icon.png", "temperature.csv", "tmp_results.csv", "notes~.txt", "a.swap", ""],
)
def test_ordinary_files_are_not_temp_files(name):
    assert not is_temp_file(name)


def test_temp_patterns_live_in_one_constant():
    assert TEMP_FILE_PATTERNS == (
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


def test_temp_files_are_excluded_before_classification_and_counted():
    entries = [
        fm(f"{ROOT}/논문A/main.tex", at(4), modified_by=KIM),
        fm(f"{ROOT}/논문A/~$main.docx", at(4, 1), modified_by=KIM),  # in the window, by a co-author
        fm(f"{ROOT}/논문A/.~lock.data.xlsx#", at(4, 2), modified_by=PARK),
        fm(f"{ROOT}/분석/model.do.stswp", at(4, 3), modified_by=ME),
        fm(f"{ROOT}/.DS_Store", datetime(2026, 9, 1), shared=False),  # long before the window
    ]
    assert classify_entry(entries[1], ME, SINCE) == EXCLUDED_TEMP
    assert classify_entry(entries[4], ME, SINCE) == EXCLUDED_TEMP  # temp wins over "before the window"
    dbx = FakeDropbox(entries, names={KIM: "김공저", PARK: "박공저"})
    payload = collect_updates(dbx, ROOT, SINCE, SEOUL, name_cache={})
    assert payload["total_files"] == 1
    assert [f["path"] for g in payload["groups"] for p in g["by"] for f in p["files"]] == ["main.tex"]
    assert payload["stats"] == {
        "scanned": 5,
        "changed_in_window": 1,
        "excluded_mine": 0,
        "excluded_unknown_modifier": 0,
        "excluded_temp": 4,
    }


# ---------------------------------------------------------------- briefing mode is bound per run


def test_concurrent_briefing_and_ad_hoc_runs_keep_their_own_modes(tmp_path, monkeypatch):
    """Tools run in-process: two runs at once must not share a briefing flag."""
    state_file = tmp_path / "state.json"
    StateStore(state_file).mark_checked("dropbox", CHECKPOINT)
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("MUNGCHI_STATE_FILE", str(state_file))
    monkeypatch.setattr(dropbox_tool, "utcnow", lambda: NOW)
    both_listing = threading.Barrier(2, timeout=5)

    class OverlappingDropbox(FakeDropbox):
        def files_list_folder(self, path, recursive=False):
            both_listing.wait()  # each run waits until the other one is in flight too
            return super().files_list_folder(path, recursive=recursive)

    monkeypatch.setattr(
        dropbox_tool, "make_client", lambda cfg: OverlappingDropbox(checkpoint_entries(), names={KIM: "김공저"})
    )
    briefing_tool = make_check_dropbox_updates(briefing=True)
    ad_hoc_tool = make_check_dropbox_updates()

    async def both():
        return await asyncio.gather(briefing_tool.handler({}), ad_hoc_tool.handler({}))

    briefing_result, ad_hoc_result = (json.loads(r["content"][0]["text"]) for r in asyncio.run(both()))
    assert briefing_result["since_basis"] == "briefing_checkpoint" and briefing_result["total_files"] == 2
    assert ad_hoc_result["since_basis"] == "default_24h" and ad_hoc_result["total_files"] == 1
    # Only the briefing run moved the checkpoint.
    assert StateStore(state_file).last_checked("dropbox") == NOW

    # A model cannot ask for briefing mode: it is not a tool argument.
    assert set(briefing_tool.input_schema["properties"]) == {"since_hours"}
    assert briefing_tool.name == ad_hoc_tool.name == check_dropbox_updates.name == "check_dropbox_updates"
