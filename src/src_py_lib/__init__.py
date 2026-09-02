"""Public interface for src-py-lib consumers."""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from src_py_lib.clients.github import (
    GitHubClient,
    PullRequest,
    SearchedPullRequest,
    gh_cli_token,
    pr_ref_from_url,
)
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
from src_py_lib.clients.slack_session import (
    SlackSession,
    browser_signin,
    read_session,
    slack_client_from_session,
    write_session,
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
from src_py_lib.utils.events import (
    CallbackEventSink,
    CompositeEventSink,
    EventRuntime,
    EventSink,
    InMemoryEventSink,
    JSONLEventSink,
    NullEventSink,
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
    EventBridgeHandler,
    LoggingConfig,
    LoggingSettings,
    cli_logging_handlers,
    cli_run_context,
    critical,
    debug,
    error,
    info,
    log_context,
    log_event,
    logging_settings_from_config,
    observability_context,
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
    OtelLogsSink,
    configure_open_telemetry,
    current_traceparent_header,
    open_telemetry_settings_from_config,
    traceparent_fields,
)
from src_py_lib.utils.tsv import display_width, pad_display, write_tsv


def logging(
    config: object | None = None,
    *,
    command: str | None = None,
    git_cwd: Path | str | None = None,
    logging_config: LoggingSettings | None = None,
    open_telemetry: OpenTelemetrySettings | None = None,
    run_fields: Mapping[str, Any] | None = None,
    run_summary: Callable[[], Mapping[str, Any]] | None = None,
    resource: Mapping[str, Any] | None = None,
) -> AbstractContextManager[Path | None]:
    """Configure standard CLI-mode logging for one run and emit startup metadata.

    CLI mode: installs terminal and event-bridge handlers on the configured
    package loggers (never the root logger) and writes the JSONL event log.
    Importable-module callers should use `observability_context()` instead,
    which never touches stdlib logging handlers.
    """
    resolved_logging_config = logging_config
    if open_telemetry is not None:
        resolved_logging_config = logging_config or logging_settings_from_config(config)
        resolved_logging_config = LoggingSettings(
            logger_names=resolved_logging_config.logger_names,
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
    return cli_run_context(
        command or _script_name(),
        config,
        git_cwd=git_cwd,
        logging_config=resolved_logging_config,
        run_fields=run_fields,
        run_summary=run_summary,
        resource=resource,
    )


def _script_name() -> str:
    return Path(sys.argv[0]).stem or "python"


__all__ = [
    "CallbackEventSink",
    "CompositeEventSink",
    "Config",
    "ConfigError",
    "EventBridgeHandler",
    "EventRuntime",
    "EventSink",
    "GraphQLError",
    "GraphQLClient",
    "GitHubClient",
    "GoogleSheetsClient",
    "GoogleSheetsError",
    "HTTPClient",
    "HTTPClientError",
    "HTTPResponse",
    "InMemoryEventSink",
    "JSONDict",
    "JSONLEventSink",
    "LinearClient",
    "LinearClientConfig",
    "LoggingConfig",
    "LoggingSettings",
    "NullEventSink",
    "OpenTelemetryConfig",
    "OpenTelemetryRuntime",
    "OpenTelemetrySettings",
    "OpenTelemetrySetupError",
    "OtelLogsSink",
    "PullRequest",
    "SearchedPullRequest",
    "SlackClient",
    "SlackClientConfig",
    "SlackError",
    "SlackPacer",
    "SlackSession",
    "SourcegraphClient",
    "SourcegraphClientConfig",
    "SourcegraphJaegerTraceError",
    "SourcegraphJaegerTraceSummary",
    "SourcegraphTrace",
    "aliased_batched_query",
    "browser_signin",
    "config_field",
    "config_field_names",
    "config_help_formatter",
    "cli_logging_handlers",
    "cli_run_context",
    "config_snapshot",
    "configure_open_telemetry",
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
    "logging_settings_from_config",
    "log_event",
    "log_context",
    "normalize_sourcegraph_endpoint",
    "observability_context",
    "open_telemetry_settings_from_config",
    "parse_args",
    "pr_ref_from_url",
    "quota_project_from_adc",
    "read_session",
    "resolve_log_level_name",
    "save_json_cache",
    "slack_client_from_config",
    "slack_client_from_session",
    "sourcegraph_client_from_config",
    "stage",
    "startup_event",
    "stream_connection_nodes",
    "submit_with_log_context",
    "traceparent_fields",
    "warning",
    "write_session",
    "write_tsv",
    "display_width",
    "pad_display",
]
