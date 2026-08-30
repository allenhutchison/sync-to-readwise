from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from sync_to_readwise.core.config import SourceConfig
from sync_to_readwise.core.item import Item
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
    # Items actually pushed to Readwise this run. Carried so the daemon can
    # record them in the activity feed. repr=False keeps log lines readable.
    created_items: list[Item] = field(default_factory=list, repr=False)


class Syncer:
    """Pulls candidates from a Source and pushes new ones to Readwise."""

    def __init__(self, readwise: ReadwiseClient) -> None:
        self.readwise = readwise

    def sync(self, source: Source, source_cfg: SourceConfig) -> SyncResult:
        log.info("sync_started", source=source.name)

        # Brings the persistent URL store up to date. Incremental after the
        # first run, and shared by every source — dedup is a property of the
        # destination, not of who is syncing into it.
        self.readwise.warm_cache()

        location = source_cfg.location or source.default_location
        source_tags = {*source.default_tags, *source_cfg.tags}

        seen = created = skipped = errors = 0
        created_items: list[Item] = []
        for item in source.fetch_candidates():
            seen += 1
            if self.readwise.exists(item.url):
                skipped += 1
                continue
            try:
                tags = sorted({*source_tags, *item.tags})
                self.readwise.create_document(item, location=location, tags=tags)
                created += 1
                created_items.append(item)
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
            source=source.name,
            seen=seen,
            created=created,
            skipped=skipped,
            errors=errors,
            created_items=created_items,
        )
        log.info(
            "sync_completed",
            source=result.source,
            seen=seen,
            created=created,
            skipped=skipped,
            errors=errors,
        )
        return result
