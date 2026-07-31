# Repo invariants

Read by `audit-architecture`, which checks each diff against every rule listed here. **This is the
file that most needs your judgment** — the audit can only enforce what's written below. Everything
here was inferred from the code; nothing has been confirmed by a human yet.

## Confirmed by reading the code (verify these are actually the rules you want)

### 1. A new source is one file plus a registry entry — nothing else

`Source` (`core/source.py`) is the only extension point. A new source subclasses it, sets
`name` / `default_location` / `default_tags`, implements `fetch_candidates()`, and gets a
`_build_<name>` factory added to `REGISTRY` in `registry.py`. A source must **not** reach into the
syncer, the scheduler, or the CLI. If a diff adds source-specific branching outside
`sources/` + `registry.py`, that's a violation.

### 2. Registry factories are the only place secrets are unwrapped

`_build_*` factories call `.get_secret_value()` and pass plain `str` into the source constructor.
Source classes take plain strings and never see `Settings` or `AppConfig`. Keeps sources trivially
testable and keeps secret handling in one auditable place.

### 3. Dedup is Readwise-side; there is no local "already synced" store

`Syncer.sync` warms the Readwise URL cache then calls `readwise.exists(item.url)`. Deliberate: no
local state file to corrupt or lose on container replacement. A change that introduces a local
synced-URL ledger reverses an architectural decision and needs discussion, not a silent PR.
(`core/state.py` holds *runtime/activity* state for the status page — not sync dedup state. Don't
conflate them.)

### 4. One bad item must never abort a sync run

The per-item `try/except` in `Syncer.sync` increments `errors`, logs `item_create_failed`, and
continues. Don't narrow that to a bare `raise`, and don't widen the try block so a failure skips the
counter bookkeeping.

### 5. A source with missing credentials degrades, it does not crash the daemon

Sources whose secrets are absent are logged and skipped at startup. The daemon must keep running the
sources that *are* configured. Any change that makes a missing optional secret fatal is a
regression.

### 6. Secrets never reach logs, the status page, or YAML

Secrets are `SecretStr` sourced from env (Doppler). `data/config.yaml` is committed and reviewable —
nothing secret goes in it. The status page on :8088 is unauthenticated and publicly reachable on the
homelab network: **it must never render a token, a raw API response containing one, or a full
`Settings` dump.**

### 7. Every `Settings` env var is registered in the test isolation list

Adding a field to `Settings` requires adding its env var name to `_SETTINGS_ENV_VARS` in
`tests/conftest.py`. Skipping this makes the suite pass or fail depending on the developer's shell.

### 8. The CLI must import every source

`docker-smoke` in CI runs `sync-to-readwise --help`, which forces Click to discover every subcommand
and transitively import every source. An import-time error in a new source fails CI here even
without secrets — so keep source modules import-safe (no network or filesystem work at import time).

### 9. Deploy artifact is the Docker image; `data/` is the only writable path

The container runs as a long-lived process with an internal scheduler. Anything that must survive a
restart (YouTube refresh token, activity state) goes under `cfg.data_dir` (`/data` in the image,
a mounted volume). Writing outside `/data` at runtime will be lost on image update.

## TODO — needs a human

- [ ] **Confirm or correct each rule above.** They're inferred, not authored.
- [ ] Is the Readwise-side-dedup rule (#3) truly permanent, or is a local cache acceptable if the
      Readwise API gets expensive?
- [ ] What are the actual security expectations for the :8088 status page and the OAuth callback
      route it serves? Rule #6 states the conservative reading.
- [ ] Any invariant about `docker-compose.yml` vs `docker-compose.prod.yml` staying in sync.
- [ ] Any rule about `docs/index.html` (the GitHub Pages deep dive) needing an update when the
      architecture changes.
