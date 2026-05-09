from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from sync_to_readwise.core.item import Item


class Source(ABC):
    """A pluggable content source. Implementations yield Items to be synced into Readwise."""

    name: str
    """Unique identifier used in config and as a default tag (e.g. 'youtube')."""

    default_location: str = "later"
    """Readwise Reader location: 'new', 'later', 'shortlist', 'archive', 'feed'."""

    default_tags: tuple[str, ...] = ()
    """Tags applied to every Item from this source (in addition to per-Item tags)."""

    @abstractmethod
    def fetch_candidates(self) -> Iterable[Item]:
        """Yield candidate items. May be lazy/paginated. The Syncer handles dedup."""
        ...
