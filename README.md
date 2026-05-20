# src-py-lib

Reusable libraries for Sourcegraph-adjacent Python projects

This repo is the shared implementation layer for patterns which get
rebuilt in separate scripts: API clients, HTTP retries/timeouts, structured logging,
etc.

## Experimental - This is not a supported Sourcegraph product

This repo was created for Sourcegraph Implementation Engineering deployments,
and is not intended, designed, built, or supported for use in any other scenario.
Feel free to open issues or PRs, but responses are best effort.

## Install from another project

```sh
uv add git+https://github.com/sourcegraph/src-py-lib.git
```

## What is included

- `src_py_lib.utils.logging` — centralized human stderr logs plus optional structured
  JSONL events, run IDs, context fields, event timing, retention, startup
  metadata, and sanitized config snapshots.
- `src_py_lib.utils.http` — stdlib JSON HTTP client with a shared 30-second timeout,
  retry policy, `Retry-After` support, and contextual errors.
- `src_py_lib.clients.sourcegraph` — Sourcegraph GraphQL client.
- `src_py_lib.clients.linear` — Linear GraphQL client and batched issue lookups.
- `src_py_lib.clients.slack` — Slack Web API client with cursor pagination and
  method pacing. Consider `slack_sdk` if usage grows beyond simple GET,
  pagination, and rate-limit handling.
- `src_py_lib.clients.github` — GitHub GraphQL client, PR URL parsing, and
  batched PR lookups. Defaults to `https://github.com`; pass `github_url` for
  GitHub Enterprise Server. Keep lightweight for GraphQL; GitHub SDKs help more
  for REST.
- `src_py_lib.clients.one_password` — tiny 1Password CLI wrapper for resolving
  `op://...` secret references after config loading.
- `src_py_lib.clients.google_sheets` — Google Sheets API primitives using gcloud
  Application Default Credentials or a provided access token. Prefer Google's
  official libraries if Sheets usage grows beyond small primitives, because
  auth, quota project, token refresh, batching, and error shapes are subtle.

Prefer this library for shared logging, HTTP policy, and thin API wrappers.
Prefer vendor SDKs when they replace tricky auth, token refresh, retries,
pagination, quota behavior, or complex request models.

## Example

Configure logging once at process startup. Prefer configuring the root logger
(`logger_name=""`, the default) so project modules and shared `src_py_lib` modules
such as `src_py_lib.utils.http` are captured by the same terminal and JSONL handlers.

```python
from pathlib import Path

from src_py_lib.clients.sourcegraph import SourcegraphClient
from src_py_lib.utils.logging import (
    LoggingConfig,
    configure_logging,
    default_event_file,
    startup_event,
)

event_file = configure_logging(
    LoggingConfig(
        logger_name="",
        event_file=default_event_file(Path("logs")),
    )
)
startup_event(command="sync", config={"src_token": "provided"}, event_file=event_file)

client = SourcegraphClient("https://sourcegraph.example.com", "token")
data = client.graphql("query Viewer { viewer { username } }")
```

## Development

```sh
uv sync
uv run ruff format .
uv run ruff check .
uv run pyright
uv run python -m unittest discover -s tests
npx --yes markdownlint-cli2
```
