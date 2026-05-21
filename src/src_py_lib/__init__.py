"""Public interface for src-py-lib consumers."""

from __future__ import annotations

import sys
from contextlib import AbstractContextManager
from pathlib import Path

from src_py_lib.clients.graphql import GraphQLError, introspect_schema
from src_py_lib.clients.linear import (
    LinearClient,
    LinearClientConfig,
    linear_client_from_config,
)
from src_py_lib.utils.config import (
    Config,
    ConfigError,
    config_field,
    config_snapshot,
)
from src_py_lib.utils.config import (
    config_parse_args as parse_args,
)
from src_py_lib.utils.json_types import JSONDict, json_dict, json_list
from src_py_lib.utils.logging import (
    LoggingConfig,
    LoggingSettings,
    configure_logging,
    critical,
    debug,
    error,
    event,
    info,
    log,
    log_context,
    logging_context,
    startup_event,
    warning,
)
from src_py_lib.utils.tsv import write_tsv


def logging(
    config: object | None = None,
    *,
    command: str | None = None,
    git_cwd: Path | str | None = None,
    logging_config: LoggingSettings | None = None,
) -> AbstractContextManager[Path | None]:
    """Configure standard CLI logging and emit startup metadata."""
    return logging_context(
        command or _script_name(),
        config,
        git_cwd=git_cwd,
        logging_config=logging_config,
    )


def _script_name() -> str:
    return Path(sys.argv[0]).stem or "python"


__all__ = [
    "Config",
    "ConfigError",
    "GraphQLError",
    "JSONDict",
    "LinearClient",
    "LinearClientConfig",
    "LoggingConfig",
    "LoggingSettings",
    "config_field",
    "config_snapshot",
    "configure_logging",
    "critical",
    "debug",
    "error",
    "event",
    "info",
    "introspect_schema",
    "json_dict",
    "json_list",
    "linear_client_from_config",
    "logging",
    "log",
    "log_context",
    "parse_args",
    "startup_event",
    "warning",
    "write_tsv",
]
