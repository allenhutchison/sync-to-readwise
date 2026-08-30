from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import pytest

from sync_to_readwise.core import urlstore as urlstore_mod
from sync_to_readwise.core.urlstore import (
    CURSOR_OVERLAP,
    SCHEMA_VERSION,
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


class TestSchemaVersion:
    def test_fresh_store_is_stamped_without_a_wipe(self, tmp_path: Path) -> None:
        path = tmp_path / STORE_FILENAME
        store = UrlStore(path)
        store.add(Document(url="https://a.example/1"))
        store.close()

        reopened = UrlStore(path)
        # Same version, so the row survives the reopen.
        assert reopened.contains("https://a.example/1")
        row = reopened._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        assert int(row[0]) == SCHEMA_VERSION
        reopened.close()

    def test_store_from_an_older_schema_is_discarded(self, tmp_path: Path) -> None:
        """A v1 store holds feed URLs, which would suppress real syncs.

        Those rows can't be topped up alongside correct ones — `exists()` would
        keep reporting saved-document membership for RSS items — so the store is
        rebuilt from scratch instead.
        """
        path = tmp_path / STORE_FILENAME
        store = UrlStore(path)
        store.add(Document(url="https://feed.example/rss-item"))
        store.set_cursor("2026-03-01T12:00:00+00:00")
        # Rewind the stamp to look like a store written before this version.
        store._conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION - 1),),
        )
        store._conn.commit()
        store.close()

        reopened = UrlStore(path)
        assert not reopened.contains("https://feed.example/rss-item")
        assert len(reopened) == 0
        # The cursor goes too, or the rebuild would only fetch recent changes
        # and leave the library permanently half-known.
        assert reopened.cursor is None
        reopened.close()

    def test_unstamped_store_with_rows_is_discarded(self, tmp_path: Path) -> None:
        # Stores written before versioning existed carry rows but no version.
        path = tmp_path / STORE_FILENAME
        store = UrlStore(path)
        store.add(Document(url="https://feed.example/rss-item"))
        store._conn.execute("DELETE FROM meta WHERE key = 'schema_version'")
        store._conn.commit()
        store.close()

        reopened = UrlStore(path)
        assert len(reopened) == 0
        reopened.close()

    def test_concurrent_opens_of_a_stale_store_are_serialized(self, tmp_path: Path) -> None:
        """Two processes opening a stale store must not undo each other.

        The check and the reset run under BEGIN IMMEDIATE, so the second opener
        sees the version the first stamped instead of acting on a stale read and
        wiping the rebuild. Threads stand in for processes here; they use
        separate connections to the same file, which is the part that matters.
        """
        path = tmp_path / STORE_FILENAME
        seed = UrlStore(path)
        seed.add(Document(url="https://feed.example/rss-item"))
        seed._conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION - 1),),
        )
        seed._conn.commit()
        seed.close()

        start = threading.Barrier(4)
        opened: list[UrlStore] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def _open() -> None:
            try:
                start.wait(timeout=5)
                store = UrlStore(path)
                with lock:
                    opened.append(store)
            except BaseException as exc:  # noqa: BLE001 - recorded and re-raised below
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=_open) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, errors
        for store in opened:
            store.close()

        # Whoever won, the store ends stamped current with the stale row gone.
        final = UrlStore(path)
        assert not final.contains("https://feed.example/rss-item")
        version = final._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        assert int(version[0]) == SCHEMA_VERSION
        final.close()

    def test_a_rebuilt_store_is_not_wiped_by_a_later_open(self, tmp_path: Path) -> None:
        """Once one opener has stamped the current version, the next tops up."""
        path = tmp_path / STORE_FILENAME
        seed = UrlStore(path)
        seed._conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION - 1),),
        )
        seed._conn.commit()
        seed.close()

        first = UrlStore(path)  # resets, stamps current
        first.add(Document(url="https://a.example/rebuilt"))
        first.set_cursor("2026-03-01T12:00:00+00:00")

        second = UrlStore(path)
        assert second.contains("https://a.example/rebuilt")
        assert second.cursor is not None
        second.close()
        first.close()

    def test_a_failed_reset_rolls_back_rather_than_half_wiping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reset that dies partway must leave the store as it was.

        Committing a half-done reset would drop the documents but keep the old
        version stamp, so the next open would wipe again and the store could
        never rebuild.
        """
        path = tmp_path / STORE_FILENAME
        seed = UrlStore(path)
        seed.add(Document(url="https://a.example/1"))
        seed.set_cursor("2026-03-01T12:00:00+00:00")
        seed._conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION - 1),),
        )
        seed._conn.commit()
        seed.close()

        boom = RuntimeError("disk gave up mid-reset")

        def _explode(*args: object, **kwargs: object) -> None:
            raise boom

        monkeypatch.setattr(urlstore_mod.log, "warning", _explode)

        with pytest.raises(RuntimeError):
            UrlStore(path)

        # Untouched: the row, the cursor, and the stale stamp are all still there.
        monkeypatch.undo()
        recovered = sqlite3.connect(path)
        assert recovered.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert (
            recovered.execute("SELECT value FROM meta WHERE key = 'updated_after'").fetchone()
            is not None
        )
        assert recovered.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[
            0
        ] == str(SCHEMA_VERSION - 1)
        recovered.close()
