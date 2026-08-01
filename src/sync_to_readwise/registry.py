"""Source registry. To add a new source, register it here."""

from __future__ import annotations

from collections.abc import Callable

from sync_to_readwise.core.config import AppConfig, SourceConfig
from sync_to_readwise.core.source import Source
from sync_to_readwise.sources.github_stars import GitHubStarsSource
from sync_to_readwise.sources.karakeep import KarakeepOptions, KarakeepSource
from sync_to_readwise.sources.youtube import YouTubeLikesSource

SourceFactory = Callable[[AppConfig, SourceConfig], Source]


def _build_youtube(cfg: AppConfig, src_cfg: SourceConfig) -> Source:
    return YouTubeLikesSource(
        client_id=cfg.settings.youtube_oauth_client_id.get_secret_value(),
        client_secret=cfg.settings.youtube_oauth_client_secret.get_secret_value(),
        token_dir=cfg.data_dir,
    )


def _build_github_stars(cfg: AppConfig, src_cfg: SourceConfig) -> Source:
    return GitHubStarsSource(token=cfg.settings.github_token.get_secret_value())


def _build_karakeep(cfg: AppConfig, src_cfg: SourceConfig) -> Source:
    options = KarakeepOptions.model_validate(src_cfg.model_extra or {})
    return KarakeepSource(
        base_url=cfg.settings.karakeep_url,
        api_key=cfg.settings.karakeep_api_key.get_secret_value(),
        no_sync_tags=options.no_sync_tags,
        import_tags=options.import_tags,
    )


REGISTRY: dict[str, SourceFactory] = {
    "youtube": _build_youtube,
    "github_stars": _build_github_stars,
    "karakeep": _build_karakeep,
}


def build_source(name: str, cfg: AppConfig) -> Source:
    if name not in REGISTRY:
        raise KeyError(f"Unknown source: {name!r}. Registered: {sorted(REGISTRY)}")
    return REGISTRY[name](cfg, cfg.source_config(name))
