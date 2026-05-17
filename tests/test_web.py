from __future__ import annotations

import http.client
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

from sync_to_readwise.core.state import STATE_FILENAME, SyncState
from sync_to_readwise.web import render
from sync_to_readwise.web.app import StatusApp
from sync_to_readwise.web.render import render_message, render_status_page
from sync_to_readwise.web.server import serve_in_thread

NOW = datetime(2026, 5, 17, 18, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------- render utils


class TestTimeHelpers:
    def test_parse_invalid_returns_none(self) -> None:
        assert render._parse(None) is None
        assert render._parse("") is None
        assert render._parse("not-a-date") is None

    def test_delta_str_units(self) -> None:
        assert render._delta_str(30) == "30 sec"
        assert render._delta_str(120) == "2 min"
        assert render._delta_str(7200) == "2 hr"
        assert render._delta_str(86400) == "1 day"
        assert render._delta_str(86400 * 3) == "3 days"

    def test_rel_past(self) -> None:
        assert render._rel_past(None, NOW) == "never"
        assert render._rel_past("2026-05-17T17:58:00+00:00", NOW) == "2 min ago"

    def test_rel_future(self) -> None:
        assert render._rel_future(None, NOW) == "—"
        assert render._rel_future("2026-05-17T17:59:00+00:00", NOW) == "due now"
        assert render._rel_future("2026-05-17T18:12:00+00:00", NOW) == "in 12 min"

    def test_uptime(self) -> None:
        assert render._uptime(None, NOW) == "—"
        assert render._uptime("2026-05-17T17:30:00+00:00", NOW) == "30m"
        assert render._uptime("2026-05-17T15:30:00+00:00", NOW) == "2h 30m"
        assert render._uptime("2026-05-11T04:00:00+00:00", NOW) == "6d 14h"


# ---------------------------------------------------------------- render pages


def _healthy_snapshot() -> dict:
    return {
        "daemon_started_at": "2026-05-11T04:00:00+00:00",
        "sources": {
            "github_stars": {
                "enabled": True,
                "interval_minutes": 15,
                "last_run_at": "2026-05-17T17:56:00+00:00",
                "last_success_at": "2026-05-17T17:56:00+00:00",
                "last_status": "ok",
                "last_error": None,
                "auth_failed": False,
                "last_result": {"seen": 145, "created": 2, "skipped": 143, "errors": 0},
                "next_run_at": "2026-05-17T18:11:00+00:00",
                "total_created": 12,
                "history": [0, 1, 0, 2, 0],
            }
        },
        "recent_events": [
            {
                "at": "2026-05-17T17:56:00+00:00",
                "kind": "ok",
                "source": "github_stars",
                "message": "seen 145, created 2, skipped 143",
            }
        ],
    }


class TestRenderStatusPage:
    def test_empty_state(self) -> None:
        html = render_status_page(
            {"daemon_started_at": None, "sources": {}, "recent_events": []}, now=NOW
        )
        assert "Waiting for" in html
        assert "No sources are enabled" in html
        assert "No activity recorded yet" in html

    def test_healthy(self) -> None:
        html = render_status_page(_healthy_snapshot(), now=NOW)
        assert "All channels" in html
        assert "operational" in html
        assert "github_stars" in html
        assert "6d 14h" in html

    def test_failing_with_auth_failure(self) -> None:
        snap = {
            "daemon_started_at": "2026-05-17T17:00:00+00:00",
            "sources": {
                "youtube": {
                    "enabled": True,
                    "interval_minutes": 15,
                    "last_run_at": "2026-05-17T17:59:00+00:00",
                    "last_success_at": None,
                    "last_status": "error",
                    "last_error": "YouTubeAuthError: token revoked",
                    "auth_failed": True,
                    "last_result": None,
                    "next_run_at": "2026-05-17T18:14:00+00:00",
                    "total_created": 0,
                    "history": [],
                }
            },
            "recent_events": [
                {
                    "at": "2026-05-17T17:59:00+00:00",
                    "kind": "error",
                    "source": "youtube",
                    "message": "YouTubeAuthError: token revoked",
                },
                {
                    "at": "2026-05-17T17:40:00+00:00",
                    "kind": "created",
                    "source": "youtube",
                    "message": "A Video",
                    "url": "https://youtu.be/v",
                },
            ],
        }
        html = render_status_page(snap, now=NOW)
        assert "needs attention" in html
        assert "re-authorization required" in html
        assert "/auth/youtube" in html
        assert "https://youtu.be/v" in html
        assert "token revoked" in html

    def test_enabled_count_excludes_disabled(self) -> None:
        snap = _healthy_snapshot()
        snap["sources"]["youtube"] = {
            **snap["sources"]["github_stars"],
            "enabled": False,
        }
        html = render_status_page(snap, now=NOW)
        # Two sources persisted, one disabled → header reads "1 enabled".
        assert "1 enabled" in html

    def test_multiple_failing(self) -> None:
        def _failing() -> dict:
            return {
                "enabled": True,
                "interval_minutes": 15,
                "last_run_at": "2026-05-17T17:59:00+00:00",
                "last_success_at": None,
                "last_status": "error",
                "last_error": "boom",
                "auth_failed": False,
                "last_result": None,
                "next_run_at": None,
                "total_created": 0,
                "history": [],
            }

        snap = {
            "daemon_started_at": "2026-05-17T17:00:00+00:00",
            "sources": {"youtube": _failing(), "github_stars": _failing()},
            "recent_events": [],
        }
        html = render_status_page(snap, now=NOW)
        assert "2 channels" in html

    def test_youtube_connected_credential_state(self) -> None:
        snap = {
            "daemon_started_at": "2026-05-17T17:00:00+00:00",
            "sources": {
                "youtube": {
                    "enabled": True,
                    "interval_minutes": 15,
                    "last_run_at": "2026-05-17T17:58:00+00:00",
                    "last_success_at": "2026-05-17T17:58:00+00:00",
                    "last_status": "ok",
                    "last_error": None,
                    "auth_failed": False,
                    "last_result": {"seen": 5, "created": 0, "skipped": 5, "errors": 0},
                    "next_run_at": "2026-05-17T18:13:00+00:00",
                    "total_created": 3,
                    "history": [0, 1, 0],
                }
            },
            "recent_events": [],
        }
        html = render_status_page(snap, now=NOW)
        assert "connected" in html
        assert "Last successful sync 2 min ago" in html

    def test_youtube_unverified_when_never_run(self) -> None:
        snap = {
            "daemon_started_at": "2026-05-17T17:00:00+00:00",
            "sources": {
                "youtube": {
                    "enabled": True,
                    "interval_minutes": 15,
                    "last_run_at": None,
                    "last_success_at": None,
                    "last_status": None,
                    "last_error": None,
                    "auth_failed": False,
                    "last_result": None,
                    "next_run_at": None,
                    "total_created": 0,
                    "history": [],
                }
            },
            "recent_events": [],
        }
        html = render_status_page(snap, now=NOW)
        assert "unverified" in html
        assert "awaiting first sync" in html

    def test_escapes_untrusted_text(self) -> None:
        snap = {
            "daemon_started_at": "2026-05-17T17:00:00+00:00",
            "sources": {},
            "recent_events": [
                {
                    "at": "2026-05-17T17:59:00+00:00",
                    "kind": "error",
                    "source": "x",
                    "message": "<script>alert(1)</script>",
                }
            ],
        }
        html = render_status_page(snap, now=NOW)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html


class TestRenderMessage:
    def test_with_link(self) -> None:
        html = render_message("All done", "It worked.", link=("Back", "/"))
        assert "All done" in html
        assert "It worked." in html
        assert 'href="/"' in html

    def test_without_link(self) -> None:
        html = render_message("Oops", "It failed.")
        assert "Oops" in html
        assert 'class="btn"' not in html  # no action button rendered


# ---------------------------------------------------------------- app router


def _app(
    tmp_path: Path,
    youtube: object | None = None,
    public_base_url: str | None = None,
) -> StatusApp:
    state = SyncState(tmp_path / STATE_FILENAME)
    return StatusApp(state=state, youtube=youtube, public_base_url=public_base_url)


class TestDispatchBasicRoutes:
    def test_status_page(self, tmp_path: Path) -> None:
        resp = _app(tmp_path).dispatch("GET", "/", {}, "host")
        assert resp.status == 200
        assert resp.content_type.startswith("text/html")
        assert b"status" in resp.body

    def test_api_status(self, tmp_path: Path) -> None:
        resp = _app(tmp_path).dispatch("GET", "/api/status", {}, "host")
        assert resp.status == 200
        assert resp.content_type == "application/json"
        assert "sources" in json.loads(resp.body)

    def test_healthz(self, tmp_path: Path) -> None:
        resp = _app(tmp_path).dispatch("GET", "/healthz", {}, "host")
        assert resp.status == 200
        assert resp.body == b"ok"

    def test_method_not_allowed(self, tmp_path: Path) -> None:
        assert _app(tmp_path).dispatch("POST", "/", {}, "host").status == 405

    def test_not_found(self, tmp_path: Path) -> None:
        assert _app(tmp_path).dispatch("GET", "/nope", {}, "host").status == 404


class TestAuthStart:
    def test_no_youtube_returns_503(self, tmp_path: Path) -> None:
        assert _app(tmp_path).dispatch("GET", "/auth/youtube", {}, "host").status == 503

    def test_redirects_to_google(self, tmp_path: Path) -> None:
        yt = MagicMock()
        yt.web_authorization_url.return_value = ("https://accounts.google/auth", "STATE1")
        app = _app(tmp_path, youtube=yt)
        resp = app.dispatch("GET", "/auth/youtube", {}, "chowda:8080")
        assert resp.status == 302
        assert resp.headers["Location"] == "https://accounts.google/auth"
        yt.web_authorization_url.assert_called_once_with("http://chowda:8080/auth/youtube/callback")
        assert app._pending["STATE1"] == "http://chowda:8080/auth/youtube/callback"

    def test_uses_public_base_url_over_host(self, tmp_path: Path) -> None:
        yt = MagicMock()
        yt.web_authorization_url.return_value = ("u", "s")
        app = _app(tmp_path, youtube=yt, public_base_url="http://chowda:8080/")
        app.dispatch("GET", "/auth/youtube", {}, "ignored-host")
        yt.web_authorization_url.assert_called_once_with("http://chowda:8080/auth/youtube/callback")

    def test_error_returns_500(self, tmp_path: Path) -> None:
        yt = MagicMock()
        yt.web_authorization_url.side_effect = RuntimeError("boom")
        resp = _app(tmp_path, youtube=yt).dispatch("GET", "/auth/youtube", {}, "host")
        assert resp.status == 500


class TestAuthCallback:
    def _started_app(self, tmp_path: Path) -> tuple[StatusApp, MagicMock]:
        yt = MagicMock()
        yt.web_authorization_url.return_value = ("u", "STATE1")
        app = _app(tmp_path, youtube=yt)
        app.dispatch("GET", "/auth/youtube", {}, "chowda")  # populates _pending
        return app, yt

    def test_success(self, tmp_path: Path) -> None:
        app, yt = self._started_app(tmp_path)
        resp = app.dispatch(
            "GET", "/auth/youtube/callback", {"code": "CODE", "state": "STATE1"}, "chowda"
        )
        assert resp.status == 200
        yt.finish_web_authorization.assert_called_once_with(
            "http://chowda/auth/youtube/callback", "STATE1", "CODE"
        )
        assert "STATE1" not in app._pending  # consumed

    def test_no_youtube_returns_503(self, tmp_path: Path) -> None:
        resp = _app(tmp_path).dispatch(
            "GET", "/auth/youtube/callback", {"code": "c", "state": "s"}, "host"
        )
        assert resp.status == 503

    def test_error_param(self, tmp_path: Path) -> None:
        resp = _app(tmp_path, youtube=MagicMock()).dispatch(
            "GET", "/auth/youtube/callback", {"error": "access_denied"}, "host"
        )
        assert resp.status == 400

    def test_missing_params(self, tmp_path: Path) -> None:
        resp = _app(tmp_path, youtube=MagicMock()).dispatch(
            "GET", "/auth/youtube/callback", {}, "host"
        )
        assert resp.status == 400

    def test_unknown_state(self, tmp_path: Path) -> None:
        resp = _app(tmp_path, youtube=MagicMock()).dispatch(
            "GET", "/auth/youtube/callback", {"code": "c", "state": "stale"}, "host"
        )
        assert resp.status == 400

    def test_finish_failure_returns_500(self, tmp_path: Path) -> None:
        app, yt = self._started_app(tmp_path)
        yt.finish_web_authorization.side_effect = RuntimeError("disk full")
        resp = app.dispatch(
            "GET", "/auth/youtube/callback", {"code": "c", "state": "STATE1"}, "chowda"
        )
        assert resp.status == 500


# ---------------------------------------------------------------- server glue


class TestServer:
    def test_serves_requests(self, tmp_path: Path) -> None:
        server = serve_in_thread(_app(tmp_path), "127.0.0.1", 0)
        try:
            port = server.server_address[1]
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)

            conn.request("GET", "/healthz")
            resp = conn.getresponse()
            assert resp.status == 200
            assert resp.read() == b"ok"

            conn.request("GET", "/")
            resp = conn.getresponse()
            assert resp.status == 200
            assert b"status" in resp.read()

            conn.request("HEAD", "/")
            resp = conn.getresponse()
            assert resp.status == 200
            assert resp.read() == b""  # HEAD: headers only

            conn.close()
        finally:
            server.shutdown()
            server.server_close()
