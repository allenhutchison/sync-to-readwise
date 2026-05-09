from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Item:
    """A piece of content discovered by a Source, ready to push to Readwise."""

    url: str
    source_name: str
    title: str | None = None
    author: str | None = None
    summary: str | None = None
    published_date: str | None = None  # ISO 8601 if known
    tags: tuple[str, ...] = field(default_factory=tuple)
    image_url: str | None = None
