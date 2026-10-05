from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from dropbox.files import FileMetadata, FileSharingInfo, FolderMetadata

from mungchi.state import StateStore
from mungchi.tools import dropbox_tool
from mungchi.tools.dropbox_tool import (
    UNKNOWN_MODIFIER,
    check_dropbox_updates,
    classify_modifier,
    collect_updates,
    normalize_root,
    run_check,
    top_level_group,
)

ME = "dbid:" + "A" * 35
KIM = "dbid:" + "B" * 35
PARK = "dbid:" + "C" * 35
ROOT = "/Research/Papers"
SINCE = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
SEOUL = ZoneInfo("Asia/Seoul")
_rev_counter = iter(range(0x100000000, 0x1FFFFFFFF))


def rev() -> str:
    return f"{next(_rev_counter):09x}"


def fm(path, modified, *, modified_by=None, shared=True, size=100, rev_id=None):
    """A real Dropbox FileMetadata; ``server_modified`` is naive UTC like the API."""
    sharing = None
    if shared:
        sharing = FileSharingInfo(read_only=False, parent_shared_folder_id="1234", modified_by=modified_by)
    return FileMetadata(
        name=path.rsplit("/", 1)[-1],
        id="id:" + "x" * 10,
        client_modified=modified,
        server_modified=modified,
        rev=rev_id or rev(),
        size=size,
        path_lower=path.lower(),
        path_display=path,
        sharing_info=sharing,
    )


class FakeResponse:
    def __init__(self, content: bytes):
        self.content = content
        self.closed = False

    def close(self):
        self.closed = True


class FakeDropbox:
    def __init__(self, entries, revisions=None, contents=None, names=None, page_size=2):
        self.entries = entries
        self.revisions = revisions or {}
        self.contents = contents or {}
        self.names = names or {}
        self.page_size = page_size
        self.account_lookups = 0
        self.downloads: list[str] = []

    def users_get_current_account(self):
        return SimpleNamespace(account_id=ME)

    def _page(self, start):
        chunk = self.entries[start : start + self.page_size]
        more = start + self.page_size < len(self.entries)
        return SimpleNamespace(entries=chunk, has_more=more, cursor=str(start + self.page_size))

    def files_list_folder(self, path, recursive=False):
        assert path == ROOT and recursive is True
        return self._page(0)

    def files_list_folder_continue(self, cursor):
        return self._page(int(cursor))

    def files_list_revisions(self, path, limit=10):
        assert limit == 2
        return SimpleNamespace(entries=self.revisions.get(path, []))

    def files_download(self, path):
        assert path.startswith("rev:")
        self.downloads.append(path)
        return None, FakeResponse(self.contents[path[4:]])

    def users_get_account(self, account_id):
        self.account_lookups += 1
        return SimpleNamespace(name=SimpleNamespace(display_name=self.names[account_id]))


def at(day, hour=9):
    return datetime(2026, 10, day, hour, 0)  # naive UTC, as returned by Dropbox


def test_classify_modifier_excludes_me_and_unshared_files():
    assert classify_modifier(fm("/r/a.tex", at(2), modified_by=KIM), ME) == KIM
    assert classify_modifier(fm("/r/a.tex", at(2), modified_by=ME), ME) is None
    assert classify_modifier(fm("/r/a.tex", at(2), modified_by=None), ME) == UNKNOWN_MODIFIER
    assert classify_modifier(fm("/r/a.tex", at(2), shared=False), ME) is None


def test_normalize_root_and_grouping():
    assert normalize_root("") == ""
    assert normalize_root("/") == ""
    assert normalize_root("Research/Papers/") == "/Research/Papers"
    assert top_level_group("PaperA/sections/intro.tex") == "PaperA"
    assert top_level_group("README.md") == "(루트)"


def test_collect_updates_groups_by_subfolder_and_coauthor_and_diffs():
    old_rev, new_rev = rev(), rev()
    intro = fm(f"{ROOT}/PaperA/intro.tex", at(3), modified_by=KIM, rev_id=new_rev)
    previous = fm(f"{ROOT}/PaperA/intro.tex", at(1), modified_by=ME, rev_id=old_rev)
    new_bib_rev = rev()
    new_bib = fm(f"{ROOT}/PaperB/refs.bib", at(4), modified_by=PARK, rev_id=new_bib_rev)
    entries = [
        FolderMetadata(name="PaperA", path_lower=f"{ROOT}/papera".lower(), path_display=f"{ROOT}/PaperA", id="id:folder00"),
        intro,
        fm(f"{ROOT}/PaperA/mine.tex", at(3), modified_by=ME),  # my own change
        fm(f"{ROOT}/PaperA/old.tex", datetime(2026, 9, 20), modified_by=KIM),  # before window
        fm(f"{ROOT}/PaperA/private.tex", at(3), shared=False),  # outside a shared folder
        fm(f"{ROOT}/PaperA/fig1.png", at(2), modified_by=KIM),  # binary
        new_bib,
        fm(f"{ROOT}/notes.txt", at(2), modified_by=None, size=10_000_000),  # unknown modifier, too big
    ]
    dbx = FakeDropbox(
        entries,
        revisions={
            intro.path_lower: [intro, previous],
            new_bib.path_lower: [new_bib],
        },
        contents={
            old_rev: "서론 첫 문단\n두 번째 문단\n".encode(),
            new_rev: "서론 첫 문단\n새로 쓴 두 번째 문단\n".encode(),
            new_bib_rev: b"@article{kim2026,\n  title={A}\n}\n",
        },
        names={KIM: "김공저", PARK: "박공저"},
    )
    payload = collect_updates(dbx, ROOT, SINCE, SEOUL, name_cache={})

    assert payload["changed_file_count"] == 4
    folders = {f["folder"]: f for f in payload["folders"]}
    assert set(folders) == {"PaperA", "PaperB", "(루트)"}

    paper_a = {c["name"]: c["files"] for c in folders["PaperA"]["coauthors"]}
    assert set(paper_a) == {"김공저"}
    files = {f["path"]: f for f in paper_a["김공저"]}
    assert set(files) == {"PaperA/intro.tex", "PaperA/fig1.png"}
    assert "+새로 쓴 두 번째 문단" in files["PaperA/intro.tex"]["diff"]
    assert "-두 번째 문단" in files["PaperA/intro.tex"]["diff"]
    assert files["PaperA/intro.tex"]["modified"].endswith("+09:00")
    assert "diff" not in files["PaperA/fig1.png"]

    bib = folders["PaperB"]["coauthors"][0]
    assert bib["name"] == "박공저"
    assert bib["files"][0]["new_file"] is True
    assert "+@article{kim2026," in bib["files"][0]["diff"]

    root_files = folders["(루트)"]["coauthors"][0]
    assert root_files["name"] == "수정자 미상"
    assert root_files["files"][0]["diff"] is None
    assert "200KB" in root_files["files"][0]["diff_note"]

    # Display names are looked up once per account.
    assert dbx.account_lookups == 2


def test_run_check_unconfigured_returns_hint_and_does_not_raise():
    payload = run_check()
    assert payload["configured"] is False
    assert payload["missing"] == ["DROPBOX_ACCESS_TOKEN", "DROPBOX_ROOT_FOLDER"]
    assert "DROPBOX_ACCESS_TOKEN" in payload["hint"]
    assert "\n" not in payload["hint"]


def test_run_check_reports_missing_part_of_refresh_trio():
    payload = run_check(env={"DROPBOX_REFRESH_TOKEN": "r" * 20, "DROPBOX_ROOT_FOLDER": ROOT})
    assert payload["configured"] is False
    assert payload["missing"] == ["DROPBOX_APP_KEY", "DROPBOX_APP_SECRET"]


def test_tool_handler_unconfigured_returns_json():
    result = asyncio.run(check_dropbox_updates.handler({}))
    data = json.loads(result["content"][0]["text"])
    assert data["configured"] is False
    assert "DROPBOX_ROOT_FOLDER" in data["missing"]


def test_run_check_uses_and_updates_state(tmp_path):
    env = {"DROPBOX_ACCESS_TOKEN": "sl.fake-token-value-0123456789abcdef", "DROPBOX_ROOT_FOLDER": ROOT}
    store = StateStore(tmp_path / "state.json")
    last = datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc)
    store.mark_checked("dropbox", last)
    now = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
    entries = [
        fm(f"{ROOT}/PaperA/fig.png", at(2), modified_by=KIM),  # before last check
        fm(f"{ROOT}/PaperA/fig2.png", at(4), modified_by=KIM),
    ]
    dbx = FakeDropbox(entries, names={KIM: "김공저"})
    payload = run_check(env=env, now=now, client_factory=lambda cfg: dbx, store=store)
    assert payload["ok"] is True
    assert payload["changed_file_count"] == 1
    assert payload["window"]["basis"] == "마지막 확인 시각 이후"
    assert store.last_checked("dropbox") == now


def test_run_check_error_is_scrubbed_and_state_untouched(tmp_path):
    token = "sl.secret-token-value-0123456789abcdef"
    env = {"DROPBOX_ACCESS_TOKEN": token, "DROPBOX_ROOT_FOLDER": ROOT}
    store = StateStore(tmp_path / "state.json")

    def boom(cfg):
        raise RuntimeError(f"401 Unauthorized: invalid token {cfg.access_token}")

    payload = run_check(env=env, client_factory=boom, store=store)
    assert payload["ok"] is False
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
