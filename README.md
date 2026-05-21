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
  JSONL events, run IDs, git commit metadata, context fields, event timing,
  retention, startup metadata, and sanitized config snapshots.
- `src_py_lib.utils.config` — Pydantic-backed `Config` models loaded from code
  defaults, `python-dotenv` `.env` parsing, shell environment, and CLI
  overrides, with typed values, required checks, safe snapshots, and `op://...`
  reference resolution.
- `src_py_lib.utils.http` — pooled `httpx` JSON HTTP client with a shared
  30-second timeout, retry policy, `Retry-After` support, and contextual errors.
- `src_py_lib.clients.graphql` — shared GraphQL execution with automatic cursor
  pagination, batched alias lookups, and schema introspection export.
- `src_py_lib.clients.sourcegraph` — Sourcegraph GraphQL client with token
  validation and shared config fields for `SRC_ENDPOINT` (default:
  `https://sourcegraph.com`) and `SRC_ACCESS_TOKEN`.
- `src_py_lib.clients.linear` — Linear GraphQL client with automatic cursor
  handling, token validation, shared config fields, and injectable HTTP policy.
- `src_py_lib.clients.slack` — Slack Web API client with token validation,
  cursor pagination, and method pacing. Consider `slack_sdk` if usage grows
  beyond simple GET, pagination, and rate-limit handling.
- `src_py_lib.clients.github` — GitHub GraphQL client, PR URL parsing, and
  batched PR lookups, with token validation. Defaults to `https://github.com`;
  pass `github_url` for GitHub Enterprise Server. Keep lightweight for GraphQL;
  GitHub SDKs help more for REST.
- `src_py_lib.clients.one_password` — tiny 1Password CLI wrapper for signing in,
  validating authenticated `op` access, and resolving `op://...` references after config loading.
- `src_py_lib.clients.google_sheets` — Google Sheets API primitives with
  spreadsheet access validation using gcloud Application Default Credentials or
  a provided access token. Prefer Google's official libraries if Sheets usage
  grows beyond small primitives, because auth, quota project, token refresh,
  batching, and error shapes are subtle.

Prefer this library for shared logging, HTTP policy, and thin API wrappers.
Prefer vendor SDKs when they replace tricky auth, token refresh, retries,
pagination, quota behavior, or complex request models.

## Example

Define one project-specific `Config` model, then load it once at CLI startup.

```python
from pathlib import Path

from src_py_lib.clients.linear import LinearClientConfig, linear_client_from_config
from src_py_lib.utils.config import config_field, config_parse_args


class LinearExportConfig(LinearClientConfig):
    output_dir: Path = config_field(
        Path("."),
        env_var="LINEAR_EXPORT_OUTPUT_DIR",
        cli_flag="--output-dir",
        metavar="PATH",
        help="Directory for generated files.",
    )

config = config_parse_args(LinearExportConfig, description="Export Linear data.")
client = linear_client_from_config(config)
print(f"Writing files under {config.output_dir}")
```

Config precedence is: code defaults, `.env`, shell environment, then CLI
overrides. API client modules can provide shared Config base classes such as
`LinearClientConfig`, and `config_parse_args` resolves `op://...` references by
default. Pass a custom `argparse.ArgumentParser` to `config_parse_args` when a
CLI also has non-Config flags. Mark sensitive fields with `secret=True` so
snapshots do not expose resolved values.

## Logging example

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
data = client.graphql("query Viewer { currentUser { username } }")
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
