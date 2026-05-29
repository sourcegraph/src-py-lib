# src-py-lib

Reusable libraries for Sourcegraph projects

## Experimental - This is not a supported Sourcegraph product

This repo was created for Sourcegraph Implementation Engineering deployments,
and is not intended, designed, built, or supported for use in any other scenario.
Feel free to open issues or PRs, but responses are best effort.

## Semantic Versioning

- Release versions are `major.minor.patch`
- Because this project is still major version 0:
  - Minor version updates are breaking changes
  - Patch version updates are not breaking changes

## Install

From PyPI:

```sh
uv add src-py-lib
```

From this repository:

```sh
uv add git+https://github.com/sourcegraph/src-py-lib.git
```

## Use it for

- Typed config models loaded from defaults, `.env`, environment variables, and CLI flags
- Root logger setup with terminal output, optional JSONL events, timing, and run metadata
- A shared `httpx` JSON client with timeouts, retries, `Retry-After`, and contextual errors
- Small helpers for TSV output, JSON caches, GraphQL pagination, and batched GraphQL queries
- Thin clients for Sourcegraph, Linear, Slack, GitHub, Google Sheets, and 1Password

Prefer vendor SDKs when they handle complex auth, token refresh, quota behavior,
pagination, retries, or request models better than a thin wrapper

## Quick example

Define a project config, parse it once, and configure logging at startup:

```python
from pathlib import Path

import src_py_lib as src


class LinearExportConfig(src.LinearClientConfig):
    output_dir: Path = src.config_field(
        default=Path("."),
        env_var="LINEAR_EXPORT_OUTPUT_DIR",
        cli_flag="--output-dir",
        metavar="PATH",
        help="Directory for generated files.",
    )

config = src.parse_args(LinearExportConfig, description="Export Linear data")

with src.logging(config):
    client = src.linear_client_from_config(config)
    client.validate()
    src.info("Starting export", output_dir=str(config.output_dir))
```

- Config precedence is code defaults, `.env`, shell environment, then CLI overrides
- `parse_args` resolves `op://...` references by default
- Mark sensitive fields with `secret=True` so config snapshots redact resolved values
- `src.logging()` configures the root logger by default so project code and `src_py_lib` internals
  share the same handlers
- Use `event()` for timed work, `stage()` for workflow context, and
  `info()` / `warning()` / `error()` for structured one-off events

## Development

```sh
uv sync
uv run ruff format .
uv run ruff check .
uv run pyright
uv run python -m unittest discover -s tests
npx --yes markdownlint-cli2
```
