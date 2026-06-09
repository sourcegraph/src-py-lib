"""Public interface for src-py-lib consumers."""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

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
    stream_connection_nodes,
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
from src_py_lib.clients.sourcegraph import (
    SourcegraphClient,
    SourcegraphClientConfig,
    SourcegraphJaegerTraceError,
    SourcegraphJaegerTraceSummary,
    SourcegraphTrace,
    decode_external_service_id,
    decode_repository_id,
    decode_sourcegraph_node_id,
    encode_repository_id,
    encode_sourcegraph_node_id,
    normalize_sourcegraph_endpoint,
    sourcegraph_client_from_config,
)
from src_py_lib.utils.config import (
    Config,
    ConfigError,
    config_field,
    config_field_names,
    config_help_formatter,
    config_snapshot,
)
from src_py_lib.utils.config import (
    config_parse_args as parse_args,
)
from src_py_lib.utils.http import HTTPClient, HTTPClientError, HTTPResponse
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
    info,
    log_context,
    log_event,
    logging_context,
    logging_settings_from_config,
    resolve_log_level_name,
    span,
    stage,
    startup_event,
    submit_with_log_context,
    warning,
)
from src_py_lib.utils.telemetry import (
    OpenTelemetryConfig,
    OpenTelemetryRuntime,
    OpenTelemetrySettings,
    OpenTelemetrySetupError,
    configure_open_telemetry,
    current_traceparent_header,
    open_telemetry_settings_from_config,
    traceparent_fields,
)
from src_py_lib.utils.tsv import write_tsv


def logging(
    config: object | None = None,
    *,
    command: str | None = None,
    git_cwd: Path | str | None = None,
    logging_config: LoggingSettings | None = None,
    open_telemetry: OpenTelemetrySettings | None = None,
    run_fields: Mapping[str, Any] | None = None,
    run_summary: Callable[[], Mapping[str, Any]] | None = None,
) -> AbstractContextManager[Path | None]:
    """Configure standard CLI logging and emit startup metadata."""
    resolved_logging_config = logging_config
    if open_telemetry is not None:
        resolved_logging_config = logging_config or logging_settings_from_config(config)
        resolved_logging_config = LoggingSettings(
            logger_name=resolved_logging_config.logger_name,
            terminal_level=resolved_logging_config.terminal_level,
            log_file_level=resolved_logging_config.log_file_level,
            log_file=resolved_logging_config.log_file,
            logs_dir=resolved_logging_config.logs_dir,
            run=resolved_logging_config.run,
            retain_log_files=resolved_logging_config.retain_log_files,
            suppress_http_dependency_logs=resolved_logging_config.suppress_http_dependency_logs,
            resource_sample_interval_seconds=(
                resolved_logging_config.resource_sample_interval_seconds
            ),
            open_telemetry=open_telemetry,
        )
    return logging_context(
        command or _script_name(),
        config,
        git_cwd=git_cwd,
        logging_config=resolved_logging_config,
        run_fields=run_fields,
        run_summary=run_summary,
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
    "HTTPResponse",
    "JSONDict",
    "LinearClient",
    "LinearClientConfig",
    "LoggingConfig",
    "LoggingSettings",
    "OpenTelemetryConfig",
    "OpenTelemetryRuntime",
    "OpenTelemetrySettings",
    "OpenTelemetrySetupError",
    "PullRequest",
    "SlackClient",
    "SlackClientConfig",
    "SlackError",
    "SlackPacer",
    "SourcegraphClient",
    "SourcegraphClientConfig",
    "SourcegraphJaegerTraceError",
    "SourcegraphJaegerTraceSummary",
    "SourcegraphTrace",
    "aliased_batched_query",
    "config_field",
    "config_field_names",
    "config_help_formatter",
    "config_snapshot",
    "configure_open_telemetry",
    "configure_logging",
    "critical",
    "current_traceparent_header",
    "debug",
    "decode_external_service_id",
    "decode_repository_id",
    "decode_sourcegraph_node_id",
    "encode_repository_id",
    "encode_sourcegraph_node_id",
    "error",
    "span",
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
    "logging_context",
    "logging_settings_from_config",
    "log_event",
    "log_context",
    "normalize_sourcegraph_endpoint",
    "open_telemetry_settings_from_config",
    "parse_args",
    "pr_ref_from_url",
    "quota_project_from_adc",
    "resolve_log_level_name",
    "save_json_cache",
    "slack_client_from_config",
    "sourcegraph_client_from_config",
    "stage",
    "startup_event",
    "stream_connection_nodes",
    "submit_with_log_context",
    "traceparent_fields",
    "warning",
    "write_tsv",
]
