from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from sync_to_readwise import cli as cli_mod
from sync_to_readwise.cli import main
from sync_to_readwise.core.item import Item
from sync_to_readwise.core.source import Source


class _StubSource(Source):
    name = "stub"
    default_location = "later"
    default_tags = ("stub",)

    def fetch_candidates(self) -> Iterable[Item]:
        yield Item(url="https://e.example/1", source_name="stub")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("READWISE_TOKEN", "rw")
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_SECRET", "cs")
    monkeypatch.setenv("GITHUB_TOKEN", "gh")
    # Keep daemon state writes inside the test's tmp dir, and don't bind a
    # real web server during CLI tests — start_web_server gets its own tests.
    monkeypatch.setenv("SYNCRW_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SYNCRW_WEB_ENABLED", "false")
    return tmp_path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestSyncOnce:
    def test_happy_path(
        self,
        env: Path,
        runner: CliRunner,
    ) -> None:
        rw = MagicMock()
        rw.exists.return_value = False
        rw.__enter__.return_value = rw
        rw.__exit__.return_value = None

        with (
            patch.object(cli_mod, "ReadwiseClient", return_value=rw) as readwise_cls,
            patch.object(cli_mod, "build_source", return_value=_StubSource()),
        ):
            result = runner.invoke(
                main,
                ["--config", str(env / "missing.yaml"), "sync-once", "stub"],
            )

        assert result.exit_code == 0, result.output
        # Echoed result line includes the per-source counters.
        assert "stub:" in result.output
        assert "seen=1" in result.output
        assert "created=1" in result.output
        # ReadwiseClient was constructed with the env token.
        readwise_cls.assert_called_once_with("rw")

    def test_missing_token_fails(self, runner: CliRunner) -> None:
        # No READWISE_TOKEN in env → load() raises ValueError, surfaced by Click.
        result = runner.invoke(main, ["sync-once", "stub"])
        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)


class TestSetupYoutube:
    def test_invokes_oauth_setup(self, env: Path, runner: CliRunner) -> None:
        from sync_to_readwise.sources.youtube import YouTubeLikesSource

        src = MagicMock(spec=YouTubeLikesSource)
        src.token_path = env / "youtube_token.json"

        with patch.object(cli_mod, "build_source", return_value=src):
            result = runner.invoke(
                main,
                [
                    "--config",
                    str(env / "missing.yaml"),
                    "setup",
                    "youtube",
                    "--port",
                    "9090",
                    "--open-browser",
                ],
            )

        assert result.exit_code == 0, result.output
        src.run_oauth_setup.assert_called_once_with(port=9090, open_browser=True)
        assert "Done. Token saved" in result.output


class TestRunDaemon:
    def test_no_sources_enabled(
        self, env: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Empty registry → "no sources" branch executes and returns cleanly.
        monkeypatch.setattr(cli_mod, "REGISTRY", {})
        result = runner.invoke(main, ["--config", str(env / "missing.yaml"), "run"])
        assert result.exit_code == 0, result.output

    def test_no_sources_runnable_when_all_skip(
        self, env: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Registry has one entry whose factory always raises → it's skipped,
        # no_sources_runnable branch fires, command returns cleanly.
        monkeypatch.setattr(
            cli_mod,
            "REGISTRY",
            {"broken": lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("nope"))},
        )

        # Patch build_source to also raise for "broken".
        def _build(name: str, _cfg) -> Source:
            raise RuntimeError("nope")

        with patch.object(cli_mod, "build_source", side_effect=_build):
            result = runner.invoke(main, ["--config", str(env / "missing.yaml"), "run"])

        assert result.exit_code == 0, result.output

    def test_schedules_runnable_sources(
        self,
        env: Path,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # One broken, one good. The good one should be scheduled and the
        # initial trigger fires _run_source — which we exercise through
        # the captured callable. Note: the closure references the patched
        # build_source by name, so we must keep patches active when calling it.
        scheduler = MagicMock()
        captured_jobs: list[tuple] = []
        scheduler.add_job.side_effect = lambda *a, **kw: captured_jobs.append((a, kw))
        scheduler.start.return_value = None

        monkeypatch.setattr(cli_mod, "REGISTRY", {"good": MagicMock(), "broken": MagicMock()})

        def _build(name: str, _cfg):
            if name == "broken":
                raise RuntimeError("missing creds")
            return _StubSource()

        rw_instance = MagicMock()
        rw_instance.exists.return_value = False

        with (
            patch.object(cli_mod, "BlockingScheduler", return_value=scheduler),
            patch.object(cli_mod, "build_source", side_effect=_build),
            patch.object(cli_mod, "ReadwiseClient", return_value=rw_instance),
        ):
            result = runner.invoke(main, ["--config", str(env / "missing.yaml"), "run"])
            assert result.exit_code == 0, result.output

            # Two jobs queued for "good": the interval job + the immediate one.
            job_ids = [kw.get("id") for _, kw in captured_jobs if "id" in kw]
            assert "good" in job_ids
            assert len(captured_jobs) == 2
            scheduler.start.assert_called_once()

            # The first add_job call captured _run_source as its first arg.
            run_source_fn = captured_jobs[0][0][0]
            run_source_fn("good")  # drives sync without raising

        rw_instance.warm_cache.assert_called()

    def test_run_source_swallows_exception(
        self,
        env: Path,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Capture _run_source and verify the inner try/except logs but
        # doesn't propagate (so a transient failure doesn't kill the daemon).
        scheduler = MagicMock()
        captured: list = []
        scheduler.add_job.side_effect = lambda *a, **kw: captured.append((a, kw))

        with (
            patch.object(cli_mod, "BlockingScheduler", return_value=scheduler),
            patch.object(cli_mod, "REGISTRY", {"s": MagicMock()}),
        ):
            # First call (probe) succeeds; second call (job runtime) raises.
            calls = {"n": 0}

            def _build(name: str, _cfg):
                calls["n"] += 1
                if calls["n"] == 1:
                    return _StubSource()
                raise RuntimeError("transient")

            with (
                patch.object(cli_mod, "build_source", side_effect=_build),
                patch.object(cli_mod, "ReadwiseClient"),
            ):
                runner.invoke(main, ["--config", str(env / "missing.yaml"), "run"])

        # The first add_job call's first positional arg is the _run_source closure.
        run_source_fn = captured[0][0][0]
        # Should not raise even though build_source raises on this call.
        run_source_fn("s")

    def test_shutdown_handler_registered(
        self,
        env: Path,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Verify the SIGTERM/SIGINT handlers are wired and callable.
        scheduler = MagicMock()
        captured_signals: dict[int, object] = {}

        def _signal(signum, handler):
            captured_signals[signum] = handler

        with (
            patch.object(cli_mod, "BlockingScheduler", return_value=scheduler),
            patch.object(cli_mod.signal, "signal", side_effect=_signal),
            patch.object(cli_mod, "REGISTRY", {"s": MagicMock()}),
            patch.object(cli_mod, "build_source", return_value=_StubSource()),
            patch.object(cli_mod, "ReadwiseClient") as readwise_cls,
        ):
            readwise_cls.return_value = MagicMock()
            runner.invoke(main, ["--config", str(env / "missing.yaml"), "run"])

        import signal as _signal_mod

        assert _signal_mod.SIGTERM in captured_signals
        assert _signal_mod.SIGINT in captured_signals

        # Invoking the handler triggers scheduler.shutdown + rw.close.
        captured_signals[_signal_mod.SIGTERM]()
        scheduler.shutdown.assert_called_once_with(wait=False)


class TestNextRunAt:
    def test_returns_iso_for_datetime(self) -> None:
        from datetime import UTC, datetime

        scheduler = MagicMock()
        job = MagicMock()
        job.next_run_time = datetime(2026, 5, 17, 18, 0, 0, tzinfo=UTC)
        scheduler.get_job.return_value = job
        assert cli_mod._next_run_at(scheduler, "x") == "2026-05-17T18:00:00+00:00"

    def test_none_when_no_job(self) -> None:
        scheduler = MagicMock()
        scheduler.get_job.return_value = None
        assert cli_mod._next_run_at(scheduler, "x") is None

    def test_none_when_next_time_unset(self) -> None:
        scheduler = MagicMock()
        job = MagicMock()
        job.next_run_time = None
        scheduler.get_job.return_value = job
        assert cli_mod._next_run_at(scheduler, "x") is None


class TestStartWebServer:
    def _cfg_and_state(self, env: Path):
        from sync_to_readwise.core.config import load
        from sync_to_readwise.core.state import STATE_FILENAME, SyncState

        cfg = load(env / "missing.yaml")
        state = SyncState(env / STATE_FILENAME)
        return cfg, state

    def test_disabled_returns_none(self, env: Path) -> None:
        # The env fixture sets SYNCRW_WEB_ENABLED=false.
        cfg, state = self._cfg_and_state(env)
        assert cli_mod.start_web_server(cfg, state) is None

    def test_enabled_starts_server_with_youtube(
        self, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SYNCRW_WEB_ENABLED", "true")
        cfg, state = self._cfg_and_state(env)
        with patch.object(cli_mod, "serve_in_thread") as serve:
            cli_mod.start_web_server(cfg, state)
        serve.assert_called_once()
        app = serve.call_args[0][0]
        assert app._youtube is not None  # client creds present in env fixture

    def test_enabled_without_youtube_creds(
        self, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SYNCRW_WEB_ENABLED", "true")
        # No YouTube client creds → build_source raises → page serves anyway.
        monkeypatch.delenv("YOUTUBE_OAUTH_CLIENT_ID", raising=False)
        monkeypatch.delenv("YOUTUBE_OAUTH_CLIENT_SECRET", raising=False)
        cfg, state = self._cfg_and_state(env)
        with patch.object(cli_mod, "serve_in_thread") as serve:
            cli_mod.start_web_server(cfg, state)
        app = serve.call_args[0][0]
        assert app._youtube is None
