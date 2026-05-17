"""Configuration: secrets from env (Doppler-injected), structured config from YAML.

The split mirrors how `pepper` does it:
  * Secrets and per-environment values live in Doppler. The Doppler CLI in the
    container entrypoint exports them as env vars at process start; locally,
    `doppler run -- <cmd>` does the same thing.
  * Non-secret per-source structured config (intervals, locations, tags, which
    sources to enable) lives in `data/config.yaml` so it's reviewable in git.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ReaderLocation = Literal["new", "later", "shortlist", "archive", "feed"]


class Settings(BaseSettings):
    """Secrets + simple env-driven config. Populated from env vars (Doppler).

    Third-party SDK env vars use their conventional unprefixed names so Doppler
    secrets line up with upstream docs. Internal vars are prefixed with
    `SYNCRW_` to avoid collisions.
    """

    model_config = SettingsConfigDict(
        env_prefix="SYNCRW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Internal (SYNCRW_-prefixed)
    log_level: str = "INFO"
    data_dir: Path = Path("/data")

    # Status web server (SYNCRW_-prefixed: SYNCRW_WEB_ENABLED, SYNCRW_WEB_PORT, ...)
    web_enabled: bool = True
    web_host: str = "0.0.0.0"
    web_port: int = Field(default=8088, ge=1, le=65535)
    # Public base URL the browser uses to reach the status page, e.g.
    # "http://chowda:8088". Used to build the OAuth redirect URI; must match an
    # authorized redirect URI on the Google "Web application" OAuth client.
    # Empty = derive it from the request's Host header.
    public_base_url: str = ""

    # Third-party tokens / OAuth (unprefixed conventional names)
    readwise_token: SecretStr = Field(default=SecretStr(""), validation_alias="READWISE_TOKEN")
    youtube_oauth_client_id: SecretStr = Field(
        default=SecretStr(""), validation_alias="YOUTUBE_OAUTH_CLIENT_ID"
    )
    youtube_oauth_client_secret: SecretStr = Field(
        default=SecretStr(""), validation_alias="YOUTUBE_OAUTH_CLIENT_SECRET"
    )
    github_token: SecretStr = Field(default=SecretStr(""), validation_alias="GITHUB_TOKEN")

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand(cls, v: object) -> object:
        if isinstance(v, str):
            return Path(os.path.expanduser(v))
        return v


class SourceConfig(BaseModel):
    """Per-source overrides loaded from YAML."""

    enabled: bool = True
    interval_minutes: int = Field(default=15, ge=1)
    location: ReaderLocation | None = None  # None = use source default
    tags: list[str] = Field(default_factory=list)  # added on top of source default tags

    model_config = {"extra": "allow"}


class YamlConfig(BaseModel):
    """The structured config from `data/config.yaml`."""

    sources: dict[str, SourceConfig] = Field(default_factory=dict)


class AppConfig(BaseModel):
    """Bundled view of everything the app needs at runtime."""

    settings: Settings
    yaml: YamlConfig

    model_config = {"arbitrary_types_allowed": True}

    def source_config(self, name: str) -> SourceConfig:
        return self.yaml.sources.get(name) or SourceConfig()

    @property
    def data_dir(self) -> Path:
        return self.settings.data_dir

    @property
    def log_level(self) -> str:
        return self.settings.log_level


def load(yaml_path: Path | None = None) -> AppConfig:
    """Load secrets from env (Doppler) and structured config from YAML.

    `yaml_path` is optional — missing file means "no per-source overrides", which
    is fine for the common case where defaults are used.
    """
    settings = Settings()  # type: ignore[call-arg]  # pydantic-settings reads env

    raw: dict[str, object] = {}
    if yaml_path is not None and yaml_path.exists():
        raw = yaml.safe_load(yaml_path.read_text()) or {}
    yaml_cfg = YamlConfig.model_validate(raw)

    if not settings.readwise_token.get_secret_value():
        raise ValueError(
            "READWISE_TOKEN is not set. Put it in Doppler (or .env for local dev). "
            "Get one at https://readwise.io/access_token."
        )

    return AppConfig(settings=settings, yaml=yaml_cfg)
