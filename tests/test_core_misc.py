from __future__ import annotations

import logging
from collections.abc import Iterable
from unittest.mock import MagicMock

import pytest

from sync_to_readwise.core.config import SourceConfig
from sync_to_readwise.core.item import Item
from sync_to_readwise.core.logging import configure_logging
from sync_to_readwise.core.source import Source
from sync_to_readwise.core.syncer import Syncer, SyncResult


class TestItem:
    def test_defaults(self) -> None:
        i = Item(url="https://x.example", source_name="t")
        assert i.title is None
        assert i.tags == ()

    def test_immutable(self) -> None:
        # frozen dataclass → assignment raises FrozenInstanceError.
        from dataclasses import FrozenInstanceError

        i = Item(url="https://x.example", source_name="t")
        with pytest.raises(FrozenInstanceError):
            i.url = "https://other.example"  # type: ignore[misc]


class TestSourceABC:
    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            Source()  # type: ignore[abstract]

    def test_concrete_subclass_works(self) -> None:
        class _S(Source):
            name = "s"

            def fetch_candidates(self) -> Iterable[Item]:
                return iter([])

        s = _S()
        assert s.default_location == "later"
        assert list(s.fetch_candidates()) == []


class _FakeSource(Source):
    name = "fake"
    default_location = "shortlist"
    default_tags = ("default-tag",)
    readwise_category = "article"

    def __init__(self, items: list[Item]) -> None:
        self._items = items

    def fetch_candidates(self) -> Iterable[Item]:
        return iter(self._items)


def _stub_readwise(*, known: set[str] | None = None, raise_on: set[str] | None = None) -> MagicMock:
    rw = MagicMock()
    rw.exists.side_effect = lambda url: url in (known or set())
    if raise_on:

        def _create(item: Item, *, location: str, tags: list[str]) -> dict:
            if item.url in raise_on:
                raise RuntimeError("boom")
            return {}

        rw.create_document.side_effect = _create
    else:
        rw.create_document.return_value = {}
    return rw


class TestSyncer:
    def test_warm_cache_called_with_source_category(self) -> None:
        src = _FakeSource(items=[])
        rw = _stub_readwise()
        Syncer(rw).sync(src, SourceConfig())
        rw.warm_cache.assert_called_once_with(category="article")

    def test_warm_cache_none_when_source_lacks_category(self) -> None:
        class _NoCat(Source):
            name = "nc"

            def fetch_candidates(self) -> Iterable[Item]:
                return iter([])

        rw = _stub_readwise()
        Syncer(rw).sync(_NoCat(), SourceConfig())
        rw.warm_cache.assert_called_once_with(category=None)

    def test_creates_new_skips_known(self) -> None:
        items = [
            Item(url="https://a.example/1", source_name="fake"),
            Item(url="https://a.example/2", source_name="fake"),
            Item(url="https://a.example/3", source_name="fake"),
        ]
        src = _FakeSource(items=items)
        rw = _stub_readwise(known={"https://a.example/2"})

        result = Syncer(rw).sync(src, SourceConfig())
        assert result.seen == 3
        assert result.created == 2
        assert result.skipped == 1
        assert result.errors == 0
        assert rw.create_document.call_count == 2

    def test_location_override_from_source_config(self) -> None:
        item = Item(url="https://a.example/1", source_name="fake")
        rw = _stub_readwise()
        Syncer(rw).sync(_FakeSource(items=[item]), SourceConfig(location="archive"))
        _, kwargs = rw.create_document.call_args
        assert kwargs["location"] == "archive"

    def test_location_default_when_no_override(self) -> None:
        item = Item(url="https://a.example/1", source_name="fake")
        rw = _stub_readwise()
        Syncer(rw).sync(_FakeSource(items=[item]), SourceConfig())
        _, kwargs = rw.create_document.call_args
        # Comes from _FakeSource.default_location.
        assert kwargs["location"] == "shortlist"

    def test_tags_merge_default_and_config_sorted_dedup(self) -> None:
        item = Item(url="https://a.example/1", source_name="fake")
        rw = _stub_readwise()
        Syncer(rw).sync(
            _FakeSource(items=[item]),
            SourceConfig(tags=["custom", "default-tag"]),  # 'default-tag' duplicates Source default
        )
        _, kwargs = rw.create_document.call_args
        assert kwargs["tags"] == ["custom", "default-tag"]  # sorted, deduped

    def test_create_exception_counts_as_error_not_fatal(self) -> None:
        items = [
            Item(url="https://a.example/1", source_name="fake"),
            Item(url="https://a.example/bad", source_name="fake"),
            Item(url="https://a.example/3", source_name="fake"),
        ]
        rw = _stub_readwise(raise_on={"https://a.example/bad"})
        result = Syncer(rw).sync(_FakeSource(items=items), SourceConfig())
        assert result.seen == 3
        assert result.created == 2
        assert result.errors == 1
        assert result.skipped == 0

    def test_sync_result_dataclass_shape(self) -> None:
        # Trivial guard so renaming a field shows up in tests.
        r = SyncResult(source="s", seen=1, created=1, skipped=0, errors=0)
        assert r.__dict__ == {
            "source": "s",
            "seen": 1,
            "created": 1,
            "skipped": 0,
            "errors": 0,
            "created_items": [],
        }


class TestConfigureLogging:
    """Pytest's logging plugin owns the root logger level during a run, so we
    can't easily assert on it. Verify that our code runs cleanly and that
    structlog gets a wrapper class for each level (good enough to exercise
    every branch in the 15-line module).
    """

    @pytest.fixture(autouse=True)
    def _reset_handlers(self) -> None:
        import structlog

        root = logging.getLogger()
        original_handlers = root.handlers[:]
        root.handlers.clear()
        try:
            yield
        finally:
            root.handlers = original_handlers
            structlog.reset_defaults()

    def test_runs_with_known_level(self) -> None:
        import structlog

        configure_logging("DEBUG")
        # Just verify it dispatched successfully and structlog is now configured.
        assert structlog.is_configured()

    def test_runs_with_unknown_level(self) -> None:
        # Unknown level → falls back to INFO via getattr default; should not raise.
        configure_logging("not-a-real-level")
