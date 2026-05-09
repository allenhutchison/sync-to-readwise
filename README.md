# sync-to-readwise

Pluggable syncer that pushes content from third-party sources into [Readwise Reader](https://readwise.io/read).

Built-in sources:
- **YouTube liked videos** — when you like a video on YouTube, it shows up in Reader for triage.
- **GitHub starred repositories** — when you star a repo, the README ends up in Reader.

## Architecture

```
┌──────────────┐     ┌────────────────┐     ┌────────────┐
│  Source(s)   │ ──▶ │     Syncer     │ ──▶ │  Readwise  │
│  (YouTube)   │     │  (dedup + push)│     │   Reader   │
└──────────────┘     └────────────────┘     └────────────┘
```

- `Source` is an interface — adding a new source (Reddit saved, GitHub stars, etc.) is one file plus a registry entry.
- The syncer queries Readwise to dedup; no local "what's been synced" state file.
- Runs as a long-lived Docker container with an internal scheduler.
- **Secrets live in [Doppler](https://www.doppler.com/)**: the Doppler CLI is installed in the image and the entrypoint wraps the command with `doppler run --` when `DOPPLER_TOKEN` is set, mirroring how [`pepper`](../pepper) does it.

## Secrets

These live in Doppler (project: `sync-to-readwise`). `READWISE_TOKEN` is required; the per-source secrets are required only for the sources you actually enable — sources whose credentials are missing get logged and skipped at startup, so an unused source is harmless.

| Secret                         | Used by         | Where it comes from                                              |
|--------------------------------|-----------------|------------------------------------------------------------------|
| `READWISE_TOKEN`               | core            | https://readwise.io/access_token                                 |
| `YOUTUBE_OAUTH_CLIENT_ID`      | `youtube`       | Google Cloud Console → Credentials → OAuth 2.0 Client (Desktop)  |
| `YOUTUBE_OAUTH_CLIENT_SECRET`  | `youtube`       | Same OAuth client                                                |
| `GITHUB_TOKEN`                 | `github_stars`  | GitHub → Settings → Developer settings → Personal access tokens. Default scope is fine for public stars; add `repo` if you star private repos. |

Non-secret config (intervals, locations, tags) lives in `data/config.yaml` so it's reviewable in git.

## Setup

### 1. Doppler

```bash
brew install dopplerhq/cli/doppler   # if not already installed
doppler login
doppler setup --project sync-to-readwise --config dev
```

> The local `doppler.yaml` that `doppler setup` writes is per-developer state and is gitignored. Each contributor runs `doppler setup` once after cloning.

Set the three secrets above:

```bash
doppler secrets set READWISE_TOKEN=...
doppler secrets set YOUTUBE_OAUTH_CLIENT_ID=...
doppler secrets set YOUTUBE_OAUTH_CLIENT_SECRET=...
```

Get a YouTube OAuth client first if you don't have one:
- [Google Cloud Console](https://console.cloud.google.com/) → create or select a project.
- APIs & Services → Library → enable **YouTube Data API v3**.
- APIs & Services → Credentials → Create Credentials → OAuth client ID → **Desktop app**.
- Copy the client ID + secret into Doppler (you can ignore the JSON download).
- OAuth consent screen: External + Testing, add your Google account as a test user.

### 2. Build and configure

```bash
git clone <this repo>
cd sync-to-readwise

mkdir -p data
cp config.example.yaml data/config.yaml   # tweak intervals/tags as desired

docker compose build
```

### 3. One-time YouTube OAuth dance

```bash
doppler run -- docker compose run --rm --service-ports sync-to-readwise \
    sync-to-readwise setup youtube
```

A browser window opens. Grant access; the redirect lands on `http://localhost:8080/...` and the refresh token is written to `data/youtube_token.json`.

> **Why `--service-ports`**: by default `docker compose run` doesn't publish ports. The OAuth redirect needs port 8080 reachable from your browser.

> **Note**: the refresh token is intentionally **not** stored in Doppler. It's only useful when paired with the OAuth client secret (which *is* in Doppler), so a leaked volume on its own can't refresh tokens. It's also machine state, not config.

If you'd rather run setup outside Docker:

```bash
pip install -e .
doppler run -- sync-to-readwise --config data/config.yaml setup youtube
```

### 4. Run the daemon (dev / homelab)

```bash
doppler run -- docker compose up -d
docker compose logs -f
```

The first run **backfills all** of your liked videos into Readwise (location: `later`, tag: `youtube`). Subsequent runs poll every 15 minutes for new likes.

### Production / homelab

The image is published to Docker Hub at [`allenhutchison/sync-to-readwise`](https://hub.docker.com/r/allenhutchison/sync-to-readwise) by GitHub Actions on every push to `main` (`latest` + short SHA) and version tag `vX.Y.Z` (semver tags). `docker-compose.prod.yml` is the deployment shape: the only host-side secret is a Doppler service token; the container fetches everything else at start.

```bash
DOPPLER_TOKEN=$(doppler configs tokens create homelab --plain \
    --project sync-to-readwise --config prod --max-age 90d)

DOPPLER_TOKEN="$DOPPLER_TOKEN" docker compose -f docker-compose.prod.yml pull
DOPPLER_TOKEN="$DOPPLER_TOKEN" docker compose -f docker-compose.prod.yml up -d
```

Pin a specific image with `SYNCRW_IMAGE_TAG=v1.2.3` (or a `sha-abc1234`) instead of `latest`.

### One-shot run

For testing or a manual backfill kick:

```bash
doppler run -- docker compose run --rm sync-to-readwise \
    sync-to-readwise sync-once youtube
```

## Adding a new source

1. Create `src/sync_to_readwise/sources/<name>.py` implementing `Source`:
   ```python
   class MySource(Source):
       name = "mysource"
       default_location = "new"
       default_tags = ("mysource",)

       def fetch_candidates(self) -> Iterable[Item]:
           ...
   ```
2. Add any new secrets to `Settings` in `core/config.py` (with `validation_alias` for the env var name) and to Doppler.
3. Register a factory in `src/sync_to_readwise/registry.py`.
4. Optionally add a section under `sources:` in `data/config.yaml`.

The syncer, scheduler, dedup, and CLI pick it up automatically.

## Configuration reference

`data/config.yaml`:

| Key                                | Type    | Default        | Notes                                           |
|------------------------------------|---------|----------------|-------------------------------------------------|
| `sources.<name>.enabled`           | bool    | `true`         | Disable a source without removing config.       |
| `sources.<name>.interval_minutes`  | int     | `15`           | How often to poll.                              |
| `sources.<name>.location`          | string  | source default | `new`, `later`, `shortlist`, `archive`, `feed`. |
| `sources.<name>.tags`              | list    | `[]`           | Added on top of source default tags.            |

Environment (Doppler / `.env`):

| Var                            | Required | Notes                                              |
|--------------------------------|----------|----------------------------------------------------|
| `READWISE_TOKEN`               | yes      | https://readwise.io/access_token                   |
| `YOUTUBE_OAUTH_CLIENT_ID`      | yes      | Used by the YouTube source.                        |
| `YOUTUBE_OAUTH_CLIENT_SECRET`  | yes      | "                                                  |
| `SYNCRW_LOG_LEVEL`             | no       | Default `INFO`.                                    |
| `SYNCRW_DATA_DIR`              | no       | Default `/data`.                                   |
| `DOPPLER_TOKEN`                | prod     | Service token; entrypoint calls `doppler run` when present.       |

## CI / publishing

- `.github/workflows/ci.yml`: ruff format check + ruff lint + Docker smoke (build the image and confirm the CLI dispatches inside it). Runs on every PR and push to `main`.
- `.github/workflows/publish.yml`: pushes to `allenhutchison/sync-to-readwise` on Docker Hub. Tags: `latest` + `sha-<short>` for `main` pushes; semver `X.Y.Z` / `X.Y` / `X` for `vX.Y.Z` git tags. Uses GHA layer cache.
- `.github/dependabot.yml`: weekly PRs for Python deps (uv), GitHub Actions, and Dockerfile base images.

To enable publishing, set these repository secrets in GitHub Settings → Secrets and variables → Actions:

| Secret               | Value                                                                |
|----------------------|----------------------------------------------------------------------|
| `DOCKERHUB_USERNAME` | Your Docker Hub username (`allenhutchison`).                          |
| `DOCKERHUB_TOKEN`    | A Docker Hub access token with read/write/delete on the repo.         |

## Notes

- Dedup queries Readwise (`category=video` for the YouTube source) on each sync to build an in-memory URL set, then `Source.fetch_candidates()` is checked against it. No local state file.
- We never re-`save` an existing URL, so triaging a video in Reader (moving it out of `later`, retagging) won't be undone by a later sync.
- Private and deleted videos in your liked list are skipped silently.
