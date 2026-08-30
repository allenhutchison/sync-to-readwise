"""Durable record of which URLs already exist in Readwise Reader.

The syncer's only dedup signal is "is this URL already in Reader?" — re-saving
an existing document would mutate the location and tags on something the user
has already triaged. Answering that question used to mean walking the entire
Reader library through ``/list/`` on every process start. At 20 requests per
minute that walk is the dominant cost of a cold boot, and it was paid again on
every restart because the answer lived only in memory.

This store keeps the answer on disk between runs. The freshness cursor is an
ISO-8601 timestamp that ``/list/`` accepts directly as ``updatedAfter``, so a
restart re-reads only what changed rather than the whole library.

SQLite rather than a JSON file (the pattern used by `state.py`) for three
reasons: saves append a single row instead of rewriting the whole document, the
cursor advances in the same transaction as the rows it covers, and the scheduler
threads that write can share a connection with the reader.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import structlog

log = structlog.get_logger(__name__)

STORE_FILENAME = "readwise_urls.db"

CURSOR_KEY = "updated_after"

VERSION_KEY = "schema_version"

# Bump when stored rows become wrong rather than merely stale, and an existing
# store has to be discarded rather than topped up. On a mismatch the documents
# table and the cursor are dropped and the next warm rebuilds from scratch —
# safe precisely because this is a cache, reconstructible from Reader.
#
# 2: v1 stores were built from an unfiltered /list/ walk and so contain
#    `location=feed` RSS items. Those make `exists()` report saved-document
#    membership for URLs the user never saved, silently suppressing syncs, so
#    they cannot simply be left in place alongside correctly-scoped rows.
SCHEMA_VERSION = 2

# The cursor is rewound by this much before being stored. `updatedAfter` filters
# on a timestamp Readwise assigns, so a document saved during the walk can carry
# a stamp just below the maximum we observed and be missed by the next pass. Re-
# reading a few documents is free (the upsert is idempotent); missing one costs a
# duplicate in the user's library, so the asymmetry favors a generous overlap.
CURSOR_OVERLAP = timedelta(minutes=10)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    url_key     TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    readwise_id TEXT,
    updated_at  TEXT,
    seen_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Document(NamedTuple):
    """A Reader document as far as deduplication cares about it."""

    url: str
    readwise_id: str | None = None
    updated_at: str | None = None


def url_key(url: str) -> str:
    """Dedup key for a URL.

    Deliberately conservative. Readwise returns ``source_url`` byte-identical to
    what we sent — verified against a 304-item YouTube sync that skipped all 304
    on a cold cache — so there is no observed canonicalization to compensate for.
    Aggressive normalization here could only collapse two genuinely distinct URLs
    into one and silently drop a document, which is the expensive direction to be
    wrong in. The column exists so a real normalization rule can be introduced
    later with a migration rather than a schema change.
    """
    return url.strip()


class UrlStore:
    """Thread-safe, SQLite-backed set of known Reader URLs.

    Membership is served from an in-memory set loaded once at open, so `contains`
    stays a hash lookup on the sync hot path; SQLite is the durability layer, not
    the query path.
    """

    def __init__(self, path: Path | None = None) -> None:
        """Open the store at `path`, or in memory when `path` is None.

        An in-memory store behaves identically but never persists, which keeps
        the one-shot CLI paths and the tests from littering the data dir.
        """
        self._path = path
        target = str(path) if path is not None else ":memory:"
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

        # check_same_thread=False because the daemon's per-source scheduler
        # threads share one store; every access is serialized by self._lock.
        self._conn = sqlite3.connect(target, check_same_thread=False)
        if path is not None:
            # The daemon holds this file open for its whole life while
            # `sync-once` and `forget` open it from separate processes. Under
            # the default rollback journal a writer takes an exclusive lock on
            # the file, so those would fail with "database is locked"; WAL lets
            # a reader and a writer coexist, and the busy timeout absorbs the
            # brief writer-writer overlaps that remain. Not applied to
            # in-memory stores, which have no second process to contend with.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

        self._reset_if_stale_schema()

        self._keys: set[str] = {
            row[0] for row in self._conn.execute("SELECT url_key FROM documents")
        }
        if self._keys:
            log.info("url_store_loaded", count=len(self._keys), path=str(path) if path else None)

    def _reset_if_stale_schema(self) -> None:
        """Drop everything if the store predates SCHEMA_VERSION.

        Runs before `_keys` is populated so a stale store is never read into
        memory. A fresh store has no version row and is stamped without a wipe,
        so this is a no-op on first open.
        """
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (VERSION_KEY,)).fetchone()
        stored = int(row[0]) if row and row[0].isdigit() else None

        if stored == SCHEMA_VERSION:
            return

        if stored is not None or self._conn.execute("SELECT 1 FROM documents LIMIT 1").fetchone():
            discarded = self._conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            self._conn.execute("DELETE FROM documents")
            self._conn.execute("DELETE FROM meta WHERE key = ?", (CURSOR_KEY,))
            log.warning(
                "url_store_schema_reset",
                discarded=discarded,
                found_version=stored,
                expected_version=SCHEMA_VERSION,
            )

        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (VERSION_KEY, str(SCHEMA_VERSION)),
        )
        self._conn.commit()

    # ---------- membership ----------

    def contains(self, url: str) -> bool:
        return url_key(url) in self._keys

    def __len__(self) -> int:
        return len(self._keys)

    # ---------- writes ----------

    def add(self, doc: Document) -> None:
        self.add_many([doc])

    def add_many(self, docs: Iterable[Document]) -> int:
        """Upsert documents. Returns the number that were not already known."""
        rows = []
        new = 0
        seen_at = datetime.now().astimezone().isoformat(timespec="seconds")
        for doc in docs:
            key = url_key(doc.url)
            if not key:
                continue
            if key not in self._keys:
                new += 1
            rows.append((key, doc.url, doc.readwise_id, doc.updated_at, seen_at))

        if not rows:
            return 0

        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO documents (url_key, url, readwise_id, updated_at, seen_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(url_key) DO UPDATE SET
                    readwise_id = COALESCE(excluded.readwise_id, documents.readwise_id),
                    updated_at  = COALESCE(excluded.updated_at, documents.updated_at),
                    seen_at     = excluded.seen_at
                """,
                rows,
            )
            self._conn.commit()
            self._keys.update(row[0] for row in rows)
        return new

    def forget(self, url: str) -> bool:
        """Drop one URL so the next sync treats it as new. Returns True if present.

        The manual counterpart to never pruning automatically: a reconciler that
        removes entries it did not observe in a listing would, on a truncated
        walk, conclude the whole library is gone and re-save all of it. Deletion
        is rare and better driven by an explicit human action.

        Only affects this store instance. A daemon already running holds its own
        `_keys` set, loaded at startup, and will keep reporting the URL as known
        until it restarts — so a `forget` from the CLI reaches the daemon on its
        next restart, not immediately. Re-reading the whole set per sync to close
        that gap would cost a full table scan every interval, on every source,
        to serve an operation that happens by hand and rarely.
        """
        key = url_key(url)
        with self._lock:
            self._conn.execute("DELETE FROM documents WHERE url_key = ?", (key,))
            self._conn.commit()
            existed = key in self._keys
            self._keys.discard(key)
        return existed

    # ---------- cursor ----------

    @property
    def cursor(self) -> str | None:
        """`updatedAfter` value for the next walk, or None if never completed one."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (CURSOR_KEY,)
            ).fetchone()
        return row[0] if row else None

    def set_cursor(self, latest_updated_at: str | None) -> None:
        """Advance the cursor to `latest_updated_at`, rewound by CURSOR_OVERLAP.

        Call this only after a walk has paginated to completion. Advancing it
        per page means a walk that dies partway — the retry ceiling, a transport
        error — leaves the cursor past documents that were never ingested, and
        those are then invisible to every future pass.
        """
        if not latest_updated_at:
            return
        value = _rewind(latest_updated_at, CURSOR_OVERLAP)
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (CURSOR_KEY, value),
            )
            self._conn.commit()
        log.info("url_store_cursor_advanced", cursor=value)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _rewind(timestamp: str, delta: timedelta) -> str:
    """Subtract `delta` from an ISO-8601 timestamp, passing it through unparsed on failure.

    An unparseable stamp is not worth failing a sync over: the worst case of
    storing it as-is is a slightly narrower overlap window on the next walk.
    """
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        log.warning("url_store_unparseable_timestamp", timestamp=timestamp)
        return timestamp
    return (parsed - delta).isoformat(timespec="seconds")
