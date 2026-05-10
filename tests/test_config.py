from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sync_to_readwise.core import config as config_mod
from sync_to_readwise.core.config import (
    AppConfig,
    Settings,
    SourceConfig,
    YamlConfig,
    load,
)


class TestSettings:
    def test_defaults_when_env_empty(self) -> None:
        s = Settings()
        assert s.log_level == "INFO"
        assert s.data_dir == Path("/data")
        assert s.readwise_token.get_secret_value() == ""
        assert s.github_token.get_secret_value() == ""

    def test_env_overrides_unprefixed_secrets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("READWISE_TOKEN", "rw-token")
        monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
        monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_ID", "cid")
        monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_SECRET", "csecret")

        s = Settings()
        assert s.readwise_token.get_secret_value() == "rw-token"
        assert s.github_token.get_secret_value() == "gh-token"
        assert s.youtube_oauth_client_id.get_secret_value() == "cid"
        assert s.youtube_oauth_client_secret.get_secret_value() == "csecret"

    def test_env_prefixed_internal_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SYNCRW_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("SYNCRW_DATA_DIR", "~/syncrw-data")

        s = Settings()
        assert s.log_level == "DEBUG"
        # Tilde expansion happens via the field validator.
        assert s.data_dir == Path.home() / "syncrw-data"
        assert s.data_dir.is_absolute()

    def test_data_dir_validator_passes_through_path_objects(self) -> None:
        # Direct construction with a Path bypasses the str-only branch.
        p = Path("/tmp/foo")
        s = Settings(data_dir=p)
        assert s.data_dir == p


class TestSourceConfig:
    def test_defaults(self) -> None:
        sc = SourceConfig()
        assert sc.enabled is True
        assert sc.interval_minutes == 15
        assert sc.location is None
        assert sc.tags == []

    def test_interval_minutes_must_be_positive(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SourceConfig(interval_minutes=0)

    def test_extra_fields_allowed(self) -> None:
        # Sources can stash arbitrary opt-in keys.
        sc = SourceConfig.model_validate({"enabled": True, "custom_key": "v"})
        assert sc.enabled is True


class TestAppConfig:
    def test_source_config_returns_default_for_unknown(self) -> None:
        cfg = AppConfig(settings=Settings(), yaml=YamlConfig())
        sc = cfg.source_config("missing")
        # Falls back to a fresh default rather than raising.
        assert sc == SourceConfig()

    def test_source_config_returns_configured_entry(self) -> None:
        ycfg = YamlConfig(sources={"youtube": SourceConfig(interval_minutes=42)})
        cfg = AppConfig(settings=Settings(), yaml=ycfg)
        assert cfg.source_config("youtube").interval_minutes == 42

    def test_data_dir_and_log_level_passthroughs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("SYNCRW_LOG_LEVEL", "WARNING")
        s = Settings(data_dir=tmp_path)
        cfg = AppConfig(settings=s, yaml=YamlConfig())
        assert cfg.data_dir == tmp_path
        assert cfg.log_level == "WARNING"


class TestLoad:
    def test_raises_when_readwise_token_missing(self) -> None:
        with pytest.raises(ValueError, match="READWISE_TOKEN"):
            load(yaml_path=None)

    def test_loads_with_no_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("READWISE_TOKEN", "rw")
        cfg = load(yaml_path=None)
        assert cfg.yaml.sources == {}
        assert cfg.settings.readwise_token.get_secret_value() == "rw"

    def test_loads_with_missing_yaml_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Path that doesn't exist is treated like "no overrides".
        monkeypatch.setenv("READWISE_TOKEN", "rw")
        cfg = load(yaml_path=tmp_path / "does-not-exist.yaml")
        assert cfg.yaml.sources == {}

    def test_loads_with_yaml(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("READWISE_TOKEN", "rw")
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(
            yaml.safe_dump(
                {
                    "sources": {
                        "youtube": {
                            "enabled": True,
                            "interval_minutes": 30,
                            "tags": ["mine"],
                        }
                    }
                }
            )
        )
        cfg = load(yaml_path=yaml_path)
        yt = cfg.source_config("youtube")
        assert yt.interval_minutes == 30
        assert yt.tags == ["mine"]

    def test_loads_with_empty_yaml_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # `yaml.safe_load` returns None for an empty file — load() must
        # fall back to {} so YamlConfig validation succeeds.
        monkeypatch.setenv("READWISE_TOKEN", "rw")
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text("")
        cfg = load(yaml_path=yaml_path)
        assert cfg.yaml.sources == {}


def test_module_constants() -> None:
    # Spot-check the public ReaderLocation alias is importable.
    assert "later" in config_mod.ReaderLocation.__args__  # type: ignore[attr-defined]
