"""Public interface for src-py-lib consumers."""

from __future__ import annotations

import sys
from contextlib import AbstractContextManager
from pathlib import Path

from src_py_lib.clients.github import GitHubClient, PullRequest, gh_cli_token, pr_ref_from_url
from src_py_lib.clients.google_sheets import (
    GoogleSheetsClient,
    GoogleSheetsError,
    gcloud_adc_access_token,
    quota_project_from_adc,
)
from src_py_lib.clients.graphql import (
    GraphQLClient,
    GraphQLError,
    aliased_batched_query,
    introspect_schema,
)
from src_py_lib.clients.linear import (
    LinearClient,
    LinearClientConfig,
    linear_client_from_config,
)
from src_py_lib.clients.slack import (
    SlackClient,
    SlackClientConfig,
    SlackError,
    SlackPacer,
    slack_client_from_config,
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
from src_py_lib.utils.http import HTTPClient, HTTPClientError
from src_py_lib.utils.json_cache import load_json_cache, load_json_subset, save_json_cache
from src_py_lib.utils.json_types import (
    JSONDict,
    json_dict,
    json_dicts,
    json_int,
    json_list,
    json_str,
    json_strs,
)
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
    "GraphQLClient",
    "GitHubClient",
    "GoogleSheetsClient",
    "GoogleSheetsError",
    "HTTPClient",
    "HTTPClientError",
    "JSONDict",
    "LinearClient",
    "LinearClientConfig",
    "LoggingConfig",
    "LoggingSettings",
    "PullRequest",
    "SlackClient",
    "SlackClientConfig",
    "SlackError",
    "SlackPacer",
    "aliased_batched_query",
    "config_field",
    "config_snapshot",
    "configure_logging",
    "critical",
    "debug",
    "error",
    "event",
    "gh_cli_token",
    "gcloud_adc_access_token",
    "info",
    "introspect_schema",
    "json_dict",
    "json_dicts",
    "json_int",
    "json_list",
    "json_str",
    "json_strs",
    "linear_client_from_config",
    "load_json_cache",
    "load_json_subset",
    "logging",
    "log",
    "log_context",
    "parse_args",
    "pr_ref_from_url",
    "quota_project_from_adc",
    "save_json_cache",
    "slack_client_from_config",
    "startup_event",
    "warning",
    "write_tsv",
]
