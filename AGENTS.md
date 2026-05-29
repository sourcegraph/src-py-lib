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

- The tagged source commit must already contain the package version it
  releases. Do not make the release workflow edit `pyproject.toml`.
- The tag must be `vMAJOR.MINOR.PATCH`, and `.github/workflows/release.yml`
  verifies that it matches `project.version` before building GitHub release
  assets and publishing to PyPI.
- Prepare releases on a branch from current `main`. Set `VERSION`, then run:
- As part of every release bump, find old release-version literals in
  `AGENTS.md`, `README.md`, and release snippets, and replace them with the
  new version where they are meant to stay current.

```sh
set -euo pipefail

VERSION=0.1.6
BRANCH="release-v${VERSION}"

git fetch origin --tags --prune
git switch main
git pull --ff-only
git switch -c "${BRANCH}"

uv run python - "${VERSION}" <<'PY'
from pathlib import Path
import re
import sys

version = sys.argv[1]
path = Path("pyproject.toml")
text = path.read_text()
new_text = re.sub(
    r'(?m)^version = "[^"]+"$',
    f'version = "{version}"',
    text,
    count=1,
)
if new_text == text:
    raise SystemExit("pyproject.toml version was not updated")
path.write_text(new_text)
PY

uv lock
```

- Validate before opening the PR:

```sh
set -euo pipefail

uv lock --check
actionlint
npx --yes markdownlint-cli2@0.22.1
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run python -m unittest discover -s tests
uv build --wheel --sdist --out-dir /tmp/src-py-lib-release-check --no-create-gitignore
rm -rf /tmp/src-py-lib-release-check
```

- Commit, push, open the PR, wait for checks, then merge it. If review is
  required, stop after `gh pr checks` and ask for review before merging.

```sh
set -euo pipefail

VERSION=0.1.6
BRANCH="release-v${VERSION}"
GH_REPO="sourcegraph/src-py-lib"

git add pyproject.toml uv.lock
git commit -m "Release v${VERSION}"
git push -u origin "${BRANCH}"

gh pr create \
  --repo "${GH_REPO}" \
  --base main \
  --head "${BRANCH}" \
  --title "Release v${VERSION}" \
  --body "Bump src-py-lib package metadata to ${VERSION}."

gh pr checks "${BRANCH}" --repo "${GH_REPO}" --watch --fail-fast
gh pr merge "${BRANCH}" --repo "${GH_REPO}" --squash --delete-branch
```

- Tag the merged `main` commit. Do not tag a branch commit.

```sh
set -euo pipefail

VERSION=0.1.6

git fetch origin --tags --prune
git switch main
git pull --ff-only
git tag "v${VERSION}"
git push origin "v${VERSION}"
```

- Watch the release workflow and confirm the GitHub release and PyPI project.

```sh
set -euo pipefail

VERSION=0.1.6
GH_REPO="sourcegraph/src-py-lib"

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
