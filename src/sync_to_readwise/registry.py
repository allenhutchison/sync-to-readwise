"""Source registry. To add a new source, register it here."""

from __future__ import annotations

from collections.abc import Callable

from sync_to_readwise.core.config import AppConfig, SourceConfig
from sync_to_readwise.core.source import Source
from sync_to_readwise.sources.youtube import YouTubeLikesSource

SourceFactory = Callable[[AppConfig, SourceConfig], Source]


def _build_youtube(cfg: AppConfig, src_cfg: SourceConfig) -> Source:
    return YouTubeLikesSource(
        client_id=cfg.settings.youtube_oauth_client_id.get_secret_value(),
        client_secret=cfg.settings.youtube_oauth_client_secret.get_secret_value(),
        token_dir=cfg.data_dir,
    )


REGISTRY: dict[str, SourceFactory] = {
    "youtube": _build_youtube,
}


def build_source(name: str, cfg: AppConfig) -> Source:
    if name not in REGISTRY:
        raise KeyError(f"Unknown source: {name!r}. Registered: {sorted(REGISTRY)}")
    return REGISTRY[name](cfg, cfg.source_config(name))
