from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sync_to_readwise.sources import youtube as youtube_mod
from sync_to_readwise.sources.youtube import (
    TOKEN_FILENAME,
    YOUTUBE_SCOPES,
    YouTubeLikesSource,
    _thumbnail_url,
)


def _src(tmp_path: Path) -> YouTubeLikesSource:
    return YouTubeLikesSource(
        client_id="cid",
        client_secret="csecret",
        token_dir=tmp_path,
    )


class TestConstructor:
    def test_missing_creds_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="YOUTUBE_OAUTH_CLIENT_ID"):
            YouTubeLikesSource(client_id="", client_secret="x", token_dir=tmp_path)
        with pytest.raises(ValueError, match="YOUTUBE_OAUTH_CLIENT_ID"):
            YouTubeLikesSource(client_id="x", client_secret="", token_dir=tmp_path)

    def test_token_path_assembled(self, tmp_path: Path) -> None:
        src = _src(tmp_path)
        assert src.token_path == tmp_path / TOKEN_FILENAME


class TestThumbnailUrl:
    def test_priority_order(self) -> None:
        snippet = {
            "thumbnails": {
                "default": {"url": "default.jpg"},
                "medium": {"url": "medium.jpg"},
                "high": {"url": "high.jpg"},
                "standard": {"url": "standard.jpg"},
                "maxres": {"url": "maxres.jpg"},
            }
        }
        assert _thumbnail_url(snippet) == "maxres.jpg"

    def test_falls_back_to_lower_quality(self) -> None:
        assert _thumbnail_url({"thumbnails": {"medium": {"url": "m.jpg"}}}) == "m.jpg"
        assert _thumbnail_url({"thumbnails": {"default": {"url": "d.jpg"}}}) == "d.jpg"

    def test_skips_entries_with_no_url(self) -> None:
        snippet = {
            "thumbnails": {
                "maxres": {},  # no url
                "high": {"url": "high.jpg"},
            }
        }
        assert _thumbnail_url(snippet) == "high.jpg"

    def test_returns_none_when_no_thumbnails(self) -> None:
        assert _thumbnail_url({}) is None
        assert _thumbnail_url({"thumbnails": {}}) is None


class TestToItem:
    def test_skips_when_no_video_id(self) -> None:
        entry = {"snippet": {"title": "x"}, "contentDetails": {}}
        assert YouTubeLikesSource._to_item(entry) is None

    def test_skips_private_video(self) -> None:
        entry = {
            "snippet": {"title": "Private video"},
            "contentDetails": {"videoId": "abc123"},
        }
        assert YouTubeLikesSource._to_item(entry) is None

    def test_skips_deleted_video(self) -> None:
        entry = {
            "snippet": {"title": "Deleted video"},
            "contentDetails": {"videoId": "abc123"},
        }
        assert YouTubeLikesSource._to_item(entry) is None

    def test_uses_resourceid_videoid_when_contentdetails_missing(self) -> None:
        entry = {
            "snippet": {
                "title": "T",
                "videoOwnerChannelTitle": "Ch",
                "resourceId": {"videoId": "xyz"},
                "thumbnails": {"high": {"url": "h.jpg"}},
            },
            "contentDetails": {"videoPublishedAt": "2026-01-01T00:00:00Z"},
        }
        item = YouTubeLikesSource._to_item(entry)
        assert item is not None
        assert item.url == "https://www.youtube.com/watch?v=xyz"
        assert item.title == "T"
        assert item.author == "Ch"
        assert item.published_date == "2026-01-01T00:00:00Z"
        assert item.image_url == "h.jpg"

    def test_full_entry(self) -> None:
        entry = {
            "snippet": {
                "title": "Some Video",
                "videoOwnerChannelTitle": "Channel",
                "thumbnails": {"maxres": {"url": "max.jpg"}},
            },
            "contentDetails": {
                "videoId": "vid42",
                "videoPublishedAt": "2026-02-02T00:00:00Z",
            },
        }
        item = YouTubeLikesSource._to_item(entry)
        assert item is not None
        assert item.url == "https://www.youtube.com/watch?v=vid42"


class TestClientConfig:
    def test_shape(self, tmp_path: Path) -> None:
        cfg = _src(tmp_path)._client_config()
        installed = cfg["installed"]
        assert installed["client_id"] == "cid"
        assert installed["client_secret"] == "csecret"
        assert installed["redirect_uris"] == ["http://localhost"]
        assert installed["token_uri"].startswith("https://oauth2.googleapis.com")


class TestRunOauthSetup:
    def test_runs_flow_and_saves(self, tmp_path: Path) -> None:
        creds = MagicMock()
        creds.to_json.return_value = json.dumps({"refresh_token": "r"})

        flow = MagicMock()
        flow.run_local_server.return_value = creds

        src = _src(tmp_path)
        with patch.object(youtube_mod, "InstalledAppFlow") as flow_cls:
            flow_cls.from_client_config.return_value = flow
            src.run_oauth_setup(port=9090, open_browser=True)

        flow_cls.from_client_config.assert_called_once_with(src._client_config(), YOUTUBE_SCOPES)
        flow.run_local_server.assert_called_once_with(
            host="localhost",
            bind_addr="0.0.0.0",
            port=9090,
            open_browser=True,
        )
        # Token persisted on disk.
        assert src.token_path.exists()
        assert json.loads(src.token_path.read_text()) == {"refresh_token": "r"}


class TestSaveCredentials:
    def test_creates_parent_and_chmods(self, tmp_path: Path) -> None:
        src = YouTubeLikesSource(
            client_id="cid",
            client_secret="csecret",
            token_dir=tmp_path / "nested" / "deeper",  # parent doesn't exist
        )
        creds = MagicMock()
        creds.to_json.return_value = json.dumps({"k": "v"})
        src._save_credentials(creds)

        assert src.token_path.exists()
        # 0o600 — owner read/write only.
        assert (src.token_path.stat().st_mode & 0o777) == 0o600


class TestLoadCredentials:
    def test_raises_when_file_missing(self, tmp_path: Path) -> None:
        src = _src(tmp_path)
        with pytest.raises(FileNotFoundError, match="setup youtube"):
            src._load_credentials()

    def test_overrides_client_creds_from_settings(self, tmp_path: Path) -> None:
        src = _src(tmp_path)
        # Stash a token JSON with stale client creds.
        src.token_path.write_text(
            json.dumps(
                {
                    "client_id": "stale",
                    "client_secret": "stale",
                    "refresh_token": "r",
                    "token": "t",
                }
            )
        )

        creds = MagicMock(valid=True, expired=False)

        with patch.object(youtube_mod, "Credentials") as credentials_cls:
            credentials_cls.from_authorized_user_info.return_value = creds
            src._load_credentials()

            data_passed = credentials_cls.from_authorized_user_info.call_args[0][0]
            # The stale stored creds get overwritten by the constructor args.
            assert data_passed["client_id"] == "cid"
            assert data_passed["client_secret"] == "csecret"

    def test_refresh_path_persists_new_token(self, tmp_path: Path) -> None:
        src = _src(tmp_path)
        src.token_path.write_text(json.dumps({"refresh_token": "r"}))

        creds = MagicMock()
        # Initially invalid + expired with refresh token → triggers refresh.
        creds.valid = False
        creds.expired = True
        creds.refresh_token = "r"
        creds.to_json.return_value = json.dumps({"refreshed": True})

        with (
            patch.object(youtube_mod, "Credentials") as credentials_cls,
            patch.object(youtube_mod, "Request") as request_cls,
        ):
            credentials_cls.from_authorized_user_info.return_value = creds
            src._load_credentials()
            creds.refresh.assert_called_once()
            request_cls.assert_called_once()  # Request() instantiated

        # New token persisted.
        assert json.loads(src.token_path.read_text()) == {"refreshed": True}

    def test_unrefreshable_raises(self, tmp_path: Path) -> None:
        src = _src(tmp_path)
        src.token_path.write_text(json.dumps({}))

        creds = MagicMock()
        creds.valid = False
        creds.expired = False  # Not expired but invalid → no refresh path
        creds.refresh_token = None

        with patch.object(youtube_mod, "Credentials") as credentials_cls:
            credentials_cls.from_authorized_user_info.return_value = creds
            with pytest.raises(RuntimeError, match="invalid and not refreshable"):
                src._load_credentials()


class TestGetLikesPlaylistId:
    def test_returns_likes_id(self) -> None:
        yt = MagicMock()
        yt.channels().list().execute.return_value = {
            "items": [{"contentDetails": {"relatedPlaylists": {"likes": "LL_xyz"}}}]
        }
        assert YouTubeLikesSource._get_likes_playlist_id(yt) == "LL_xyz"

    def test_raises_when_no_channel(self) -> None:
        yt = MagicMock()
        yt.channels().list().execute.return_value = {"items": []}
        with pytest.raises(RuntimeError, match="No YouTube channel"):
            YouTubeLikesSource._get_likes_playlist_id(yt)


class TestFetchCandidates:
    def test_paginates_and_filters(self, tmp_path: Path) -> None:
        src = _src(tmp_path)

        # Fake credentials skip the load path entirely.
        creds = MagicMock(valid=True)

        # Build a fake youtube client with the chained API the source uses.
        yt = MagicMock()
        yt.channels().list().execute.return_value = {
            "items": [{"contentDetails": {"relatedPlaylists": {"likes": "LL"}}}]
        }
        # Two pages of playlist items: one has a private video to skip.
        page1 = {
            "items": [
                {
                    "snippet": {
                        "title": "Real",
                        "videoOwnerChannelTitle": "Ch",
                        "thumbnails": {"high": {"url": "h.jpg"}},
                    },
                    "contentDetails": {"videoId": "v1", "videoPublishedAt": "2026-01-01"},
                },
                {
                    "snippet": {"title": "Private video"},
                    "contentDetails": {"videoId": "v2"},
                },
            ],
            "nextPageToken": "tok",
        }
        page2 = {
            "items": [
                {
                    "snippet": {"title": "Two", "thumbnails": {}},
                    "contentDetails": {"videoId": "v3"},
                }
            ]
            # No nextPageToken → loop terminates.
        }
        yt.playlistItems().list().execute.side_effect = [page1, page2]

        with (
            patch.object(src, "_load_credentials", return_value=creds),
            patch.object(youtube_mod, "build", return_value=yt) as build_fn,
        ):
            items = list(src.fetch_candidates())

        build_fn.assert_called_once_with("youtube", "v3", credentials=creds, cache_discovery=False)
        urls = [i.url for i in items]
        assert urls == [
            "https://www.youtube.com/watch?v=v1",
            "https://www.youtube.com/watch?v=v3",
        ]


def test_class_metadata() -> None:
    assert YouTubeLikesSource.name == "youtube"
    assert YouTubeLikesSource.default_location == "later"
    assert YouTubeLikesSource.default_tags == ("youtube",)
    assert YouTubeLikesSource.readwise_category == "video"
