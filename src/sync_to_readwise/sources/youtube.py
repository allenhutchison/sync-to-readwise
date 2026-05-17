from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

import structlog
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow, InstalledAppFlow
from googleapiclient.discovery import build

from sync_to_readwise.core.item import Item
from sync_to_readwise.core.source import Source

log = structlog.get_logger(__name__)

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
TOKEN_FILENAME = "youtube_token.json"
CALLBACK_PATH = "/auth/youtube/callback"


class YouTubeAuthError(RuntimeError):
    """The stored YouTube credentials are unusable and need re-authorization.

    Raised in place of a raw google-auth `RefreshError` so callers (the daemon,
    the status page) can recognize an auth failure and surface an actionable
    "re-authorize" message instead of a stack trace.
    """


class YouTubeLikesSource(Source):
    """Sync the authenticated user's YouTube 'liked videos' playlist into Readwise.

    OAuth client credentials (client_id + client_secret) come from Doppler/env
    via Settings; we never read a `client_secrets.json` file. The refresh
    token obtained from the one-time setup flow is persisted to a mounted
    volume — it's machine state, not config, and is useless on its own
    without the client secret.
    """

    name = "youtube"
    default_location = "later"
    default_tags = ("youtube",)
    readwise_category = "video"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        token_dir: Path,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError(
                "YOUTUBE_OAUTH_CLIENT_ID and YOUTUBE_OAUTH_CLIENT_SECRET must be set "
                "(via Doppler or .env)."
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self.token_path = token_dir / TOKEN_FILENAME

    # ---------- Auth ----------

    def _client_config(self) -> dict:
        """Build the dict that google-auth-oauthlib expects in place of a JSON file."""
        return {
            "installed": {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                "redirect_uris": ["http://localhost"],
            }
        }

    def _web_client_config(self, redirect_uri: str) -> dict:
        """Client config for the browser-driven (`web`) OAuth flow.

        Unlike the installed-app flow, this needs a Google Cloud OAuth client
        of type "Web application" with `redirect_uri` registered as an
        authorized redirect URI.
        """
        return {
            "web": {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                "redirect_uris": [redirect_uri],
            }
        }

    def web_authorization_url(self, redirect_uri: str) -> tuple[str, str]:
        """Begin the browser OAuth flow; return (google_consent_url, oauth_state).

        The caller redirects the user to the consent URL and must remember
        `oauth_state` to validate the matching callback.
        """
        # The status page is served over plain HTTP on a homelab host; oauthlib
        # otherwise refuses a non-HTTPS redirect URI.
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
        flow = Flow.from_client_config(
            self._web_client_config(redirect_uri),
            scopes=YOUTUBE_SCOPES,
            redirect_uri=redirect_uri,
        )
        auth_url, oauth_state = flow.authorization_url(
            access_type="offline", prompt="consent"
        )
        return auth_url, oauth_state

    def finish_web_authorization(self, redirect_uri: str, oauth_state: str, code: str) -> None:
        """Complete the browser OAuth flow: exchange `code` and persist the token."""
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
        flow = Flow.from_client_config(
            self._web_client_config(redirect_uri),
            scopes=YOUTUBE_SCOPES,
            state=oauth_state,
            redirect_uri=redirect_uri,
        )
        flow.fetch_token(code=code)
        self._save_credentials(flow.credentials)
        log.info("youtube_oauth_complete", token_path=str(self.token_path))

    def run_oauth_setup(self, *, port: int = 8080, open_browser: bool = False) -> None:
        """Run the interactive OAuth installed-app flow and persist the refresh token.

        Inside Docker we can't open the host's browser, and we need to bind the
        callback server on 0.0.0.0 so the container's port mapping is reachable
        from `localhost:<port>` on the host. The redirect URI registered with
        Google still uses `localhost`, which is where the user's browser will
        send the code.
        """
        flow = InstalledAppFlow.from_client_config(self._client_config(), YOUTUBE_SCOPES)
        creds = flow.run_local_server(
            host="localhost",
            bind_addr="0.0.0.0",
            port=port,
            open_browser=open_browser,
        )
        self._save_credentials(creds)
        log.info("youtube_oauth_complete", token_path=str(self.token_path))

    def _load_credentials(self) -> Credentials:
        if not self.token_path.exists():
            raise FileNotFoundError(
                f"YouTube token not found at {self.token_path}. "
                "Run `sync-to-readwise setup youtube` first."
            )
        data = json.loads(self.token_path.read_text())
        # Always use the current Doppler-supplied client credentials, not whatever
        # was stored alongside the token. Lets you rotate the OAuth client without
        # redoing the full consent flow.
        data["client_id"] = self._client_id
        data["client_secret"] = self._client_secret
        creds = Credentials.from_authorized_user_info(data, YOUTUBE_SCOPES)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except RefreshError as e:
                    # Most commonly: the refresh token expired (7-day limit for
                    # OAuth apps in "testing" mode) or was revoked.
                    raise YouTubeAuthError(
                        "YouTube refresh token expired or revoked. Re-authorize at "
                        f"{CALLBACK_PATH.rsplit('/', 1)[0]} or run `sync-to-readwise "
                        "setup youtube`."
                    ) from e
                self._save_credentials(creds)
            else:
                raise YouTubeAuthError(
                    "YouTube credentials invalid and not refreshable; re-authorize."
                )
        return creds

    def _save_credentials(self, creds: Credentials) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(creds.to_json())
        self.token_path.chmod(0o600)

    # ---------- Fetch ----------

    def fetch_candidates(self) -> Iterable[Item]:
        creds = self._load_credentials()
        yt = build("youtube", "v3", credentials=creds, cache_discovery=False)

        likes_playlist_id = self._get_likes_playlist_id(yt)
        log.info("youtube_likes_playlist", playlist_id=likes_playlist_id)

        page_token: str | None = None
        while True:
            resp = (
                yt.playlistItems()
                .list(
                    part="snippet,contentDetails",
                    playlistId=likes_playlist_id,
                    maxResults=50,
                    pageToken=page_token,
                )
                .execute()
            )
            for entry in resp.get("items", []):
                item = self._to_item(entry)
                if item is not None:
                    yield item

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    @staticmethod
    def _get_likes_playlist_id(yt) -> str:  # type: ignore[no-untyped-def]
        resp = yt.channels().list(part="contentDetails", mine=True).execute()
        items = resp.get("items", [])
        if not items:
            raise RuntimeError("No YouTube channel found for the authenticated account.")
        return items[0]["contentDetails"]["relatedPlaylists"]["likes"]

    @staticmethod
    def _to_item(entry: dict) -> Item | None:
        snippet = entry.get("snippet", {})
        details = entry.get("contentDetails", {})
        video_id = details.get("videoId") or snippet.get("resourceId", {}).get("videoId")
        if not video_id:
            return None

        # Skip private / deleted videos — title is "Private video" / "Deleted video"
        # and the URL isn't playable.
        title = snippet.get("title")
        if title in {"Private video", "Deleted video"}:
            return None

        return Item(
            url=f"https://www.youtube.com/watch?v={video_id}",
            source_name="youtube",
            title=title,
            author=snippet.get("videoOwnerChannelTitle"),
            published_date=details.get("videoPublishedAt"),
            image_url=_thumbnail_url(snippet),
        )


def _thumbnail_url(snippet: dict) -> str | None:
    thumbs = snippet.get("thumbnails", {})
    for key in ("maxres", "standard", "high", "medium", "default"):
        if key in thumbs and thumbs[key].get("url"):
            return thumbs[key]["url"]
    return None
