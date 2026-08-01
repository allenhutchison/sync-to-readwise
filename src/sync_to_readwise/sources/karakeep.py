"""Karakeep link bookmarks → Readwise Reader."""

from __future__ import annotations

from collections.abc import Iterable

import httpx
from pydantic import BaseModel, ConfigDict

from sync_to_readwise.core.item import Item
from sync_to_readwise.core.source import Source

PAGE_SIZE = 100


class KarakeepOptions(BaseModel):
    """Typed view of this source's `sources.karakeep.*` block in config.yaml.

    `SourceConfig` allows extra keys so a source can carry its own options, but
    that leaves them unvalidated: `no_sync_tags: private` (a scalar instead of a
    list) would become `("p", "r", "i", ...)` and silently stop excluding the
    `private` tag. Validating here turns that into a startup error instead of a
    bookmark leaking into Reader. `extra="forbid"` catches misspelled keys for
    the same reason — a typo'd option must not look like it took effect.
    """

    model_config = ConfigDict(extra="forbid")

    no_sync_tags: tuple[str, ...] = ("no-sync",)
    import_tags: bool = True


class KarakeepSource(Source):
    """Sync unarchived Karakeep link bookmarks, with tag-based opt-out."""

    name = "karakeep"
    default_location = "later"
    default_tags = ("karakeep",)
    readwise_category = "article"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        no_sync_tags: tuple[str, ...] = ("no-sync",),
        import_tags: bool = True,
    ) -> None:
        if not base_url:
            raise ValueError("SYNCRW_KARAKEEP_URL must be set.")
        if not api_key:
            raise ValueError("KARAKEEP_API_KEY must be set (via Doppler or .env).")
        self._base_url = base_url.rstrip("/") + "/api/v1"
        self._api_key = api_key
        self._no_sync_tags = {tag.casefold() for tag in no_sync_tags}
        self._import_tags = import_tags

    def fetch_candidates(self) -> Iterable[Item]:
        with httpx.Client(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "User-Agent": "sync-to-readwise",
            },
            timeout=30.0,
        ) as client:
            cursor: str | None = None
            while True:
                params: dict[str, str | int | bool] = {
                    "archived": False,
                    "sortOrder": "desc",
                    "limit": PAGE_SIZE,
                    "includeContent": False,
                }
                if cursor:
                    params["cursor"] = cursor

                response = client.get("/bookmarks", params=params)
                response.raise_for_status()
                payload = response.json()

                for bookmark in payload.get("bookmarks", []):
                    item = self._to_item(bookmark)
                    if item is not None:
                        yield item

                cursor = payload.get("nextCursor")
                if not cursor:
                    break

    def _to_item(self, bookmark: dict) -> Item | None:
        content = bookmark.get("content") or {}
        if content.get("type") != "link" or bookmark.get("archived"):
            return None

        tag_names = tuple(
            tag["name"]
            for tag in bookmark.get("tags", [])
            if isinstance(tag, dict) and isinstance(tag.get("name"), str)
        )
        if self._no_sync_tags.intersection(tag.casefold() for tag in tag_names):
            return None

        title = bookmark.get("title") or content.get("title")
        summary = bookmark.get("summary") or content.get("description")
        return Item(
            url=content["url"],
            source_name=self.name,
            title=title,
            author=content.get("author"),
            summary=summary,
            published_date=content.get("datePublished"),
            tags=tag_names if self._import_tags else (),
            image_url=content.get("imageUrl"),
        )
