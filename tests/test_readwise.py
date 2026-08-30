from __future__ import annotations

from typing import Any

import httpx
import pytest

from sync_to_readwise.core import readwise as readwise_mod
from sync_to_readwise.core.item import Item
from sync_to_readwise.core.readwise import (
    DEFAULT_RETRY_AFTER_S,
    LIST_MIN_INTERVAL_S,
    MAX_ATTEMPTS,
    SAVE_MIN_INTERVAL_S,
    ReadwiseClient,
    ReadwiseError,
    _parse_retry_after,
)


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace time.sleep with a recorder so the suite doesn't actually wait."""
    sleeps: list[float] = []
    monkeypatch.setattr(readwise_mod.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


class _Clock:
    """Monotonic stand-in whose only movement comes from simulated sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """Freeze time and make `sleep` advance it, so throttle math is exact.

    Patching `sleep` to a no-op would leave the clock still, and every
    subsequent throttle check would see zero elapsed time and sleep again.
    """
    clock = _Clock()

    def _sleep(seconds: float) -> None:
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(readwise_mod.time, "monotonic", clock)
    monkeypatch.setattr(readwise_mod.time, "sleep", _sleep)
    return clock


@pytest.fixture
def client() -> ReadwiseClient:
    c = ReadwiseClient("token")
    yield c
    c.close()


def _item(url: str = "https://example.com/x", **overrides: Any) -> Item:
    return Item(url=url, source_name="t", **overrides)


class TestParseRetryAfter:
    def test_default_when_missing(self) -> None:
        assert _parse_retry_after(None) == DEFAULT_RETRY_AFTER_S

    def test_default_when_unparseable(self) -> None:
        # HTTP-date format isn't supported; we fall back to the default.
        assert _parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == DEFAULT_RETRY_AFTER_S

    def test_parses_numeric(self) -> None:
        assert _parse_retry_after("12") == 12.0
        assert _parse_retry_after("3.5") == 3.5


class TestContextManager:
    def test_context_manager_closes(self) -> None:
        with ReadwiseClient("token") as rw:
            assert isinstance(rw, ReadwiseClient)
        # Underlying client should be closed.
        assert rw._client.is_closed


class TestWarmCache:
    def test_paginates_and_collects_urls(
        self, httpx_mock, client: ReadwiseClient, no_sleep: list[float]
    ) -> None:
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/list/",
            json={
                "results": [
                    {"source_url": "https://a.example/1"},
                    {"url": "https://a.example/2"},  # 'url' fallback
                    {"source_url": None, "url": None},  # silently skipped
                ],
                "nextPageCursor": "page2",
            },
        )
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/list/?pageCursor=page2",
            json={
                "results": [{"source_url": "https://a.example/3"}],
                "nextPageCursor": None,
            },
        )

        client.warm_cache()
        assert client.exists("https://a.example/1")
        assert client.exists("https://a.example/2")
        assert client.exists("https://a.example/3")
        assert not client.exists("https://a.example/missing")

    def test_scoped_to_category(
        self, httpx_mock, client: ReadwiseClient, no_sleep: list[float]
    ) -> None:
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/list/?category=video",
            json={"results": [{"source_url": "https://yt.example/v"}], "nextPageCursor": None},
        )
        client.warm_cache(category="video")
        assert client.exists("https://yt.example/v")

    def test_idempotent_per_category(
        self, httpx_mock, client: ReadwiseClient, no_sleep: list[float]
    ) -> None:
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/list/?category=video",
            json={"results": [], "nextPageCursor": None},
        )
        client.warm_cache(category="video")
        # Second call with same category must not issue another HTTP request —
        # if it did, pytest-httpx would error on a missing matcher.
        client.warm_cache(category="video")

    def test_distinct_categories_warm_separately(
        self, httpx_mock, client: ReadwiseClient, no_sleep: list[float]
    ) -> None:
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/list/?category=video",
            json={"results": [], "nextPageCursor": None},
        )
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/list/?category=article",
            json={"results": [], "nextPageCursor": None},
        )
        client.warm_cache(category="video")
        client.warm_cache(category="article")


class TestCreateDocument:
    def test_payload_includes_only_present_fields(
        self, httpx_mock, client: ReadwiseClient, no_sleep: list[float]
    ) -> None:
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/save/",
            method="POST",
            json={"id": "abc"},
            status_code=201,
        )

        client.create_document(_item(), location="later", tags=[])

        req = httpx_mock.get_request()
        body = req.read()
        # Drop optional fields when empty/None.
        assert b"title" not in body
        assert b"author" not in body
        assert b"tags" not in body
        assert b"summary" not in body

    def test_payload_with_full_item(
        self, httpx_mock, client: ReadwiseClient, no_sleep: list[float]
    ) -> None:
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/save/",
            method="POST",
            json={},
            status_code=201,
        )

        item = _item(
            title="T",
            author="A",
            summary="S",
            published_date="2026-01-01",
            image_url="https://img.example/1",
        )
        client.create_document(item, location="archive", tags=["x", "y"])

        import json as _json

        sent = _json.loads(httpx_mock.get_request().read())
        assert sent == {
            "url": item.url,
            "location": "archive",
            "saved_using": "sync-to-readwise",
            "title": "T",
            "author": "A",
            "summary": "S",
            "published_date": "2026-01-01",
            "image_url": "https://img.example/1",
            "tags": ["x", "y"],
        }

    def test_url_added_to_known_cache(
        self, httpx_mock, client: ReadwiseClient, no_sleep: list[float]
    ) -> None:
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/save/",
            method="POST",
            json={},
            status_code=201,
        )

        item = _item(url="https://e.example/1")
        assert not client.exists(item.url)
        client.create_document(item, location="later", tags=[])
        assert client.exists(item.url)


class TestThrottle:
    """Pacing is per endpoint family, because the two limits differ.

    Readwise allows 20/min on /list/ and 50/min on /save/, so one shared clock
    would either throttle saves needlessly or let list walks exceed their
    ceiling. `fake_clock` makes sleeping advance virtual time, so these assert
    real throttle behavior without the suite actually waiting.
    """

    def test_first_request_is_not_delayed(
        self, httpx_mock, client: ReadwiseClient, fake_clock: _Clock
    ) -> None:
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/list/",
            json={"results": [], "nextPageCursor": None},
        )
        client.warm_cache()
        assert fake_clock.sleeps == []

    def test_list_pages_are_paced(
        self, httpx_mock, client: ReadwiseClient, fake_clock: _Clock
    ) -> None:
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/list/",
            json={"results": [], "nextPageCursor": "page2"},
        )
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/list/?pageCursor=page2",
            json={"results": [], "nextPageCursor": None},
        )
        client.warm_cache()
        # Second page waits out the /list/ interval; the first does not.
        assert fake_clock.sleeps == [LIST_MIN_INTERVAL_S]

    def test_saves_are_paced(self, httpx_mock, client: ReadwiseClient, fake_clock: _Clock) -> None:
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/save/",
            method="POST",
            json={},
            status_code=201,
            is_reusable=True,
        )
        client.create_document(_item(url="https://a.example/1"), location="later", tags=[])
        client.create_document(_item(url="https://a.example/2"), location="later", tags=[])
        assert fake_clock.sleeps == [SAVE_MIN_INTERVAL_S]

    def test_list_and_save_clocks_are_independent(
        self, httpx_mock, client: ReadwiseClient, fake_clock: _Clock
    ) -> None:
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/list/",
            json={"results": [], "nextPageCursor": None},
        )
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/save/",
            method="POST",
            json={},
            status_code=201,
        )
        client.warm_cache()
        client.create_document(_item(url="https://a.example/1"), location="later", tags=[])
        # A save immediately after a list must not inherit the list's clock.
        assert fake_clock.sleeps == []


class TestRetry:
    def test_429_retries_with_retry_after(
        self, httpx_mock, client: ReadwiseClient, no_sleep: list[float]
    ) -> None:
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/save/",
            method="POST",
            status_code=429,
            headers={"Retry-After": "2"},
        )
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/save/",
            method="POST",
            status_code=201,
            json={},
        )

        client.create_document(_item(), location="later", tags=[])
        # +1 margin on retry-after sleeps.
        assert any(s == pytest.approx(3.0) for s in no_sleep)

    def test_429_default_retry_after_when_header_missing(
        self, httpx_mock, client: ReadwiseClient, no_sleep: list[float]
    ) -> None:
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/save/",
            method="POST",
            status_code=429,
        )
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/save/",
            method="POST",
            status_code=201,
            json={},
        )
        client.create_document(_item(), location="later", tags=[])
        assert any(s == pytest.approx(DEFAULT_RETRY_AFTER_S + 1.0) for s in no_sleep)

    def test_5xx_retries_then_succeeds(
        self, httpx_mock, client: ReadwiseClient, no_sleep: list[float]
    ) -> None:
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/save/",
            method="POST",
            status_code=500,
        )
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/save/",
            method="POST",
            status_code=503,
        )
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/save/",
            method="POST",
            status_code=201,
            json={},
        )
        client.create_document(_item(), location="later", tags=[])

    def test_transport_error_retries(
        self,
        client: ReadwiseClient,
        no_sleep: list[float],
        monkeypatch: pytest.MonkeyPatch,
        httpx_mock,
    ) -> None:
        # First call raises a transport error, the rest succeed.
        calls = {"n": 0}
        original = client._client.request

        def flaky_request(method: str, path: str, **kw: Any):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("boom")
            return original(method, path, **kw)

        monkeypatch.setattr(client._client, "request", flaky_request)
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/save/",
            method="POST",
            status_code=201,
            json={},
        )
        client.create_document(_item(), location="later", tags=[])
        assert calls["n"] == 2

    def test_429_exhaustion_raises(
        self, httpx_mock, client: ReadwiseClient, no_sleep: list[float]
    ) -> None:
        for _ in range(MAX_ATTEMPTS):
            httpx_mock.add_response(
                url="https://readwise.io/api/v3/save/",
                method="POST",
                status_code=429,
                headers={"Retry-After": "1"},
            )
        with pytest.raises(httpx.HTTPStatusError):
            client.create_document(_item(), location="later", tags=[])

    def test_5xx_exhaustion_raises(
        self, httpx_mock, client: ReadwiseClient, no_sleep: list[float]
    ) -> None:
        for _ in range(MAX_ATTEMPTS):
            httpx_mock.add_response(
                url="https://readwise.io/api/v3/save/",
                method="POST",
                status_code=500,
            )
        with pytest.raises(httpx.HTTPStatusError):
            client.create_document(_item(), location="later", tags=[])

    def test_transport_error_exhaustion_raises(
        self,
        client: ReadwiseClient,
        no_sleep: list[float],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def always_fail(*_a: Any, **_kw: Any) -> Any:
            raise httpx.ConnectError("nope")

        monkeypatch.setattr(client._client, "request", always_fail)
        with pytest.raises(httpx.TransportError):
            client.create_document(_item(), location="later", tags=[])

    def test_4xx_other_raises_immediately(
        self, httpx_mock, client: ReadwiseClient, no_sleep: list[float]
    ) -> None:
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/save/",
            method="POST",
            status_code=400,
            json={"detail": "bad"},
        )
        with pytest.raises(httpx.HTTPStatusError):
            client.create_document(_item(), location="later", tags=[])


class TestRequestEdgeCases:
    def test_empty_body_returns_empty_dict(
        self, httpx_mock, client: ReadwiseClient, no_sleep: list[float]
    ) -> None:
        # 204-ish path (no content). _request should return {} rather than crash.
        httpx_mock.add_response(
            url="https://readwise.io/api/v3/list/",
            method="GET",
            content=b"",
            status_code=200,
        )
        # Use the public surface to exercise _request.
        client.warm_cache()
        # A no-content list still completes the warm_cache loop.
        assert "*" in client._cache_warmed_for


def test_readwise_error_is_exception() -> None:
    assert issubclass(ReadwiseError, Exception)
