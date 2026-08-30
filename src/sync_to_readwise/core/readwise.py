from __future__ import annotations

import threading
import time
from typing import Any

import httpx
import structlog

from sync_to_readwise.core.item import Item

log = structlog.get_logger(__name__)

READER_API = "https://readwise.io/api/v3"

# Readwise's documented per-token rate limits (https://readwise.io/reader_api):
# 20 req/min on /list/, 50 req/min on /save/.
#
# These were previously transposed here: /save/ was paced as though it were the
# 20/min endpoint, and /list/ was left unthrottled entirely as "the more
# generous one". A cold cache warm therefore paginated flat out against the
# *stricter* of the two, and since the daemon warms every source at startup it
# spent most of that time in 429 backoff (Readwise returns a ~47-58s
# Retry-After under sustained pressure).
#
# 20/min is one request every 3.0s; 3.2s leaves headroom for jitter.
LIST_MIN_INTERVAL_S = 3.2
# 50/min would allow 1.2s, but /save/ mutates the user's library and a backfill
# is not latency-sensitive, so we stay deliberately well under the ceiling.
SAVE_MIN_INTERVAL_S = 3.5

DEFAULT_RETRY_AFTER_S = 10.0
MAX_ATTEMPTS = 8


class ReadwiseError(Exception):
    pass


class ReadwiseClient:
    """Thin client for the Readwise Reader v3 API.

    Maintains an in-memory cache of known document URLs so we can dedup without
    re-saving (which would mutate location/tags on already-triaged items).
    """

    def __init__(self, token: str, *, timeout: float = 30.0) -> None:
        self._client = httpx.Client(
            base_url=READER_API,
            headers={"Authorization": f"Token {token}"},
            timeout=timeout,
        )
        self._known_urls: set[str] = set()
        self._cache_warmed_for: set[str] = set()
        # Rate limits are per access token, and `run_daemon` shares one client
        # across per-source scheduler threads that all fire at startup. Pacing
        # therefore has to be serialized globally: an unsynchronized check lets
        # N threads each independently conclude that enough time has passed and
        # issue their requests simultaneously, which is N times the intended
        # rate no matter what the interval is set to.
        self._throttle_lock = threading.Lock()
        self._last_request_at: dict[str, float] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ReadwiseClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def warm_cache(self, *, category: str | None = None) -> None:
        """Populate the in-memory URL cache from Readwise.

        Pass category='video' to scope to YouTube/video URLs (much faster than listing all).
        Idempotent per category.
        """
        cache_key = category or "*"
        if cache_key in self._cache_warmed_for:
            return

        page_cursor: str | None = None
        count = 0
        while True:
            params: dict[str, Any] = {}
            if category:
                params["category"] = category
            if page_cursor:
                params["pageCursor"] = page_cursor

            resp = self._request("GET", "/list/", params=params)
            for doc in resp.get("results", []):
                url = doc.get("source_url") or doc.get("url")
                if url:
                    self._known_urls.add(url)
                    count += 1

            page_cursor = resp.get("nextPageCursor")
            if not page_cursor:
                break

        self._cache_warmed_for.add(cache_key)
        log.info("readwise_cache_warmed", category=category, count=count)

    def exists(self, url: str) -> bool:
        return url in self._known_urls

    def create_document(self, item: Item, *, location: str, tags: list[str]) -> dict[str, Any]:
        """Save a URL to Readwise Reader. Returns the API response payload."""
        payload: dict[str, Any] = {
            "url": item.url,
            "location": location,
            "saved_using": "sync-to-readwise",
        }
        if item.title:
            payload["title"] = item.title
        if item.author:
            payload["author"] = item.author
        if item.summary:
            payload["summary"] = item.summary
        if item.published_date:
            payload["published_date"] = item.published_date
        if item.image_url:
            payload["image_url"] = item.image_url
        if tags:
            payload["tags"] = tags

        resp = self._request("POST", "/save/", json=payload)
        self._known_urls.add(item.url)
        return resp

    # ---------- internals ----------

    def _throttle(self, path: str) -> None:
        """Pace requests against the rate limit for `path`'s endpoint.

        Called once per attempt inside `_request`, not at the call site, so
        paginated walks and post-429 retries are paced too — not just the first
        request of a sequence. The lock is held across the sleep so concurrent
        callers queue behind one another rather than all waking together.
        """
        if path.startswith("/save"):
            key, min_interval = "save", SAVE_MIN_INTERVAL_S
        else:
            key, min_interval = "list", LIST_MIN_INTERVAL_S

        with self._throttle_lock:
            last = self._last_request_at.get(key)
            if last is not None:
                elapsed = time.monotonic() - last
                if elapsed < min_interval:
                    time.sleep(min_interval - elapsed)
            self._last_request_at[key] = time.monotonic()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """HTTP request with manual retry that honors Retry-After on 429s.

        Tenacity's exponential backoff caps below the ~47s Retry-After Readwise
        returns under sustained pressure, so we can't rely on it here.
        """
        backoff = 2.0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._throttle(path)
            try:
                r = self._client.request(method, path, params=params, json=json)
            except httpx.TransportError as e:
                if attempt == MAX_ATTEMPTS:
                    raise
                log.warning(
                    "readwise_transport_error",
                    path=path,
                    error=str(e),
                    attempt=attempt,
                    backoff=backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue

            if r.status_code == 429:
                retry_after = _parse_retry_after(r.headers.get("Retry-After"))
                log.warning(
                    "readwise_rate_limited",
                    path=path,
                    retry_after=retry_after,
                    attempt=attempt,
                )
                if attempt == MAX_ATTEMPTS:
                    r.raise_for_status()
                time.sleep(retry_after + 1.0)  # +1s of margin
                continue

            if 500 <= r.status_code < 600:
                if attempt == MAX_ATTEMPTS:
                    r.raise_for_status()
                log.warning(
                    "readwise_server_error",
                    path=path,
                    status=r.status_code,
                    attempt=attempt,
                    backoff=backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue

            r.raise_for_status()
            return r.json() if r.content else {}

        raise ReadwiseError(f"Exhausted {MAX_ATTEMPTS} attempts for {method} {path}")


def _parse_retry_after(value: str | None) -> float:
    if not value:
        return DEFAULT_RETRY_AFTER_S
    try:
        return float(value)
    except ValueError:
        return DEFAULT_RETRY_AFTER_S
