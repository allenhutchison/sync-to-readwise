# Testing guidelines

Read by `audit-tests` and `code-review`. Free-form prose — edit freely.

## The suite

- `uv run pytest`. `addopts` already enforce `-ra --strict-markers --strict-config` plus coverage
  with **`--cov-fail-under=90`** — a PR that drops branch coverage below 90% fails CI. Treat
  coverage regressions as blocking, not advisory.
- Tests live in `tests/`, one module per unit under test (`test_karakeep.py`,
  `test_readwise.py`, ...). A new source gets its own `test_<source>.py`.
- Tests are grouped in `class Test<Thing>:` blocks with plain `def test_...(self) -> None:` methods.
  Test functions are annotated like production code.

## Isolation

- `tests/conftest.py` has an **autouse** `_isolate_settings_env` fixture that deletes every
  `Settings`-visible env var and `chdir`s into `tests/` so a developer's `.env` or Doppler shell can
  never leak into a run. **Adding a new env-driven setting means adding its name to
  `_SETTINGS_ENV_VARS`** — otherwise the suite becomes machine-dependent.
- Tests must not touch the network, the real `/data` dir, or the developer's home directory. Use
  `tmp_path` for anything that writes.

## Mocking

- **HTTP is mocked at the transport layer with `pytest-httpx`'s `httpx_mock` fixture** — build real
  response payloads and assert on the requests actually sent. This is the default and preferred
  pattern; it exercises the real client, URL building, headers, and pagination.
- `monkeypatch` is for env vars, filesystem paths, and clock/IO seams.
- `MagicMock` is acceptable for collaborators that aren't HTTP (e.g. a `ReadwiseClient` double when
  testing `Syncer`), but **do not mock the unit under test's own internals** — mocking away the code
  a test is supposed to cover is a finding.
- Payload builders (`_bookmark(...)` in `test_karakeep.py`) are the convention for constructing
  fixture data: keyword-only, with sensible defaults, overridden per test.

## What a test should assert

- Assert on observable behavior: the `Item`s produced, the requests issued, the counters in
  `SyncResult` — not on incidental internals.
- Every source needs coverage of: missing-credential `ValueError`s, the happy-path metadata mapping,
  pagination, and its skip/filter rules (e.g. archived bookmarks, the `no-sync` tag).
- Error paths count. A `try/except` added to production code without a test that drives it is a
  coverage gap worth flagging.

## TODO — needs a human

- Whether any test may hit a real service under an opt-in marker (today: none, and `--strict-markers`
  means a new marker must be registered in `pyproject.toml` first).
- Expectations for testing the `web/` status server beyond what `test_web.py` already does.
