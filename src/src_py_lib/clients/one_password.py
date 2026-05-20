"""Small 1Password CLI client."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


class OnePasswordError(RuntimeError):
    """Raised when resolving a 1Password secret reference fails."""


@dataclass(frozen=True)
class OnePasswordClient:
    """Resolve `op://...` secret references through the `op` CLI."""

    op_binary: str = "op"

    def read(self, secret_ref: str) -> str:
        """Return the resolved secret value for one `op://...` reference."""
        try:
            result = subprocess.run(
                [self.op_binary, "read", secret_ref],
                check=True,
                text=True,
                capture_output=True,
            )
        except FileNotFoundError as exception:
            raise OnePasswordError("1Password CLI (`op`) was not found on PATH.") from exception
        except subprocess.CalledProcessError as exception:
            stderr = exception.stderr.strip()
            raise OnePasswordError(
                f"Failed to resolve 1Password reference: {stderr or exception}"
            ) from exception

        secret = result.stdout.strip()
        if not secret:
            raise OnePasswordError(f"`op read {secret_ref}` returned an empty value")
        return secret


def resolve_op_secret_ref(value: str, *, client: OnePasswordClient | None = None) -> str:
    """Resolve `value` if it is an `op://...` reference; otherwise return it.

    This is useful for config values that may be either a raw secret or a
    1Password reference. The resolved secret is returned, not logged.
    """
    stripped = value.strip()
    if not stripped:
        raise OnePasswordError("secret value is empty")
    if not stripped.startswith("op://"):
        return stripped
    return (client or OnePasswordClient()).read(stripped)
