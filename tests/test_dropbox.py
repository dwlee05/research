from __future__ import annotations

import asyncio
import json
import unicodedata
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from dropbox.files import FileMetadata, FileSharingInfo, FolderMetadata

from mungchi import config
from mungchi.state import StateStore
from mungchi.tools import dropbox_tool
from mungchi.tools.dropbox_tool import (
    MAX_LISTED_FILES,
    UNKNOWN_MODIFIER,
    check_dropbox_updates,
    classify_modifier,
    collect_updates,
    folder_link,
    normalize_root,
    relative_path,
    run_check,
    split_group,
)

ME = "dbid:" + "A" * 35
KIM = "dbid:" + "B" * 35
PARK = "dbid:" + "C" * 35
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
    "configured", "folder", "since", "total_files", "groups", "omitted",
    "subfolder", "link", "by", "name", "files", "path", "modified",
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
        return SimpleNamespace(account_id=ME)

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

    dbx = FakeDropbox([fm(f"{ROOT}/논문A/a.tex", at(3), modified_by=KIM)], names={KIM: "김공저"})
    store = StateStore(tmp_path / "state.json")
    payload = run_check(env=env_with(), now=NOW, client_factory=lambda cfg: dbx, store=store)
    assert dbx.listed_paths == ["/20_연구-진행"]
    assert payload["folder"] == "/20_연구-진행"
    assert payload["groups"][0]["subfolder"] == "논문A"


@pytest.mark.parametrize("raw", ["20_연구-진행", "/20_연구-진행/", "20_연구-진행/"])
def test_configured_root_is_normalized(raw, tmp_path):
    dbx = FakeDropbox([fm(f"{ROOT}/논문A/a.tex", at(3), modified_by=KIM)], names={KIM: "김공저"})
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
    dbx = FakeDropbox([fm(f"{ROOT}/논문A/a.tex", at(3), modified_by=KIM, size=123_456)], names={KIM: "김공저"})
    monkeypatch.setattr(dropbox_tool, "make_client", lambda cfg: dbx)
    monkeypatch.setattr(dropbox_tool, "utcnow", lambda: NOW)
    result = asyncio.run(check_dropbox_updates.handler({}))
    text = result["content"][0]["text"]
    data = json.loads(text)
    assert set(data) == {"configured", "folder", "since", "total_files", "groups", "omitted"}
    assert set(all_keys(data)) <= ALLOWED_KEYS
    assert data["groups"][0]["by"] == [{"name": "김공저", "files": [{"path": "a.tex", "modified": "2026-10-03 18:00"}]}]
    assert "논문A" in text and "123456" not in text
    assert dbx.forbidden == []


def test_run_check_uses_and_updates_state(tmp_path):
    store = StateStore(tmp_path / "state.json")
    last = datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc)
    store.mark_checked("dropbox", last)
    entries = [
        fm(f"{ROOT}/논문A/fig.png", at(2), modified_by=KIM),  # before last check
        fm(f"{ROOT}/논문A/fig2.png", at(4), modified_by=KIM),
    ]
    dbx = FakeDropbox(entries, names={KIM: "김공저"})
    payload = run_check(env=env_with(), now=NOW, client_factory=lambda cfg: dbx, store=store)
    assert payload["total_files"] == 1
    assert payload["since"] == "2026-10-03T09:00+09:00"
    assert store.last_checked("dropbox") == NOW


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
