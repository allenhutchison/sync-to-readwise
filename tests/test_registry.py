from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from sync_to_readwise.core.config import AppConfig, Settings, YamlConfig
from sync_to_readwise.registry import REGISTRY, build_source
from sync_to_readwise.sources.github_stars import GitHubStarsSource
from sync_to_readwise.sources.youtube import YouTubeLikesSource


def _cfg(tmp_path: Path) -> AppConfig:
    settings = Settings(
        readwise_token=SecretStr("rw"),
        github_token=SecretStr("ghp_xxx"),
        youtube_oauth_client_id=SecretStr("cid"),
        youtube_oauth_client_secret=SecretStr("csecret"),
        data_dir=tmp_path,
    )
    return AppConfig(settings=settings, yaml=YamlConfig())


def test_registry_lists_known_sources() -> None:
    assert set(REGISTRY) == {"youtube", "github_stars"}


def test_build_youtube(tmp_path: Path) -> None:
    src = build_source("youtube", _cfg(tmp_path))
    assert isinstance(src, YouTubeLikesSource)
    # Token path is wired from settings.data_dir.
    assert src.token_path == tmp_path / "youtube_token.json"


def test_build_github_stars(tmp_path: Path) -> None:
    src = build_source("github_stars", _cfg(tmp_path))
    assert isinstance(src, GitHubStarsSource)


def test_build_unknown_source_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError, match="Unknown source"):
        build_source("nope", _cfg(tmp_path))


def test_build_youtube_without_creds_raises(tmp_path: Path) -> None:
    settings = Settings(readwise_token=SecretStr("rw"), data_dir=tmp_path)
    cfg = AppConfig(settings=settings, yaml=YamlConfig())
    with pytest.raises(ValueError, match="YOUTUBE_OAUTH_CLIENT_ID"):
        build_source("youtube", cfg)


def test_build_github_stars_without_token_raises(tmp_path: Path) -> None:
    settings = Settings(readwise_token=SecretStr("rw"), data_dir=tmp_path)
    cfg = AppConfig(settings=settings, yaml=YamlConfig())
    with pytest.raises(ValueError, match="GITHUB_TOKEN"):
        build_source("github_stars", cfg)
