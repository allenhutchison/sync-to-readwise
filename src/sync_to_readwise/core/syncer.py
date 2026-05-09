from __future__ import annotations

from dataclasses import dataclass

import structlog

from sync_to_readwise.core.config import SourceConfig
from sync_to_readwise.core.readwise import ReadwiseClient
from sync_to_readwise.core.source import Source

log = structlog.get_logger(__name__)


@dataclass
class SyncResult:
    source: str
    seen: int
    created: int
    skipped: int
    errors: int


class Syncer:
    """Pulls candidates from a Source and pushes new ones to Readwise."""

    def __init__(self, readwise: ReadwiseClient) -> None:
        self.readwise = readwise

    def sync(self, source: Source, source_cfg: SourceConfig) -> SyncResult:
        log.info("sync_started", source=source.name)

        # Warm the URL cache once (scoped to category=video for the YouTube case)
        # Sources that aren't videos can opt out by overriding warm_cache_category.
        warm_category = getattr(source, "readwise_category", None)
        self.readwise.warm_cache(category=warm_category)

        location = source_cfg.location or source.default_location
        tags = sorted({*source.default_tags, *source_cfg.tags})

        seen = created = skipped = errors = 0
        for item in source.fetch_candidates():
            seen += 1
            if self.readwise.exists(item.url):
                skipped += 1
                continue
            try:
                self.readwise.create_document(item, location=location, tags=tags)
                created += 1
                log.info(
                    "item_created",
                    source=source.name,
                    url=item.url,
                    title=item.title,
                )
            except Exception as e:
                errors += 1
                log.exception("item_create_failed", source=source.name, url=item.url, error=str(e))

        result = SyncResult(
            source=source.name, seen=seen, created=created, skipped=skipped, errors=errors
        )
        log.info("sync_completed", **result.__dict__)
        return result
