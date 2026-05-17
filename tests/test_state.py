from __future__ import annotations

import json
from pathlib import Path

from sync_to_readwise.core.item import Item
from sync_to_readwise.core.state import MAX_HISTORY, STATE_FILENAME, SyncState
from sync_to_readwise.core.syncer import SyncResult


def _state(tmp_path: Path) -> SyncState:
    return SyncState(tmp_path / STATE_FILENAME)


def _result(
    source: str = "youtube",
    created: int = 2,
    items: list[Item] | None = None,
) -> SyncResult:
    return SyncResult(
        source=source,
        seen=10,
        created=created,
        skipped=8,
        errors=0,
        created_items=items or [],
    )


class TestLoad:
    def test_starts_empty_when_no_file(self, tmp_path: Path) -> None:
        snap = _state(tmp_path).snapshot()
        assert snap["daemon_started_at"] is None
        assert snap["sources"] == {}
        assert snap["recent_events"] == []

    def test_loads_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / STATE_FILENAME
        path.write_text(
            json.dumps(
                {
                    "daemon_started_at": "2026-01-01T00:00:00+00:00",
                    "sources": {},
                    "recent_events": [],
                }
            )
        )
        assert SyncState(path).snapshot()["daemon_started_at"] == "2026-01-01T00:00:00+00:00"

    def test_corrupt_file_starts_fresh(self, tmp_path: Path) -> None:
        path = tmp_path / STATE_FILENAME
        path.write_text("{not valid json")
        # Should not raise — corrupt state is logged and discarded.
        assert SyncState(path).snapshot()["sources"] == {}

    def test_non_dict_json_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / STATE_FILENAME
        path.write_text(json.dumps([1, 2, 3]))
        assert SyncState(path).snapshot()["sources"] == {}


class TestMutations:
    def test_mark_daemon_started_persists(self, tmp_path: Path) -> None:
        s = _state(tmp_path)
        s.mark_daemon_started()
        assert s.snapshot()["daemon_started_at"] is not None
        on_disk = json.loads((tmp_path / STATE_FILENAME).read_text())
        assert on_disk["daemon_started_at"] is not None

    def test_register_source(self, tmp_path: Path) -> None:
        s = _state(tmp_path)
        s.register_source("youtube", enabled=True, interval_minutes=15)
        src = s.snapshot()["sources"]["youtube"]
        assert src["enabled"] is True
        assert src["interval_minutes"] == 15

    def test_record_success(self, tmp_path: Path) -> None:
        s = _state(tmp_path)
        item = Item(url="https://y/1", source_name="youtube", title="A Video")
        s.record_success(
            _result(created=1, items=[item]), next_run_at="2026-01-01T00:15:00+00:00"
        )
        src = s.snapshot()["sources"]["youtube"]
        assert src["last_status"] == "ok"
        assert src["last_error"] is None
        assert src["auth_failed"] is False
        assert src["last_result"] == {"seen": 10, "created": 1, "skipped": 8, "errors": 0}
        assert src["total_created"] == 1
        assert src["history"] == [1]
        assert src["next_run_at"] == "2026-01-01T00:15:00+00:00"

        events = s.snapshot()["recent_events"]
        assert [e["kind"] for e in events] == ["ok", "created"]
        created = events[1]
        assert created["message"] == "A Video"
        assert created["url"] == "https://y/1"

    def test_record_success_uses_url_when_no_title(self, tmp_path: Path) -> None:
        s = _state(tmp_path)
        item = Item(url="https://y/2", source_name="youtube")  # title is None
        s.record_success(_result(created=1, items=[item]), next_run_at=None)
        created = next(e for e in s.snapshot()["recent_events"] if e["kind"] == "created")
        assert created["message"] == "https://y/2"

    def test_total_created_and_history_accumulate(self, tmp_path: Path) -> None:
        s = _state(tmp_path)
        s.record_success(_result(created=2), next_run_at=None)
        s.record_success(_result(created=3), next_run_at=None)
        src = s.snapshot()["sources"]["youtube"]
        assert src["total_created"] == 5
        assert src["history"] == [2, 3]

    def test_record_failure(self, tmp_path: Path) -> None:
        s = _state(tmp_path)
        s.record_failure(
            "youtube", error="YouTubeAuthError: revoked", auth_failed=True, next_run_at=None
        )
        src = s.snapshot()["sources"]["youtube"]
        assert src["last_status"] == "error"
        assert src["last_error"] == "YouTubeAuthError: revoked"
        assert src["auth_failed"] is True
        assert s.snapshot()["recent_events"][0]["kind"] == "error"

    def test_history_capped(self, tmp_path: Path) -> None:
        s = _state(tmp_path)
        for i in range(MAX_HISTORY + 6):
            s.record_success(_result(created=i), next_run_at=None)
        history = s.snapshot()["sources"]["youtube"]["history"]
        assert len(history) == MAX_HISTORY
        assert history[-1] == MAX_HISTORY + 5  # newest retained

    def test_events_capped(self, tmp_path: Path) -> None:
        s = _state(tmp_path)
        for _ in range(60):
            s.record_failure("youtube", error="e", auth_failed=False, next_run_at=None)
        assert len(s.snapshot()["recent_events"]) == 40

    def test_snapshot_is_a_copy(self, tmp_path: Path) -> None:
        s = _state(tmp_path)
        s.register_source("youtube", enabled=True, interval_minutes=15)
        snap = s.snapshot()
        snap["sources"]["youtube"]["interval_minutes"] = 999
        # Mutating the snapshot must not leak back into the store.
        assert s.snapshot()["sources"]["youtube"]["interval_minutes"] == 15

    def test_state_survives_reload(self, tmp_path: Path) -> None:
        path = tmp_path / STATE_FILENAME
        first = SyncState(path)
        first.record_success(_result(created=4), next_run_at=None)
        # A fresh instance reads what the first one flushed.
        assert SyncState(path).snapshot()["sources"]["youtube"]["total_created"] == 4
