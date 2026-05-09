"""GitHub starred repositories → Readwise Reader.

Authenticated via a personal access token. Default scope is fine — the API
endpoint `/user/starred` returns public stars for any token; only stars on
private repos require the `repo` scope.
"""

from __future__ import annotations

from collections.abc import Iterable

import httpx
import structlog

from sync_to_readwise.core.item import Item
from sync_to_readwise.core.source import Source

log = structlog.get_logger(__name__)

GITHUB_API = "https://api.github.com"
PER_PAGE = 100


class GitHubStarsSource(Source):
    """Sync the authenticated user's starred repositories into Readwise."""

    name = "github_stars"
    default_location = "later"
    default_tags = ("github",)
    # Readwise categorizes ordinary web pages (including GitHub repo pages) as
    # `article`. Scoping the dedup cache to that category avoids paginating
    # through every video / book / pdf the user has saved.
    readwise_category = "article"

    def __init__(self, *, token: str) -> None:
        if not token:
            raise ValueError("GITHUB_TOKEN must be set (via Doppler or .env).")
        self._client = httpx.Client(
            base_url=GITHUB_API,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "sync-to-readwise",
            },
            timeout=30.0,
        )

    def fetch_candidates(self) -> Iterable[Item]:
        # Default sort=created (when starred) descending — newest stars first,
        # which matches the YouTube source's order and would let a future
        # short-circuit-on-known-URL optimization stop early on steady-state
        # syncs.
        url: str | None = "/user/starred"
        params: dict[str, str | int] | None = {"per_page": PER_PAGE, "sort": "created"}
        while url:
            r = self._client.get(url, params=params)
            r.raise_for_status()
            for repo in r.json():
                yield self._to_item(repo)
            url = _next_url(r.headers.get("link"))
            # Subsequent URLs from the Link header already carry the query
            # string, so don't re-pass params.
            params = None

    @staticmethod
    def _to_item(repo: dict) -> Item:
        owner = (repo.get("owner") or {}).get("login")
        return Item(
            url=repo["html_url"],
            source_name="github_stars",
            title=repo.get("full_name") or repo.get("name"),
            author=owner,
            summary=repo.get("description"),
        )


def _next_url(link_header: str | None) -> str | None:
    """Parse the GitHub Link header and return the URL with rel="next", if any.

    Format: ``<https://api.github.com/...>; rel="next", <...>; rel="last"``
    """
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        url = section[0].strip().strip("<>")
        rel = section[1].strip()
        if rel == 'rel="next"':
            return url
    return None
