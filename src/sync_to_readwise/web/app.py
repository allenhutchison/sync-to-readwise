"""Request router for the status page.

`StatusApp.dispatch` is framework-free: it takes the parsed pieces of a request
and returns a `Response`. The only side effects are delegated to the injected
YouTube source (the OAuth dance), so the router is directly unit-testable
without opening a socket.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import structlog

from sync_to_readwise.core.state import SyncState
from sync_to_readwise.sources.youtube import CALLBACK_PATH, YouTubeLikesSource
from sync_to_readwise.web.render import render_message, render_status_page

log = structlog.get_logger(__name__)

_BACK = ("Back to status", "/")


@dataclass
class Response:
    status: int
    content_type: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


def _html(status: int, markup: str) -> Response:
    return Response(status, "text/html; charset=utf-8", markup.encode("utf-8"))


def _text(status: int, message: str) -> Response:
    return Response(status, "text/plain; charset=utf-8", message.encode("utf-8"))


class StatusApp:
    """Routes status-page requests against a `SyncState` and a YouTube source."""

    def __init__(
        self,
        *,
        state: SyncState,
        youtube: YouTubeLikesSource | None = None,
        public_base_url: str | None = None,
    ) -> None:
        self._state = state
        self._youtube = youtube
        self._public_base_url = public_base_url.rstrip("/") if public_base_url else None
        # oauth_state -> redirect_uri, recorded at /auth/youtube and consumed
        # by the matching callback to bind the two halves of the flow.
        self._pending: dict[str, str] = {}

    def dispatch(self, method: str, path: str, query: dict[str, str], host: str) -> Response:
        if method not in ("GET", "HEAD"):
            return _text(405, "method not allowed")
        if path == "/":
            return _html(200, render_status_page(self._state.snapshot()))
        if path == "/api/status":
            body = json.dumps(self._state.snapshot(), indent=2).encode("utf-8")
            return Response(200, "application/json", body)
        if path == "/healthz":
            return _text(200, "ok")
        if path == "/auth/youtube":
            return self._auth_start(host)
        if path == CALLBACK_PATH:
            return self._auth_callback(query, host)
        return _html(404, render_message("Not found", f"No route for {path}.", link=_BACK))

    # ---------- OAuth re-authorization ----------

    def _redirect_uri(self, host: str) -> str:
        # A configured public base URL wins (it must match what's registered in
        # Google Cloud); otherwise trust the Host header the browser used.
        base = self._public_base_url or f"http://{host}"
        return base + CALLBACK_PATH

    def _auth_start(self, host: str) -> Response:
        if self._youtube is None:
            return _html(
                503,
                render_message(
                    "YouTube not configured",
                    "Set YOUTUBE_OAUTH_CLIENT_ID and YOUTUBE_OAUTH_CLIENT_SECRET "
                    "to enable re-authorization.",
                    link=_BACK,
                ),
            )
        redirect_uri = self._redirect_uri(host)
        try:
            auth_url, oauth_state = self._youtube.web_authorization_url(redirect_uri)
        except Exception as e:
            log.exception("youtube_auth_start_failed")
            return _html(
                500, render_message("Could not start authorization", str(e), link=_BACK)
            )
        self._pending[oauth_state] = redirect_uri
        return Response(302, "text/plain; charset=utf-8", b"", headers={"Location": auth_url})

    def _auth_callback(self, query: dict[str, str], host: str) -> Response:
        if self._youtube is None:
            return _html(
                503, render_message("YouTube not configured", "Re-auth is unavailable.")
            )
        if query.get("error"):
            return _html(
                400, render_message("Authorization declined", query["error"], link=_BACK)
            )
        code = query.get("code")
        oauth_state = query.get("state")
        if not code or not oauth_state:
            return _html(
                400,
                render_message(
                    "Invalid callback", "The request is missing a code or state.", link=_BACK
                ),
            )
        redirect_uri = self._pending.pop(oauth_state, None)
        if redirect_uri is None:
            return _html(
                400,
                render_message(
                    "Expired authorization session",
                    "That authorization link is stale. Start over.",
                    link=("Re-authorize", "/auth/youtube"),
                ),
            )
        try:
            self._youtube.finish_web_authorization(redirect_uri, oauth_state, code)
        except Exception as e:
            log.exception("youtube_auth_finish_failed")
            return _html(
                500,
                render_message(
                    "Could not save credentials", str(e), link=("Try again", "/auth/youtube")
                ),
            )
        return _html(
            200,
            render_message(
                "YouTube re-authorized",
                "The new token is saved. The next scheduled sync will use it.",
                link=_BACK,
            ),
        )
