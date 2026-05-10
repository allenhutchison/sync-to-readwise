from __future__ import annotations

import pytest

from sync_to_readwise.sources.github_stars import GitHubStarsSource, _next_url


class TestNextUrl:
    def test_none_when_header_missing(self) -> None:
        assert _next_url(None) is None
        assert _next_url("") is None

    def test_returns_next_url(self) -> None:
        h = (
            '<https://api.github.com/u/starred?page=2>; rel="next", '
            '<https://api.github.com/u/starred?page=10>; rel="last"'
        )
        assert _next_url(h) == "https://api.github.com/u/starred?page=2"

    def test_returns_none_when_no_next(self) -> None:
        h = (
            '<https://api.github.com/u/starred?page=1>; rel="prev", '
            '<https://api.github.com/u/starred?page=10>; rel="last"'
        )
        assert _next_url(h) is None

    def test_skips_malformed_section(self) -> None:
        # No semicolon → split yields a single element, which we skip.
        assert _next_url("<https://x>") is None


class TestGitHubStarsSource:
    def test_missing_token_raises(self) -> None:
        with pytest.raises(ValueError, match="GITHUB_TOKEN"):
            GitHubStarsSource(token="")

    def test_to_item_maps_repo_fields(self) -> None:
        repo = {
            "html_url": "https://github.com/o/r",
            "full_name": "o/r",
            "name": "r",
            "owner": {"login": "o"},
            "description": "desc",
        }
        item = GitHubStarsSource._to_item(repo)
        assert item.url == "https://github.com/o/r"
        assert item.title == "o/r"
        assert item.author == "o"
        assert item.summary == "desc"
        assert item.source_name == "github_stars"

    def test_to_item_falls_back_to_name_when_full_name_missing(self) -> None:
        repo = {
            "html_url": "https://github.com/o/r",
            "name": "r",
            "owner": {"login": "o"},
        }
        item = GitHubStarsSource._to_item(repo)
        assert item.title == "r"

    def test_to_item_handles_missing_owner(self) -> None:
        repo = {"html_url": "https://github.com/o/r", "full_name": "o/r"}
        item = GitHubStarsSource._to_item(repo)
        assert item.author is None

    def test_fetch_candidates_paginates(self, httpx_mock) -> None:
        # Page 1: link header points to page 2.
        httpx_mock.add_response(
            url="https://api.github.com/user/starred?per_page=100&sort=created",
            json=[
                {
                    "html_url": "https://github.com/a/b",
                    "full_name": "a/b",
                    "owner": {"login": "a"},
                    "description": "first",
                }
            ],
            headers={"link": '<https://api.github.com/user/starred?page=2>; rel="next"'},
        )
        # Page 2: no next link → loop terminates.
        httpx_mock.add_response(
            url="https://api.github.com/user/starred?page=2",
            json=[
                {
                    "html_url": "https://github.com/c/d",
                    "full_name": "c/d",
                    "owner": {"login": "c"},
                    "description": "second",
                }
            ],
        )

        src = GitHubStarsSource(token="ghp_xxx")
        items = list(src.fetch_candidates())
        assert [i.url for i in items] == [
            "https://github.com/a/b",
            "https://github.com/c/d",
        ]

    def test_fetch_candidates_single_page(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/user/starred?per_page=100&sort=created",
            json=[],
        )
        src = GitHubStarsSource(token="ghp_xxx")
        assert list(src.fetch_candidates()) == []

    def test_fetch_candidates_propagates_http_errors(self, httpx_mock) -> None:
        import httpx

        httpx_mock.add_response(
            url="https://api.github.com/user/starred?per_page=100&sort=created",
            status_code=401,
        )
        src = GitHubStarsSource(token="bad")
        with pytest.raises(httpx.HTTPStatusError):
            list(src.fetch_candidates())

    def test_class_metadata(self) -> None:
        # Lock down the dedup-cache scoping and default location/tags.
        assert GitHubStarsSource.name == "github_stars"
        assert GitHubStarsSource.default_location == "later"
        assert GitHubStarsSource.default_tags == ("github",)
        assert GitHubStarsSource.readwise_category == "article"
