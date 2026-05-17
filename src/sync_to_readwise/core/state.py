"""Runtime activity record for the status page.

`SyncResult` (see `syncer.py`) is ephemeral — logged and discarded. The status
page needs that history to survive, so the daemon records every run here. The
store is JSON-backed (`<data_dir>/sync_state.json`, matching the token-file
pattern) and guarded by a single lock: APScheduler worker threads write while
the web server thread reads.
"""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import structlog

from sync_to_readwise.core.syncer import SyncResult

log = structlog.get_logger(__name__)

STATE_FILENAME = "sync_state.json"
MAX_RECENT_EVENTS = 40
MAX_HISTORY = 14


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _empty_source() -> dict:
    return {
        "enabled": True,
        "interval_minutes": None,
        "last_run_at": None,
        "last_success_at": None,
        "last_status": None,  # "ok" | "error" | None (never run)
        "last_error": None,
        "auth_failed": False,
        "last_result": None,  # {"seen", "created", "skipped", "errors"}
        "next_run_at": None,
        "total_created": 0,
        "history": [],  # created-count per recent successful sync (for the sparkline)
    }


class SyncState:
    """Thread-safe, JSON-backed record of daemon activity.

    Every public method acquires a single lock and flushes to disk, so the
    file on disk always reflects the latest committed state and the web
    server can read a consistent `snapshot()` at any time.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict = {
            "daemon_started_at": None,
            "sources": {},
            "recent_events": [],
        }
        self._load()

    # ---------- persistence ----------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            loaded = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            # A corrupt state file is not worth crashing the daemon over —
            # start fresh; the next sync repopulates it.
            log.warning("sync_state_load_failed", path=str(self._path), error=str(e))
            return
        if isinstance(loaded, dict):
            self._data.update(loaded)

    def _flush(self) -> None:
        """Atomically write the whole state. Caller must hold the lock."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(self._path)

    # ---------- helpers (caller holds the lock) ----------

    def _source(self, name: str) -> dict:
        return self._data["sources"].setdefault(name, _empty_source())

    def _event(self, kind: str, source: str, message: str, url: str | None = None) -> None:
        event = {"at": _now(), "kind": kind, "source": source, "message": message}
        if url:
            event["url"] = url
        events = self._data["recent_events"]
        events.insert(0, event)
        del events[MAX_RECENT_EVENTS:]

    # ---------- mutations ----------

    def mark_daemon_started(self) -> None:
        with self._lock:
            self._data["daemon_started_at"] = _now()
            self._flush()

    def register_source(self, name: str, *, enabled: bool, interval_minutes: int) -> None:
        with self._lock:
            src = self._source(name)
            src["enabled"] = enabled
            src["interval_minutes"] = interval_minutes
            self._flush()

    def record_success(self, result: SyncResult, *, next_run_at: str | None) -> None:
        with self._lock:
            src = self._source(result.source)
            now = _now()
            src["last_run_at"] = now
            src["last_success_at"] = now
            src["last_status"] = "ok"
            src["last_error"] = None
            src["auth_failed"] = False
            src["next_run_at"] = next_run_at
            src["last_result"] = {
                "seen": result.seen,
                "created": result.created,
                "skipped": result.skipped,
                "errors": result.errors,
            }
            src["total_created"] += result.created
            history = src.setdefault("history", [])
            history.append(result.created)
            del history[:-MAX_HISTORY]
            for item in result.created_items:
                self._event("created", result.source, item.title or item.url, url=item.url)
            self._event(
                "ok",
                result.source,
                f"seen {result.seen}, created {result.created}, skipped {result.skipped}",
            )
            self._flush()

    def record_failure(
        self, source: str, *, error: str, auth_failed: bool, next_run_at: str | None
    ) -> None:
        with self._lock:
            src = self._source(source)
            src["last_run_at"] = _now()
            src["last_status"] = "error"
            src["last_error"] = error
            src["auth_failed"] = auth_failed
            src["next_run_at"] = next_run_at
            self._event("error", source, error)
            self._flush()

    # ---------- read ----------

    def snapshot(self) -> dict:
        """Return a deep copy of the full state — safe to hand to the renderer."""
        with self._lock:
            return deepcopy(self._data)
