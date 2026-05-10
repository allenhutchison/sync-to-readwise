from __future__ import annotations

import signal
from pathlib import Path

import click
import structlog
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from sync_to_readwise.core.config import AppConfig, SourceConfig, load
from sync_to_readwise.core.logging import configure_logging
from sync_to_readwise.core.readwise import ReadwiseClient
from sync_to_readwise.core.syncer import Syncer
from sync_to_readwise.registry import REGISTRY, build_source
from sync_to_readwise.sources.youtube import YouTubeLikesSource

log = structlog.get_logger(__name__)

DEFAULT_CONFIG_PATH = Path("/data/config.yaml")


def _load(config_path: Path) -> AppConfig:
    cfg = load(config_path)
    configure_logging(cfg.log_level)
    return cfg


@click.group()
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=DEFAULT_CONFIG_PATH,
    show_default=True,
    help="Path to config.yaml",
)
@click.pass_context
def main(ctx: click.Context, config_path: Path) -> None:
    """Sync content from third-party sources into Readwise Reader."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


@main.group()
def setup() -> None:
    """One-time setup commands (OAuth flows, etc.)."""


@setup.command("youtube")
@click.option("--port", type=int, default=8080, show_default=True)
@click.option(
    "--open-browser/--no-open-browser",
    default=False,
    show_default=True,
    help="Try to open the auth URL in a local browser. Off by default since "
    "this typically runs inside Docker without a browser.",
)
@click.pass_context
def setup_youtube(ctx: click.Context, port: int, open_browser: bool) -> None:
    """Run the YouTube OAuth flow and store a refresh token.

    Reads YOUTUBE_OAUTH_CLIENT_ID / YOUTUBE_OAUTH_CLIENT_SECRET from the
    environment (Doppler-injected). The resulting refresh token is written
    to <data_dir>/youtube_token.json.

    The command prints an auth URL — open it in your browser on the host
    machine, grant access, and the redirect to localhost:{port} will land
    back here and complete the flow.
    """
    cfg = _load(ctx.obj["config_path"])
    source = build_source("youtube", cfg)
    assert isinstance(source, YouTubeLikesSource)
    click.echo(f"Starting OAuth callback server on 0.0.0.0:{port}.")
    click.echo("Open the printed URL in your browser to authorize.")
    source.run_oauth_setup(port=port, open_browser=open_browser)
    click.echo(f"Done. Token saved to {source.token_path}")


@main.command("sync-once")
@click.argument("source_name")
@click.pass_context
def sync_once(ctx: click.Context, source_name: str) -> None:
    """Run a single sync of one source. Useful for testing and backfill."""
    cfg = _load(ctx.obj["config_path"])
    source = build_source(source_name, cfg)
    src_cfg = cfg.source_config(source_name)

    with ReadwiseClient(cfg.settings.readwise_token.get_secret_value()) as rw:
        syncer = Syncer(rw)
        result = syncer.sync(source, src_cfg)

    click.echo(
        f"{result.source}: seen={result.seen} created={result.created} "
        f"skipped={result.skipped} errors={result.errors}"
    )


@main.command("run")
@click.pass_context
def run_daemon(ctx: click.Context) -> None:
    """Run the long-lived scheduler. Each enabled source runs on its own interval.

    At startup we probe-build every source whose `enabled` flag is on. Sources
    whose constructors raise (typically: missing credentials) are logged as
    warnings and skipped. This lets you register a source in code without
    forcing every deployment to configure its credentials.
    """
    cfg = _load(ctx.obj["config_path"])

    candidates = {
        name: cfg.source_config(name) for name in REGISTRY if cfg.source_config(name).enabled
    }
    if not candidates:
        log.warning("no_sources_enabled", registered=sorted(REGISTRY))
        return

    enabled: dict[str, SourceConfig] = {}
    for name, src_cfg in candidates.items():
        try:
            build_source(name, cfg)
        except Exception as e:
            log.warning(
                "source_skipped",
                source=name,
                reason=type(e).__name__,
                detail=str(e),
            )
            continue
        enabled[name] = src_cfg

    if not enabled:
        log.warning("no_sources_runnable", registered=sorted(REGISTRY))
        return

    rw = ReadwiseClient(cfg.settings.readwise_token.get_secret_value())
    syncer = Syncer(rw)
    scheduler = BlockingScheduler()

    def _run_source(name: str) -> None:
        try:
            source = build_source(name, cfg)
            src_cfg = cfg.source_config(name)
            syncer.sync(source, src_cfg)
        except Exception:
            log.exception("scheduled_sync_failed", source=name)

    for name, src_cfg in enabled.items():
        trigger = IntervalTrigger(minutes=src_cfg.interval_minutes)
        scheduler.add_job(_run_source, trigger, args=[name], id=name)
        log.info("source_scheduled", source=name, interval_minutes=src_cfg.interval_minutes)
        scheduler.add_job(_run_source, args=[name], id=f"{name}-initial")

    def _shutdown(*_: object) -> None:
        log.info("shutdown_requested")
        scheduler.shutdown(wait=False)
        rw.close()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("daemon_started", sources=list(enabled))
    scheduler.start()


if __name__ == "__main__":
    main()
