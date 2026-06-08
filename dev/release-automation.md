# Release automation plan

## Goal

- Make publishing a release one command, which releases the current remote
head of `main`

```sh
./dev/release.sh <version>
```

- The script takes either `version` or `v<version>`
  - Verifies it's ahead of the latest release
  - Derives and pushes the `v<version>` release tag
  - Verifies the release, including the GHA workflow and PyPI

## Source of truth

`origin/main` plus the release tag is the release source of truth.

The release script should:

1. fetch `origin/main` and tags
2. resolve the exact `origin/main` commit to release
3. validate `<version>` as `1.2.3` or `v1.2.3`
4. strip a leading `v` if present, then derive `release_tag="v${version}"`
5. tag the resolved `origin/main` commit

Package builds use `hatch-vcs`, so the release tag supplies the package version.
The release script should not edit files, create release branches, or commit to
`main`.

## One-script flow

`./dev/release.sh <version>` should:

1. refuse dirty local release-script changes that would affect its own behavior
2. fetch `origin main --tags --prune`
3. reject versions that do not match `^v?[0-9]+\.[0-9]+\.[0-9]+$`
4. fail if an unprefixed tag like `<version>` exists
5. fail if `v<version>` already exists locally, on GitHub, or as a release
6. create annotated tag `v<version>` pointing at the `origin/main` commit
7. push only that tag
8. watch the release workflow for that tag
9. verify the GitHub Release has wheel, source distribution, and checksum assets
10. verify PyPI lists the new version

The only normal command that creates or pushes release tags should be this
script.

## Version policy

Keep release versions out of docs and copyable examples. `pyproject.toml` should
use `dynamic = ["version"]`; do not add a hard-coded `project.version` for a
release.

Good docs:

```sh
./dev/release.sh <version>
```

Avoid docs like:

```sh
VERSION=0.1.6
git tag "v${VERSION}"
```

## Workflow dispatch fallback

If the GitHub release workflow keeps a manual dispatch button, its input should
also be `version`, not `tag`.

```yaml
workflow_dispatch:
  inputs:
    version:
      description: "Package version, for example 1.2.3 or v1.2.3"
      required: true
      type: string
```

The workflow should derive `v<version>` internally and reject anything that is
not `MAJOR.MINOR.PATCH` or `vMAJOR.MINOR.PATCH`.

## Dependency policy

Keep this low-dependency. A stdlib Python script plus existing `uv`, `git`, and
`gh` commands should be enough. Do not add a release framework unless this script
becomes hard to maintain.
