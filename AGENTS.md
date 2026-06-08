# Agents

<!-- HUMAN-MAINTAINED - START -->

## Project principles

- This repo is public, never write non-public information in this repo
- Keep code and docs brief, for humans to read / understand / audit quickly
- Reuse and improve existing solutions / approaches / designs / helpers / tools / patterns,
  before adding new / similar ones
- Keep runtime dependencies minimal; justify new dependencies in code review
- Preserve unrelated user or agent edits in the worktree

## Standard commands

```sh
actionlint
npx --yes markdownlint-cli2@0.22.1
uv sync
uv run ruff format .
uv run ruff check .
uv run pyright
uv run python -m unittest discover -s tests
```

<!-- HUMAN-MAINTAINED - END -->

<!-- AGENT-MAINTAINED - START -->

## Toolchain

- Use `uv` for dependency management, virtualenv creation, and command running
- Use pyright in strict mode; fix linting / typing issues instead of suppressing them
- Use ruff for formatting, import sorting, and linting

## Runtime standards

- Configure the root logger by default (`logger_name=""`) so project modules
  and shared `src_py_lib` modules are captured by the same handlers
- Startup logs should include command, sanitized runtime config, commit when
  available, and log file path when applicable
- Use shared HTTP/client helpers for timeout policy, API error wrapping, and
  rate-limit handling

## Code organization

- Put importable package code under `src/`
- Put tests under `tests/`
- Keep module-level constants near the top of each module, after imports
- Prefer specific package/module names over broad `helpers` or `utils` modules

## Release process

- Package versions are derived from Git tags through `hatch-vcs`.
- `pyproject.toml` must use `dynamic = ["version"]`; do not add a hard-coded
  `project.version` for releases.
- The release tag must be `vMAJOR.MINOR.PATCH` and point at a commit reachable
  from `origin/main`.
- The release workflow builds from the tag and checks that wheel and source
  distribution filenames match the tag version before publishing.
- Do not make the release workflow edit `pyproject.toml` or `uv.lock`.
- Tag the remote head of `main` directly:

```sh
set -euo pipefail

VERSION_INPUT=<next-version>
VERSION="${VERSION_INPUT#v}"
GH_REPO="sourcegraph/src-py-lib"

[[ "${VERSION_INPUT}" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]]
git fetch origin --tags --prune
MAIN_COMMIT="$(git rev-parse origin/main)"
git tag -a "v${VERSION}" "${MAIN_COMMIT}" -m "Release v${VERSION}"
git push origin "v${VERSION}"

RUN_ID="$(
  gh run list \
    --repo "${GH_REPO}" \
    --workflow release.yml \
    --branch "v${VERSION}" \
    --limit 1 \
    --json databaseId \
    --jq '.[0].databaseId // empty'
)"
test -n "${RUN_ID}"
gh run watch "${RUN_ID}" --repo "${GH_REPO}" --exit-status
gh release view "v${VERSION}" --repo "${GH_REPO}"
uvx --from pip pip index versions src-py-lib
```

- If a pushed tag points at the wrong commit, move it only after explicit
  human approval.

## Before finishing changes

- Re-read edited files for organization and stale comments
- Update `README.md` when setup or user-facing behavior changes
- Update this `AGENTS.md` only with durable project-specific discoveries

<!-- AGENT-MAINTAINED - END -->
