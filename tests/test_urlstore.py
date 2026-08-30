from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sync_to_readwise.core.urlstore import (
    CURSOR_OVERLAP,
    STORE_FILENAME,
    Document,
    UrlStore,
    url_key,
)


class TestMembership:
    def test_add_and_contains(self) -> None:
        store = UrlStore()
        assert not store.contains("https://a.example/1")
        store.add(Document(url="https://a.example/1"))
        assert store.contains("https://a.example/1")
        store.close()

    def test_add_many_counts_only_new(self) -> None:
        store = UrlStore()
        assert store.add_many([Document(url="https://a.example/1")]) == 1
        # Re-adding a known URL is an upsert, not a new document.
        assert (
            store.add_many(
                [Document(url="https://a.example/1"), Document(url="https://a.example/2")]
            )
            == 1
        )
        assert len(store) == 2
        store.close()

    def test_blank_urls_are_skipped(self) -> None:
        store = UrlStore()
        assert store.add_many([Document(url="   "), Document(url="")]) == 0
        assert len(store) == 0
        store.close()

    def test_key_ignores_surrounding_whitespace(self) -> None:
        assert url_key("  https://a.example/1  ") == "https://a.example/1"


class TestPersistence:
    def test_survives_reopen(self, tmp_path: Path) -> None:
        """The whole point: a restart must not start from an empty cache."""
        path = tmp_path / STORE_FILENAME
        first = UrlStore(path)
        first.add(Document(url="https://a.example/1", readwise_id="doc1"))
        first.close()

        second = UrlStore(path)
        assert second.contains("https://a.example/1")
        second.close()

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "dir" / STORE_FILENAME
        store = UrlStore(path)
        store.add(Document(url="https://a.example/1"))
        store.close()
        assert path.exists()

    def test_file_backed_store_uses_wal(self, tmp_path: Path) -> None:
        # Without WAL a writing process takes an exclusive lock on the file, and
        # `sync-once` or `forget` running beside the daemon fail with
        # "database is locked".
        store = UrlStore(tmp_path / STORE_FILENAME)
        mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        store.close()

    def test_two_processes_can_write_the_same_file(self, tmp_path: Path) -> None:
        """The daemon keeps its store open; `forget` and `sync-once` open the same file."""
        path = tmp_path / STORE_FILENAME
        daemon = UrlStore(path)
        daemon.add(Document(url="https://a.example/1"))

        beside = UrlStore(path)
        beside.add(Document(url="https://a.example/2"))
        assert beside.forget("https://a.example/1") is True
        beside.close()

        daemon.add(Document(url="https://a.example/3"))
        daemon.close()

        final = UrlStore(path)
        assert final.contains("https://a.example/2")
        assert final.contains("https://a.example/3")
        final.close()

    def test_upsert_keeps_known_id_when_later_row_lacks_one(self, tmp_path: Path) -> None:
        # create_document knows the id; a later listing row might not carry one.
        path = tmp_path / STORE_FILENAME
        store = UrlStore(path)
        store.add(Document(url="https://a.example/1", readwise_id="doc1"))
        store.add(Document(url="https://a.example/1", readwise_id=None))
        row = store._conn.execute(
            "SELECT readwise_id FROM documents WHERE url_key = ?", ("https://a.example/1",)
        ).fetchone()
        assert row[0] == "doc1"
        store.close()


class TestCursor:
    def test_absent_until_first_walk(self) -> None:
        store = UrlStore()
        assert store.cursor is None
        store.close()

    def test_stored_value_is_rewound_by_the_overlap(self) -> None:
        """The cursor deliberately lags the newest document seen.

        `updatedAfter` filters on a Readwise-assigned stamp, so a document saved
        during a walk can sit just under the maximum observed and be missed. Re-
        reading is idempotent; missing one produces a duplicate.
        """
        store = UrlStore()
        store.set_cursor("2026-03-01T12:00:00+00:00")
        stored = store.cursor
        assert stored is not None
        expected = datetime.fromisoformat("2026-03-01T12:00:00+00:00") - CURSOR_OVERLAP
        assert datetime.fromisoformat(stored) == expected
        store.close()

    def test_accepts_zulu_suffix(self) -> None:
        store = UrlStore()
        store.set_cursor("2026-03-01T12:00:00Z")
        assert store.cursor is not None
        store.close()

    def test_none_is_a_no_op(self) -> None:
        store = UrlStore()
        store.set_cursor("2026-03-01T12:00:00+00:00")
        before = store.cursor
        store.set_cursor(None)
        assert store.cursor == before
        store.close()

    def test_unparseable_timestamp_is_stored_verbatim(self) -> None:
        # Worst case is a narrower overlap next walk — not worth failing a sync.
        store = UrlStore()
        store.set_cursor("not-a-timestamp")
        assert store.cursor == "not-a-timestamp"
        store.close()

    def test_survives_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / STORE_FILENAME
        first = UrlStore(path)
        first.set_cursor("2026-03-01T12:00:00+00:00")
        expected = first.cursor
        first.close()

        second = UrlStore(path)
        assert second.cursor == expected
        second.close()


class TestForget:
    def test_removes_and_reports_presence(self, tmp_path: Path) -> None:
        path = tmp_path / STORE_FILENAME
        store = UrlStore(path)
        store.add(Document(url="https://a.example/1"))

        assert store.forget("https://a.example/1") is True
        assert not store.contains("https://a.example/1")
        # Gone from disk too, not just the in-memory set.
        store.close()
        reopened = UrlStore(path)
        assert not reopened.contains("https://a.example/1")
        reopened.close()

    def test_unknown_url_reports_false(self) -> None:
        store = UrlStore()
        assert store.forget("https://a.example/nope") is False
        store.close()
