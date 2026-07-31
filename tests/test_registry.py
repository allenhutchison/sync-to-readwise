from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from sync_to_readwise.core.config import AppConfig, Settings, SourceConfig, YamlConfig
from sync_to_readwise.registry import REGISTRY, build_source
from sync_to_readwise.sources.github_stars import GitHubStarsSource
from sync_to_readwise.sources.karakeep import KarakeepSource
from sync_to_readwise.sources.youtube import YouTubeLikesSource


def _cfg(tmp_path: Path) -> AppConfig:
    settings = Settings(
        readwise_token=SecretStr("rw"),
        github_token=SecretStr("ghp_xxx"),
        karakeep_url="http://karakeep:3000",
        karakeep_api_key=SecretStr("kk"),
        youtube_oauth_client_id=SecretStr("cid"),
        youtube_oauth_client_secret=SecretStr("csecret"),
        data_dir=tmp_path,
    )
    return AppConfig(settings=settings, yaml=YamlConfig())


def test_registry_lists_known_sources() -> None:
    assert set(REGISTRY) == {"youtube", "github_stars", "karakeep"}


def test_build_youtube(tmp_path: Path) -> None:
    src = build_source("youtube", _cfg(tmp_path))
    assert isinstance(src, YouTubeLikesSource)
    # Token path is wired from settings.data_dir.
    assert src.token_path == tmp_path / "youtube_token.json"


def test_build_github_stars(tmp_path: Path) -> None:
    src = build_source("github_stars", _cfg(tmp_path))
    assert isinstance(src, GitHubStarsSource)


def test_build_karakeep(tmp_path: Path) -> None:
    src = build_source("karakeep", _cfg(tmp_path))
    assert isinstance(src, KarakeepSource)


def test_build_karakeep_with_options(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.yaml.sources["karakeep"] = SourceConfig.model_validate(
        {"no_sync_tags": ["private"], "import_tags": False}
    )
    src = build_source("karakeep", cfg)
    assert isinstance(src, KarakeepSource)
    assert src._no_sync_tags == {"private"}
    assert src._import_tags is False


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


def test_build_karakeep_without_config_raises(tmp_path: Path) -> None:
    settings = Settings(readwise_token=SecretStr("rw"), data_dir=tmp_path)
    cfg = AppConfig(settings=settings, yaml=YamlConfig())
    with pytest.raises(ValueError, match="SYNCRW_KARAKEEP_URL"):
        build_source("karakeep", cfg)
