from __future__ import annotations

import httpx
import pytest

from sync_to_readwise.core.config import SourceConfig
from sync_to_readwise.core.readwise import SYNC_LOCATIONS, ReadwiseClient
from sync_to_readwise.core.syncer import Syncer
from sync_to_readwise.sources.karakeep import KarakeepSource


def _bookmark(
    *,
    url: str = "https://example.com/article",
    archived: bool = False,
    tags: list[str] | None = None,
    content_type: str = "link",
) -> dict:
    return {
        "id": "bookmark-id",
        "archived": archived,
        "title": "Saved title",
        "summary": "AI summary",
        "tags": [
            {"id": f"tag-{index}", "name": name, "attachedBy": "human"}
            for index, name in enumerate(tags or [])
        ],
        "content": {
            "type": content_type,
            "url": url,
            "title": "Crawled title",
            "description": "Crawled description",
            "author": "Author",
            "datePublished": "2026-07-01T12:00:00.000Z",
            "imageUrl": "https://example.com/image.jpg",
        },
    }


class TestKarakeepSource:
    def test_missing_configuration_raises(self) -> None:
        with pytest.raises(ValueError, match="SYNCRW_KARAKEEP_URL"):
            KarakeepSource(base_url="", api_key="key")
        with pytest.raises(ValueError, match="KARAKEEP_API_KEY"):
            KarakeepSource(base_url="http://karakeep:3000", api_key="")

    def test_fetches_unarchived_bookmarks_and_maps_metadata(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=(
                "http://karakeep:3000/api/v1/bookmarks"
                "?archived=false&sortOrder=desc&limit=100&includeContent=false"
            ),
            json={"bookmarks": [_bookmark(tags=["python"])], "nextCursor": None},
        )

        source = KarakeepSource(base_url="http://karakeep:3000/", api_key="secret")
        items = list(source.fetch_candidates())

        assert len(items) == 1
        item = items[0]
        assert item.url == "https://example.com/article"
        assert item.title == "Saved title"
        assert item.summary == "AI summary"
        assert item.author == "Author"
        assert item.published_date == "2026-07-01T12:00:00.000Z"
        assert item.image_url == "https://example.com/image.jpg"
        assert item.tags == ("python",)
        request = httpx_mock.get_request()
        assert request.headers["Authorization"] == "Bearer secret"

    def test_existing_non_article_url_is_not_saved_again(
        self, httpx_mock, no_sleep: list[float]
    ) -> None:
        existing_url = "https://example.com/video"
        # The warm walks every saved location; the document lives in one of them.
        for location in SYNC_LOCATIONS:
            httpx_mock.add_response(
                url=f"https://readwise.io/api/v3/list/?location={location}",
                json={
                    "results": (
                        [{"source_url": existing_url, "category": "video"}]
                        if location == "new"
                        else []
                    ),
                    "nextPageCursor": None,
                },
            )
        httpx_mock.add_response(
            url=(
                "http://karakeep:3000/api/v1/bookmarks"
                "?archived=false&sortOrder=desc&limit=100&includeContent=false"
            ),
            json={"bookmarks": [_bookmark(url=existing_url)], "nextCursor": None},
        )

        source = KarakeepSource(base_url="http://karakeep:3000", api_key="secret")
        with ReadwiseClient("token") as readwise:
            result = Syncer(readwise).sync(source, SourceConfig())

        assert result.created == 0
        assert result.skipped == 1
        assert not httpx_mock.get_requests(method="POST")

    def test_paginates_with_next_cursor(self, httpx_mock) -> None:
        base_query = "archived=false&sortOrder=desc&limit=100&includeContent=false"
        httpx_mock.add_response(
            url=f"http://karakeep:3000/api/v1/bookmarks?{base_query}",
            json={"bookmarks": [_bookmark(url="https://one.example")], "nextCursor": "next"},
        )
        httpx_mock.add_response(
            url=f"http://karakeep:3000/api/v1/bookmarks?{base_query}&cursor=next",
            json={"bookmarks": [_bookmark(url="https://two.example")], "nextCursor": None},
        )

        source = KarakeepSource(base_url="http://karakeep:3000", api_key="secret")
        assert [item.url for item in source.fetch_candidates()] == [
            "https://one.example",
            "https://two.example",
        ]

    @pytest.mark.parametrize("content_type", ["text", "asset", "unknown"])
    def test_skips_non_link_bookmarks(self, content_type: str) -> None:
        source = KarakeepSource(base_url="http://karakeep:3000", api_key="secret")
        assert source._to_item(_bookmark(content_type=content_type)) is None

    def test_skips_archived_bookmarks_defensively(self) -> None:
        source = KarakeepSource(base_url="http://karakeep:3000", api_key="secret")
        assert source._to_item(_bookmark(archived=True)) is None

    def test_no_sync_tag_excludes_case_insensitively(self) -> None:
        source = KarakeepSource(base_url="http://karakeep:3000", api_key="secret")
        assert source._to_item(_bookmark(tags=["interesting", "NO-SYNC"])) is None

    def test_custom_no_sync_tags(self) -> None:
        source = KarakeepSource(
            base_url="http://karakeep:3000",
            api_key="secret",
            no_sync_tags=("private",),
        )
        assert source._to_item(_bookmark(tags=["private"])) is None
        assert source._to_item(_bookmark(tags=["no-sync"])) is not None

    def test_can_disable_tag_import(self) -> None:
        source = KarakeepSource(
            base_url="http://karakeep:3000",
            api_key="secret",
            import_tags=False,
        )
        item = source._to_item(_bookmark(tags=["python"]))
        assert item is not None
        assert item.tags == ()

    def test_falls_back_to_crawled_metadata(self) -> None:
        bookmark = _bookmark()
        bookmark["title"] = None
        bookmark["summary"] = None
        source = KarakeepSource(base_url="http://karakeep:3000", api_key="secret")
        item = source._to_item(bookmark)
        assert item is not None
        assert item.title == "Crawled title"
        assert item.summary == "Crawled description"

    def test_propagates_http_errors(self, httpx_mock) -> None:
        httpx_mock.add_response(status_code=401)
        source = KarakeepSource(base_url="http://karakeep:3000", api_key="bad")
        with pytest.raises(httpx.HTTPStatusError):
            list(source.fetch_candidates())

    def test_class_metadata(self) -> None:
        assert KarakeepSource.name == "karakeep"
        assert KarakeepSource.default_location == "later"
        assert KarakeepSource.default_tags == ("karakeep",)
        assert not hasattr(KarakeepSource, "readwise_category")
