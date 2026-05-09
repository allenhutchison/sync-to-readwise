from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from sync_to_readwise.core.item import Item

log = structlog.get_logger(__name__)

READER_API = "https://readwise.io/api/v3"

# Readwise documents 20 req/min on the /save/ endpoint. Pace at one every ~3.5s
# to stay comfortably under that. /list/ has a more generous limit, so we only
# throttle saves.
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
        self._last_save_at: float = 0.0

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

        self._throttle_save()
        resp = self._request("POST", "/save/", json=payload)
        self._last_save_at = time.monotonic()
        self._known_urls.add(item.url)
        return resp

    # ---------- internals ----------

    def _throttle_save(self) -> None:
        elapsed = time.monotonic() - self._last_save_at
        if elapsed < SAVE_MIN_INTERVAL_S:
            time.sleep(SAVE_MIN_INTERVAL_S - elapsed)

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
