from __future__ import annotations

import threading
import time
from typing import Any

import httpx
import structlog

from sync_to_readwise.core.item import Item
from sync_to_readwise.core.urlstore import Document, UrlStore

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

    Maintains a cache of known document URLs so we can dedup without re-saving
    (which would mutate location/tags on already-triaged items). Backed by
    `UrlStore`, so the cache survives restarts and a warm is incremental.
    """

    def __init__(
        self,
        token: str,
        *,
        timeout: float = 30.0,
        store: UrlStore | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=READER_API,
            headers={"Authorization": f"Token {token}"},
            timeout=timeout,
        )
        # No store supplied means an ephemeral in-memory one, which keeps
        # one-shot CLI paths and tests from touching the data dir.
        self._store = store if store is not None else UrlStore()
        self._owns_store = store is None
        self._warmed = False
        # Held across the whole walk, not just the flag check: the daemon starts
        # every source at once, and without it each would begin its own
        # concurrent walk before any of them set the flag.
        self._warm_lock = threading.Lock()
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
        if self._owns_store:
            self._store.close()

    def __enter__(self) -> ReadwiseClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def warm_cache(self) -> None:
        """Bring the local URL store in line with Reader.

        Incremental whenever the store carries a cursor from a previous
        completed walk: `/list/` is asked only for documents updated since then,
        which is normally a single request. A store with no cursor — a first run,
        or a deleted database — falls back to a full walk.

        There is no category scoping. It existed to keep the full walk
        affordable, at the cost of fetching the same document once per scope and
        leaving sources whose items span every category (Karakeep) unable to
        scope at all. Once the walk is incremental the scoping only costs
        requests.

        Idempotent per process, and safe to call from several threads: the first
        caller walks while the rest block, rather than all walking at once.
        """
        with self._warm_lock:
            if self._warmed:
                return

            cursor = self._store.cursor
            page_cursor: str | None = None
            latest_updated_at: str | None = cursor
            found = 0
            new = 0

            while True:
                params: dict[str, Any] = {}
                if cursor:
                    params["updatedAfter"] = cursor
                if page_cursor:
                    params["pageCursor"] = page_cursor

                resp = self._request("GET", "/list/", params=params)

                batch: list[Document] = []
                for doc in resp.get("results", []):
                    url = doc.get("source_url") or doc.get("url")
                    if not url:
                        continue
                    updated_at = doc.get("updated_at")
                    if updated_at and (latest_updated_at is None or updated_at > latest_updated_at):
                        latest_updated_at = updated_at
                    batch.append(
                        Document(url=url, readwise_id=doc.get("id"), updated_at=updated_at)
                    )

                found += len(batch)
                new += self._store.add_many(batch)

                page_cursor = resp.get("nextPageCursor")
                if not page_cursor:
                    break

            # Only now that pagination finished end to end. Advancing per page
            # would let a walk that dies midway strand every document after the
            # last committed page outside all future windows.
            self._store.set_cursor(latest_updated_at)
            self._warmed = True

        log.info(
            "readwise_cache_warmed",
            incremental=bool(cursor),
            found=found,
            new=new,
            known=len(self._store),
        )

    def exists(self, url: str) -> bool:
        return self._store.contains(url)

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
        self._store.add(Document(url=item.url, readwise_id=resp.get("id")))
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
