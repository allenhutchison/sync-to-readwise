# Coding guidelines

Read by `code-review` and `audit-architecture`. Free-form prose — edit freely; the audits are only
as good as what's written here.

## Style and tooling

- Ruff is the only formatter/linter. `line-length = 100`, `target-version = py311`, rule set
  `E, F, I, B, UP, N, SIM`. Don't add a second formatter or hand-format around ruff.
- Every module starts with `from __future__ import annotations`.
- Type-annotate public functions, methods, and dataclass fields. There is no mypy/pyright gate, so
  annotations are a review concern, not a CI one — treat a missing or wrong annotation as a real
  finding.
- Prefer modern typing: `X | None`, `list[str]`, `tuple[str, ...]`, `Literal[...]`,
  `collections.abc` imports over `typing` equivalents.
- Keyword-only constructor arguments (`def __init__(self, *, base_url: str, ...)`) for anything with
  more than one parameter — see `KarakeepSource` and `GitHubStarsSource`.

## Logging

- **`structlog` only. Never `print`, never bare `logging` calls in application code.** Each module
  gets `log = structlog.get_logger(__name__)` at module scope.
- Log events are **snake_case event names with structured kwargs**, not interpolated sentences:
  `log.info("item_created", source=source.name, url=item.url)` — not
  `log.info(f"created {item.url}")`.
- Use `log.exception(...)` inside `except` blocks so the traceback is captured.
- Logging is configured once by `core.logging.configure_logging()`; modules must not reconfigure it.
- **Never log a secret.** Tokens are `SecretStr`; don't put `.get_secret_value()` output into a log
  event, an error message, or a status-page field.

## Secrets and configuration

- The split is load-bearing: **secrets come from env (Doppler-injected), structured config comes from
  `data/config.yaml`.** Don't move a secret into YAML or a tunable into env without a deliberate
  decision.
- Every secret field on `Settings` is `SecretStr` with an explicit `validation_alias` for its
  conventional upstream name (`READWISE_TOKEN`, `GITHUB_TOKEN`, ...). Internal settings use the
  `SYNCRW_` prefix. New third-party token → `SecretStr` + unprefixed alias; new internal knob →
  `SYNCRW_`-prefixed.
- `.get_secret_value()` is called at the *construction boundary* (the registry factories), not
  scattered through source implementations.

## Error handling

- Missing required config raises `ValueError` at construction time with a message naming the env var
  the operator must set (`raise ValueError("KARAKEEP_API_KEY must be set (via Doppler or .env).")`).
  Fail loudly at startup rather than silently no-op'ing at sync time.
- A source whose credentials are absent is logged and skipped at startup — an unused source must
  never break the daemon.
- Per-item failures inside the sync loop are caught, counted into `SyncResult.errors`, logged with
  `log.exception`, and the loop continues. Do not let one bad item abort a whole sync run.
- Do not swallow an exception without logging it and reflecting it in a counter or return value.

## HTTP

- `httpx` for all outbound HTTP. Prefer a `with httpx.Client(...)` context manager in sources;
  `ReadwiseClient` deliberately holds a long-lived client because it's reused across runs.
- Always set an explicit timeout and paginate explicitly (`PAGE_SIZE` module constants), rather than
  relying on library defaults or unbounded loops.

## Comments

- Comments explain *why*, not *what* — the existing ones are a good model (e.g. why the refresh token
  is deliberately kept out of Doppler, why `repr=False` on `created_items`). Don't add narration.

## TODO — needs a human

- Any rule about the `web/` status server (templating, escaping, auth expectations) — not yet stated.
- Whether new dependencies need justification, and any policy on pinning.
