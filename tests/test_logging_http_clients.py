"""Focused tests for logging, HTTP, and API-client primitives."""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Mapping
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import src_py_lib as src
from src_py_lib.clients.github import GitHubClient, graphql_api_url, pr_ref_from_url
from src_py_lib.clients.google_sheets import GoogleSheetsClient
from src_py_lib.clients.graphql import (
    GraphQLClient,
    GraphQLError,
    introspect_schema,
    stream_connection_nodes,
)
from src_py_lib.clients.linear import LinearClient, LinearClientConfig, linear_client_from_config
from src_py_lib.clients.one_password import (
    OnePasswordClient,
    OnePasswordError,
    resolve_op_secret_ref,
)
from src_py_lib.clients.slack import SlackClient
from src_py_lib.clients.sourcegraph import (
    SourcegraphClient,
    SourcegraphClientConfig,
    SourcegraphTrace,
    decode_external_service_id,
    decode_repository_id,
    encode_repository_id,
    normalize_sourcegraph_endpoint,
    sourcegraph_client_from_config,
)
from src_py_lib.utils.config import (
    Config,
    ConfigError,
    add_config_arguments,
    config_env_file_from_args,
    config_field,
    config_field_names,
    config_overrides_from_args,
    config_parse_args,
    config_snapshot,
    load_config,
    load_config_env_file,
    load_config_from_args,
    resolve_config_refs,
)
from src_py_lib.utils.events import (
    EVENT_FIELD_ORDER,
    CallbackEventSink,
    CompositeEventSink,
    InMemoryEventSink,
    JSONLEventSink,
    NullEventSink,
    current_event_runtime,
    ordered_event_payload,
    severity_fields,
)
from src_py_lib.utils.http import HTTPClient, HTTPClientError, HTTPResponse
from src_py_lib.utils.json_types import JSONDict, json_dict, json_list
from src_py_lib.utils.logging import (
    EventBridgeHandler,
    LoggingConfig,
    LoggingSettings,
    cli_logging_handlers,
    critical,
    debug,
    default_log_file,
    error,
    info,
    log_event,
    logging_settings_from_config,
    observability_context,
    resolve_log_level_name,
    span,
    stage,
    startup_event,
    warning,
)
from src_py_lib.utils.telemetry import OtelLogsSink


class RecordingHTTP(HTTPClient):
    """HTTPClient test double that records JSON request arguments."""

    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, str | int | float | bool | None] | None = None,
        json_body: object | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "query": query,
                "json_body": json_body,
            }
        )
        if self.responses:
            return self.responses.pop(0)
        return {"data": {"viewer": {"username": "alice"}}}


class RecordingLogHandler(logging.Handler):
    """Stdlib logging handler that collects records for isolation assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def events_named(events: list[dict[str, Any]], event_name: str) -> list[dict[str, Any]]:
    """Return the structured events with the given event name."""
    return [event for event in events if event["event_name"] == event_name]


def phase_event(events: list[dict[str, Any]], event_name: str, phase: str) -> dict[str, Any]:
    """Return the first event with the given name and `phase` attribute."""
    return next(
        event for event in events_named(events, event_name) if event["attributes"]["phase"] == phase
    )


class FakeOnePasswordClient(OnePasswordClient):
    """1Password test double that avoids shelling out."""

    def read(self, secret_ref: str) -> str:
        if secret_ref == "op://vault/item/field":
            return "resolved-secret"
        if secret_ref == "op://vault/page-size/value":
            return "40"
        if secret_ref == "op://vault/labels/value":
            return "gamma, delta"
        if secret_ref == "op://vault/name/value":
            return "resolved-name"
        raise OnePasswordError(f"unexpected secret ref: {secret_ref}")


class ExampleConfig(Config):
    """Config model used by Config tests."""

    token: str = config_field(
        default="",
        env_var="EXAMPLE_TOKEN",
        cli_flag="--token",
        metavar="TOKEN",
        help="Example token",
        secret=True,
    )
    page_size: int = config_field(
        default=25,
        env_var="EXAMPLE_PAGE_SIZE",
        cli_flag="--page-size",
        metavar="N",
        help="Example page size",
    )
    include_archived: bool = config_field(
        default=False,
        env_var="EXAMPLE_INCLUDE_ARCHIVED",
        cli_flag="--include-archived",
        help="Include archived examples",
    )
    output_dir: Path = config_field(
        default=Path("out"),
        env_var="EXAMPLE_OUTPUT_DIR",
        cli_flag="--output-dir",
        metavar="PATH",
        help="Example output directory",
    )
    labels: tuple[str, ...] = config_field(
        default=(),
        env_var="EXAMPLE_LABELS",
        cli_flag="--labels",
        metavar="CSV",
        help="Example labels",
    )


class RequiredConfig(Config):
    """Config model with a required secret field."""

    token: str = config_field(
        default="",
        env_var="REQUIRED_TOKEN",
        cli_flag="--token",
        metavar="TOKEN",
        help="Required token",
        secret=True,
        required=True,
    )
    name: str = config_field(
        default="",
        env_var="REQUIRED_NAME",
        cli_flag="--name",
        metavar="NAME",
        help="Non-secret required config name",
    )


class MultilineHelpConfig(Config):
    """Config model with multiline CLI help text."""

    notes: str = config_field(
        default="",
        env_var="MULTILINE_HELP_NOTES",
        cli_flag="--notes",
        metavar="TEXT",
        help="First line.\nSecond line.\n  Indented detail.",
    )


class GroupedHelpConfig(Config):
    """Config model with grouped help sections."""

    alpha: str = config_field(
        default="",
        env_var="GROUPED_HELP_ALPHA",
        cli_flag="--alpha",
        help="Alpha option",
        help_group="First group",
    )
    beta: str = config_field(
        default="",
        env_var="GROUPED_HELP_BETA",
        cli_flag="--beta",
        help="Beta option",
        help_group="Second group",
    )
    gamma: str = config_field(
        default="",
        env_var="GROUPED_HELP_GAMMA",
        cli_flag="--gamma",
        help="Gamma option",
        help_group="First group",
    )


class SnapshotOrderConfig(Config):
    """Config model whose field names and env-var names sort differently."""

    alpha: str = config_field(default="a", env_var="ZZZ_ALPHA")
    zulu: str = config_field(default="z", env_var="AAA_ZULU")


class BoundedConfig(Config):
    """Config model with numeric bounds."""

    page_size: int = config_field(
        default=25,
        env_var="BOUNDED_PAGE_SIZE",
        cli_flag="--page-size",
        metavar="N",
        ge=1,
    )
    sample_interval: float = config_field(
        default=10.0,
        env_var="BOUNDED_SAMPLE_INTERVAL",
        cli_flag="--sample-interval",
        metavar="SECS",
        ge=0,
    )


class PatternConfig(Config):
    """Config model with a string pattern constraint."""

    date: str | None = config_field(
        default=None,
        env_var="PATTERN_DATE",
        cli_flag="--date",
        metavar="YYYY-MM-DD",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )


class CommandStyleConfig(Config):
    """Config model with command-style flags."""

    get: bool = config_field(
        default=False,
        env_var="COMMAND_STYLE_GET",
        cli_flag="--get",
        cli_action="store_true",
    )
    verbose: bool = config_field(
        default=False,
        env_var="COMMAND_STYLE_VERBOSE",
        cli_flag="--verbose",
        cli_aliases=("-v",),
        cli_action="store_true",
    )
    schema_path: Path | None = config_field(
        default=None,
        env_var="COMMAND_STYLE_SCHEMA_PATH",
        cli_flag="--get-schema",
        cli_nargs="?",
        cli_const="schema.gql",
        metavar="FILE",
    )


class LinearExampleConfig(LinearClientConfig):
    """Config model composed from Linear client fields and app fields."""

    page_size: int = config_field(
        default=25,
        env_var="LINEAR_EXAMPLE_PAGE_SIZE",
        cli_flag="--page-size",
        metavar="N",
        help="Example page size",
    )


class SourcegraphExampleConfig(SourcegraphClientConfig):
    """Config model composed from Sourcegraph client fields and app fields."""

    repo_query: str = config_field(
        default="",
        env_var="SOURCEGRAPH_EXAMPLE_REPO_QUERY",
        cli_flag="--repo-query",
        metavar="QUERY",
        help="Example Sourcegraph repository query",
    )


class LoggingExampleConfig(LoggingConfig):
    """Config model composed from shared logging fields."""


class ConfigTest(unittest.TestCase):
    def test_load_config_env_file_uses_dotenv_parser(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "\n".join(
                    (
                        "# comment",
                        "export EXAMPLE_TOKEN='quoted token'",
                        "EXAMPLE_PAGE_SIZE=10 # inline comment",
                        "EXAMPLE_OUTPUT_DIR=${EXAMPLE_TOKEN}/out",
                        "BARE_KEY",
                    )
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                load_config_env_file(env_file),
                {
                    "EXAMPLE_TOKEN": "quoted token",
                    "EXAMPLE_PAGE_SIZE": "10",
                    "EXAMPLE_OUTPUT_DIR": "quoted token/out",
                },
            )

    def test_client_config_mixin_adds_linear_token_and_builds_client(self) -> None:
        parser = argparse.ArgumentParser()
        add_config_arguments(parser, LinearExampleConfig)
        args = parser.parse_args(["--linear-api-token", "test-token", "--page-size", "50"])

        config = load_config_from_args(
            LinearExampleConfig,
            args,
            env={},
            resolve_op_refs=False,
        )
        http = RecordingHTTP()
        client = linear_client_from_config(config, http=http)

        self.assertEqual(config.linear_api_token, "test-token")
        self.assertEqual(config.page_size, 50)
        self.assertEqual(client.token, "test-token")
        self.assertIs(client.http, http)

    def test_client_config_mixin_adds_sourcegraph_fields_and_builds_client(self) -> None:
        parser = argparse.ArgumentParser()
        add_config_arguments(parser, SourcegraphExampleConfig)
        args = parser.parse_args(
            [
                "--src-endpoint",
                "https://sourcegraph.example.com",
                "--src-access-token",
                "test-token",
                "--repo-query",
                "repo:example",
            ]
        )

        config = load_config_from_args(
            SourcegraphExampleConfig,
            args,
            env={},
            resolve_op_refs=False,
        )
        client = sourcegraph_client_from_config(config)

        self.assertEqual(config.src_endpoint, "https://sourcegraph.example.com")
        self.assertEqual(config.src_access_token, "test-token")
        self.assertEqual(config.repo_query, "repo:example")
        self.assertEqual(client.endpoint, "https://sourcegraph.example.com")
        self.assertEqual(client.token, "test-token")

    def test_load_config_uses_precedence_and_pydantic_types(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            env_file = base_dir / ".env"
            env_file.write_text(
                "\n".join(
                    (
                        "EXAMPLE_TOKEN=op://vault/item/field",
                        "EXAMPLE_PAGE_SIZE=10",
                        "EXAMPLE_INCLUDE_ARCHIVED=false",
                        "EXAMPLE_OUTPUT_DIR=from-env-file",
                        "EXAMPLE_LABELS=op://vault/labels/value",
                    )
                ),
                encoding="utf-8",
            )

            config = load_config(
                ExampleConfig,
                env_file=env_file,
                env={
                    "EXAMPLE_PAGE_SIZE": "op://vault/page-size/value",
                    "EXAMPLE_OUTPUT_DIR": "from-shell",
                },
                cli_overrides={
                    "include_archived": True,
                    "output_dir": "from-cli",
                },
                base_dir=base_dir,
                resolve_op_refs=True,
                op_client=FakeOnePasswordClient(),
            )

            self.assertEqual(config.token, "resolved-secret")
            self.assertEqual(config.page_size, 40)
            self.assertTrue(config.include_archived)
            self.assertEqual(config.output_dir, base_dir / "from-cli")
            self.assertEqual(config.labels, ("gamma", "delta"))
            snapshot = config_snapshot(config)
            self.assertEqual(
                list(snapshot),
                [
                    "EXAMPLE_INCLUDE_ARCHIVED",
                    "EXAMPLE_LABELS",
                    "EXAMPLE_OUTPUT_DIR",
                    "EXAMPLE_PAGE_SIZE",
                    "EXAMPLE_TOKEN",
                ],
            )
            self.assertEqual(
                snapshot,
                {
                    "EXAMPLE_INCLUDE_ARCHIVED": True,
                    "EXAMPLE_LABELS": ["gamma", "delta"],
                    "EXAMPLE_OUTPUT_DIR": str(base_dir / "from-cli"),
                    "EXAMPLE_PAGE_SIZE": 40,
                    "EXAMPLE_TOKEN": "provided",
                },
            )

    def test_config_snapshot_sorts_emitted_keys(self) -> None:
        snapshot = config_snapshot(SnapshotOrderConfig())

        self.assertEqual(list(snapshot), ["AAA_ZULU", "ZZZ_ALPHA"])
        self.assertEqual(snapshot, {"AAA_ZULU": "z", "ZZZ_ALPHA": "a"})

    def test_argparse_helpers_add_flags_and_collect_overrides(self) -> None:
        parser = argparse.ArgumentParser()
        add_config_arguments(parser, ExampleConfig)

        args = parser.parse_args(
            [
                "--env-file",
                "custom.env",
                "--token",
                "raw-token",
                "--page-size",
                "50",
                "--no-include-archived",
                "--labels",
                "one,two",
            ]
        )

        self.assertEqual(config_env_file_from_args(args), Path("custom.env"))
        self.assertEqual(
            config_overrides_from_args(ExampleConfig, args),
            {
                "token": "raw-token",
                "page_size": "50",
                "include_archived": False,
                "labels": "one,two",
            },
        )

    def test_config_field_names_combines_config_classes_and_fields(self) -> None:
        self.assertEqual(
            config_field_names(SourcegraphClientConfig, LoggingConfig, "page_size"),
            (
                "src_endpoint",
                "src_access_token",
                "src_log_level",
                "verbose",
                "quiet",
                "silent",
                "page_size",
            ),
        )

    def test_add_config_arguments_can_select_reusable_field_sets(self) -> None:
        parser = argparse.ArgumentParser()
        add_config_arguments(
            parser,
            ExampleConfig,
            include_fields=("token", "page_size", "EXAMPLE_LABELS"),
            exclude_fields=("page_size",),
        )

        args = parser.parse_args(["--token", "raw-token", "--labels", "one,two"])

        self.assertEqual(
            config_overrides_from_args(ExampleConfig, args),
            {
                "token": "raw-token",
                "labels": "one,two",
            },
        )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["--page-size", "50"])

    def test_config_parse_args_help_only_shows_selected_fields(self) -> None:
        stdout = io.StringIO()

        with redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            config_parse_args(
                ExampleConfig,
                argv=["--help"],
                env={},
                resolve_op_refs=False,
                include_fields=("labels", "token"),
            )

        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertIn("--token TOKEN", help_text)
        self.assertIn("--labels CSV", help_text)
        self.assertLess(help_text.index("--labels CSV"), help_text.index("--token TOKEN"))
        self.assertNotIn("--page-size", help_text)
        self.assertNotIn("--include-archived", help_text)

    def test_config_parse_args_groups_help_by_field_metadata(self) -> None:
        stdout = io.StringIO()

        with redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            config_parse_args(
                GroupedHelpConfig,
                argv=["--help"],
                env={},
                resolve_op_refs=False,
                include_fields=("beta", "alpha", "gamma"),
            )

        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertLess(help_text.index("Second group:"), help_text.index("First group:"))
        self.assertLess(help_text.index("First group:"), help_text.index("Config:"))
        self.assertLess(help_text.index("--alpha"), help_text.index("--gamma"))
        self.assertIn("Second group:\n  --beta", help_text)
        self.assertIn("First group:\n  --alpha", help_text)
        self.assertNotIn("override matching environment variables", help_text)

    def test_config_arguments_support_aliases_actions_and_optional_values(self) -> None:
        parser = argparse.ArgumentParser()
        add_config_arguments(parser, CommandStyleConfig)

        default_schema_args = parser.parse_args(["--get", "-v", "--get-schema"])
        named_schema_args = parser.parse_args(["--get-schema", "custom.gql"])

        default_schema_config = load_config_from_args(
            CommandStyleConfig,
            default_schema_args,
            env={},
            resolve_op_refs=False,
        )
        named_schema_config = load_config_from_args(
            CommandStyleConfig,
            named_schema_args,
            env={},
            resolve_op_refs=False,
        )

        self.assertTrue(default_schema_config.get)
        self.assertTrue(default_schema_config.verbose)
        self.assertEqual(default_schema_config.schema_path, Path.cwd() / "schema.gql")
        self.assertEqual(named_schema_config.schema_path, Path.cwd() / "custom.gql")

    def test_config_field_supports_numeric_bounds(self) -> None:
        config = load_config(
            BoundedConfig,
            env_file=None,
            env={"BOUNDED_PAGE_SIZE": "1", "BOUNDED_SAMPLE_INTERVAL": "0"},
            resolve_op_refs=False,
        )

        self.assertEqual(config.page_size, 1)
        self.assertEqual(config.sample_interval, 0)
        with self.assertRaisesRegex(ConfigError, "greater than or equal to 1"):
            load_config(
                BoundedConfig,
                env_file=None,
                env={"BOUNDED_PAGE_SIZE": "0"},
                resolve_op_refs=False,
            )
        with self.assertRaisesRegex(ConfigError, "greater than or equal to 0"):
            load_config(
                BoundedConfig,
                env_file=None,
                env={"BOUNDED_SAMPLE_INTERVAL": "-0.1"},
                resolve_op_refs=False,
            )

    def test_config_field_supports_string_pattern(self) -> None:
        config = load_config(
            PatternConfig,
            env_file=None,
            env={"PATTERN_DATE": "2026-01-31"},
            resolve_op_refs=False,
        )

        self.assertEqual(config.date, "2026-01-31")
        with self.assertRaisesRegex(ConfigError, "String should match pattern"):
            load_config(
                PatternConfig,
                env_file=None,
                env={"PATTERN_DATE": "2026-1-31"},
                resolve_op_refs=False,
            )
        with self.assertRaisesRegex(ConfigError, "String should match pattern"):
            load_config(
                PatternConfig,
                env_file=None,
                env={"PATTERN_DATE": "2026-01-31T00:00:00Z"},
                resolve_op_refs=False,
            )

    def test_logging_config_mixin_adds_log_level_from_cli_and_env(self) -> None:
        parser = argparse.ArgumentParser()
        add_config_arguments(parser, LoggingExampleConfig)
        args = parser.parse_args(["--src-log-level", "INFO", "-v"])

        cli_config = load_config_from_args(
            LoggingExampleConfig,
            args,
            env={"SRC_LOG_LEVEL": "WARNING"},
            resolve_op_refs=False,
        )
        env_config = load_config(
            LoggingExampleConfig,
            env_file=None,
            env={"SRC_LOG_LEVEL": "ERROR"},
            resolve_op_refs=False,
        )

        self.assertEqual(cli_config.src_log_level, "INFO")
        self.assertTrue(cli_config.verbose)
        self.assertEqual(env_config.src_log_level, "ERROR")

    def test_logging_config_rejects_multiple_log_level_alias(self) -> None:
        with self.assertRaisesRegex(ConfigError, "choose only one of --verbose"):
            load_config(
                LoggingExampleConfig,
                env_file=None,
                env={"SRC_LOG_VERBOSE": "true", "SRC_LOG_QUIET": "true"},
                resolve_op_refs=False,
            )

    def test_resolve_log_level_name_maps_cli_alias(self) -> None:
        self.assertEqual(resolve_log_level_name(verbose=True), "DEBUG")
        self.assertEqual(resolve_log_level_name(quiet=True), "WARNING")
        self.assertEqual(resolve_log_level_name(silent=True), "ERROR")
        self.assertEqual(resolve_log_level_name(log_level="trace"), "trace")
        self.assertIsNone(resolve_log_level_name(object()))

        config = LoggingExampleConfig(src_log_level="INFO")
        self.assertEqual(resolve_log_level_name(config), "INFO")
        verbose_config = LoggingExampleConfig(src_log_level="INFO", verbose=True)
        self.assertEqual(resolve_log_level_name(verbose_config), "DEBUG")
        quiet_config = config_parse_args(
            LoggingExampleConfig,
            argv=["-q"],
            env={},
            resolve_op_refs=False,
        )
        self.assertEqual(resolve_log_level_name(quiet_config), "WARNING")
        env_config = load_config(
            LoggingExampleConfig,
            env_file=None,
            env={"SRC_LOG_SILENT": "true"},
            resolve_op_refs=False,
        )
        self.assertTrue(env_config.silent)
        self.assertEqual(resolve_log_level_name(env_config), "ERROR")

    def test_logging_settings_from_config_maps_common_cli_levels(self) -> None:
        default_settings = logging_settings_from_config(
            resource_sample_interval_seconds=2.5,
        )
        self.assertEqual(default_settings.terminal_level, "INFO")
        self.assertEqual(default_settings.log_file_level, "debug")
        self.assertEqual(default_settings.resource_sample_interval_seconds, 2.5)

        quiet_config = LoggingExampleConfig(src_log_level="INFO", quiet=True)
        quiet_settings = logging_settings_from_config(quiet_config)
        self.assertEqual(quiet_settings.terminal_level, "WARNING")
        self.assertEqual(quiet_settings.log_file_level, "WARNING")

        log_level_config = LoggingExampleConfig(src_log_level="ERROR")
        log_level_settings = logging_settings_from_config(log_level_config)
        self.assertEqual(log_level_settings.terminal_level, "ERROR")
        self.assertEqual(log_level_settings.log_file_level, "ERROR")

    def test_config_parse_args_loads_config_and_reports_config_errors(self) -> None:
        config = config_parse_args(
            ExampleConfig,
            argv=["--token", "raw-token", "--page-size", "50"],
            env={},
            resolve_op_refs=False,
            description="Example CLI.",
        )

        self.assertEqual(config.token, "raw-token")
        self.assertEqual(config.page_size, 50)

        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            config_parse_args(RequiredConfig, argv=[], env={}, resolve_op_refs=False)

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("REQUIRED_TOKEN", stderr.getvalue())

    def test_config_parse_args_preserves_description_newlines_in_help(self) -> None:
        description = "Example CLI.\n\nSteps:\n  1. Collect data.\n  2. Export data."
        stdout = io.StringIO()

        with redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            config_parse_args(
                ExampleConfig,
                argv=["--help"],
                description=description,
                env={},
                resolve_op_refs=False,
            )

        self.assertEqual(raised.exception.code, 0)
        self.assertIn(description, stdout.getvalue())

    def test_config_parse_args_keeps_long_options_on_help_line(self) -> None:
        stdout = io.StringIO()

        with redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            config_parse_args(
                SourcegraphExampleConfig,
                argv=["--help"],
                env={},
                resolve_op_refs=False,
            )

        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertNotIn("--src-access-token TOKEN\n", help_text)
        self.assertRegex(help_text, r"--src-access-token TOKEN +Sourcegraph access token")

    def test_config_parse_args_preserves_argument_help_newlines(self) -> None:
        stdout = io.StringIO()

        with redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            config_parse_args(
                MultilineHelpConfig,
                argv=["--help"],
                env={},
                resolve_op_refs=False,
            )

        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertIn("First line.\n", help_text)
        self.assertRegex(help_text, r"\n +Second line\.\n")
        self.assertRegex(help_text, r"\n +  Indented detail\.")

    def test_config_field_requires_named_default(self) -> None:
        config_field_any: Any = config_field

        with self.assertRaises(TypeError):
            config_field_any("", env_var="POSITIONAL_DEFAULT")

    def test_required_values_and_reference_resolution(self) -> None:
        with self.assertRaisesRegex(ConfigError, "REQUIRED_TOKEN"):
            load_config(RequiredConfig, env_file=None, env={})

        config = load_config(
            RequiredConfig,
            env_file=None,
            env={
                "REQUIRED_TOKEN": "op://vault/item/field",
                "REQUIRED_NAME": "op://vault/name/value",
            },
        )
        resolved = resolve_config_refs(config, client=FakeOnePasswordClient())

        self.assertEqual(config.token, "op://vault/item/field")
        self.assertEqual(config.name, "op://vault/name/value")
        self.assertEqual(resolved.token, "resolved-secret")
        self.assertEqual(resolved.name, "resolved-name")


class GraphQLTest(unittest.TestCase):
    def test_introspect_schema_returns_schema_with_documentation_query(self) -> None:
        schema: JSONDict = {
            "description": "Example schema.",
            "queryType": {"name": "Query"},
            "types": [{"kind": "OBJECT", "name": "Query", "description": "Root query."}],
        }
        http = RecordingHTTP([{"data": {"__schema": schema}}])
        client = GraphQLClient("https://example.com/graphql", {}, "Example", http=http)

        self.assertEqual(introspect_schema(client), schema)
        body = json_dict(http.calls[0]["json_body"])
        query = str(body.get("query") or "")
        self.assertIn("description", query)
        self.assertIn("fields(includeDeprecated: true)", query)
        self.assertIn("inputFields", query)
        self.assertIn("enumValues(includeDeprecated: true)", query)
        self.assertIn("deprecationReason", query)
        self.assertNotIn("__schema {\n    description", query)
        self.assertNotIn("isRepeatable", query)
        self.assertNotIn("args(includeDeprecated: true)", query)

    def test_introspect_schema_writes_schema_file(self) -> None:
        schema: JSONDict = {
            "description": "Example schema.",
            "queryType": {"name": "Query"},
            "types": [{"kind": "OBJECT", "name": "Query"}],
        }
        seen: dict[str, str] = {}

        def execute(query: str) -> JSONDict:
            seen["query"] = query
            return {"__schema": schema}

        with tempfile.TemporaryDirectory() as directory:
            output_file = Path(directory) / "schema" / "schema.json"

            result = introspect_schema(execute, output_file=output_file)

            self.assertIsNone(result)
            self.assertIn("IntrospectionQuery", seen["query"])
            self.assertEqual(json.loads(output_file.read_text(encoding="utf-8")), schema)


class EventSinkTest(unittest.TestCase):
    def test_severity_fields_maps_python_levels_to_otel_pairs(self) -> None:
        self.assertEqual(severity_fields(logging.DEBUG), ("DEBUG", 5))
        self.assertEqual(severity_fields(logging.INFO), ("INFO", 9))
        self.assertEqual(severity_fields(logging.WARNING), ("WARN", 13))
        self.assertEqual(severity_fields(logging.ERROR), ("ERROR", 17))
        self.assertEqual(severity_fields(logging.CRITICAL), ("FATAL", 21))
        self.assertEqual(severity_fields(35), ("WARN", 13))
        self.assertEqual(severity_fields(15), ("DEBUG", 5))
        self.assertEqual(severity_fields(1), ("TRACE", 1))

    def test_ordered_event_payload_puts_model_fields_first_and_sorts_attributes(self) -> None:
        payload: dict[str, Any] = {
            "attributes": {"zulu": 1, "alpha": 2},
            "custom_extra": True,
            "event_name": "example",
            "severity_number": 9,
            "severity_text": "INFO",
            "time_unix_nano": 123,
        }

        ordered = ordered_event_payload(payload)

        self.assertEqual(
            list(ordered),
            [
                "time_unix_nano",
                "severity_text",
                "severity_number",
                "event_name",
                "attributes",
                "custom_extra",
            ],
        )
        self.assertEqual(list(ordered["attributes"]), ["alpha", "zulu"])

    def test_jsonl_event_sink_is_thread_safe_and_ignores_emit_after_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "events.json"
            sink = JSONLEventSink(path)

            def emit_batch(worker: int) -> None:
                for index in range(50):
                    sink.emit(
                        {
                            "event_name": f"event_{worker}_{index}",
                            "attributes": {"worker": worker},
                        }
                    )

            workers = [threading.Thread(target=emit_batch, args=(worker,)) for worker in range(8)]
            for worker_thread in workers:
                worker_thread.start()
            for worker_thread in workers:
                worker_thread.join()
            sink.close()
            sink.emit({"event_name": "after_close", "attributes": {}})

            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 400)
            self.assertEqual(events_named(rows, "after_close"), [])

    def test_composite_sink_fans_out_and_sinks_receive_copies(self) -> None:
        received: list[dict[str, Any]] = []

        def mutating_callback(event: dict[str, Any]) -> None:
            received.append(event)
            event["mutated"] = True

        memory = InMemoryEventSink()
        composite = CompositeEventSink((CallbackEventSink(mutating_callback), memory))
        original: dict[str, Any] = {"event_name": "example", "attributes": {"answer": 42}}

        composite.emit(original)

        self.assertEqual(received[0]["event_name"], "example")
        self.assertNotIn("mutated", original)
        self.assertNotIn("mutated", memory.events[0])
        memory.events[0]["stored_mutation"] = True
        self.assertNotIn("stored_mutation", original)


class LoggingTest(unittest.TestCase):
    def test_default_log_file_uses_dashed_timestamp_offset_and_run(self) -> None:
        path = default_log_file(Path("logs"), run="1ea51330")

        self.assertEqual(path.parent, Path("logs"))
        self.assertRegex(
            path.name,
            r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-\d{4}-1ea51330\.json$",
        )

    def test_span_and_log_event_default_to_null_sink_outside_runs(self) -> None:
        self.assertIsInstance(current_event_runtime().sink, NullEventSink)

        with span("no_context_span") as extra:
            extra["answer"] = 42
            log_event("info", "no_context_event", logger_name="ignored", answer=42)

    def test_observability_context_leaves_root_logger_and_files_untouched(self) -> None:
        root_logger = logging.getLogger()
        handlers_before = list(root_logger.handlers)
        level_before = root_logger.level
        sink = InMemoryEventSink()

        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            with observability_context("guest-test", sink=sink, run="test-run"):
                self.assertEqual(list(root_logger.handlers), handlers_before)
                self.assertEqual(root_logger.level, level_before)
                with span("guest_span"):
                    info("guest_event", answer=42)
            self.assertEqual(list(Path(directory).iterdir()), [])

        self.assertEqual(list(root_logger.handlers), handlers_before)
        self.assertEqual(root_logger.level, level_before)
        self.assertEqual(len(events_named(sink.events, "guest_event")), 1)

    def test_observability_context_emits_run_startup_and_run_end_events(self) -> None:
        sink = InMemoryEventSink()
        config = ExampleConfig(token="secret-token")

        with observability_context(
            "unit-test",
            config,
            sink=sink,
            run="test-run",
            run_fields={"endpoint": "https://example.com"},
            resource={"service.name": "unit-test-service"},
        ):
            info("inside_command", answer=42)

        run_start = phase_event(sink.events, "run", "start")
        self.assertEqual(run_start["severity_text"], "DEBUG")
        self.assertEqual(run_start["severity_number"], 5)
        self.assertEqual(run_start["resource"]["process.pid"], os.getpid())
        self.assertEqual(run_start["resource"]["process.runtime.name"], sys.implementation.name)
        self.assertEqual(run_start["resource"]["process.runtime.version"], sys.version.split()[0])
        self.assertEqual(run_start["resource"]["service.name"], "unit-test-service")

        startup = events_named(sink.events, "startup")[0]
        self.assertEqual(startup["severity_text"], "INFO")
        self.assertNotIn("resource", startup)
        self.assertEqual(startup["attributes"]["command"], "unit-test")
        self.assertEqual(startup["attributes"]["run"], "test-run")
        self.assertEqual(startup["attributes"]["config"]["EXAMPLE_TOKEN"], "provided")

        inside = events_named(sink.events, "inside_command")[0]
        self.assertEqual(inside["attributes"]["command"], "unit-test")
        self.assertEqual(inside["attributes"]["answer"], 42)
        self.assertIsInstance(inside["time_unix_nano"], int)

        run_end = phase_event(sink.events, "run", "end")
        self.assertEqual(run_end["severity_text"], "INFO")
        end_attributes = run_end["attributes"]
        self.assertEqual(end_attributes["status"], "ok")
        self.assertIsNone(end_attributes["error.type"])
        self.assertEqual(end_attributes["exit_code"], 0)
        self.assertEqual(end_attributes["endpoint"], "https://example.com")
        self.assertGreaterEqual(end_attributes["duration_ms"], 0)
        self.assertEqual(end_attributes["http.client.request.count"], 0)
        self.assertEqual(end_attributes["http.client.retry.count"], 0)
        self.assertEqual(end_attributes["http.client.transport_error.count"], 0)

    def test_observability_context_records_system_exit_semantics(self) -> None:
        clean_sink = InMemoryEventSink()
        with (
            self.assertRaises(SystemExit),
            observability_context("unit-test", sink=clean_sink, run="test-run"),
        ):
            raise SystemExit(0)

        clean_end = phase_event(clean_sink.events, "run", "end")
        self.assertEqual(clean_end["severity_text"], "INFO")
        self.assertEqual(clean_end["attributes"]["status"], "ok")
        self.assertIsNone(clean_end["attributes"]["error.type"])
        self.assertEqual(clean_end["attributes"]["exit_code"], 0)

        failing_sink = InMemoryEventSink()
        with (
            self.assertRaises(SystemExit),
            observability_context("unit-test", sink=failing_sink, run="test-run"),
        ):
            raise SystemExit(3)

        failing_end = phase_event(failing_sink.events, "run", "end")
        self.assertEqual(failing_end["severity_text"], "ERROR")
        self.assertEqual(failing_end["attributes"]["status"], "error")
        self.assertEqual(failing_end["attributes"]["error.type"], "SystemExit")
        self.assertEqual(failing_end["attributes"]["exit_code"], 3)

    def test_observability_context_min_level_suppresses_debug_events(self) -> None:
        sink = InMemoryEventSink()

        with observability_context("unit-test", sink=sink, run="test-run", min_level="info"):
            debug("hidden_event")
            info("visible_event")

        names = [event["event_name"] for event in sink.events]
        self.assertNotIn("hidden_event", names)
        self.assertIn("visible_event", names)
        self.assertIn("startup", names)
        run_phases = [event["attributes"]["phase"] for event in events_named(sink.events, "run")]
        self.assertEqual(run_phases, ["end"])

    def test_observability_context_resource_sampler_emits_samples_and_summary(self) -> None:
        sink = InMemoryEventSink()

        with observability_context(
            "unit-test", sink=sink, run="test-run", resource_sample_interval_seconds=3600
        ):
            pass

        samples = events_named(sink.events, "resource_sample")
        self.assertGreaterEqual(len(samples), 2)
        for sample in samples:
            self.assertEqual(sample["severity_text"], "DEBUG")
            self.assertIn("num_threads", sample["attributes"])
            self.assertIn("rss_mb", sample["attributes"])

        run_end = phase_event(sink.events, "run", "end")
        self.assertIn("peak_rss_mb", run_end["attributes"])
        self.assertIn("cpu_user_seconds", run_end["attributes"])
        self.assertIn("cpu_system_seconds", run_end["attributes"])
        self.assertIn("cpu_count_logical", run_end["attributes"])

    def test_observability_context_resource_sampler_interval_zero_summarizes_only(self) -> None:
        sink = InMemoryEventSink()

        with observability_context(
            "unit-test", sink=sink, run="test-run", resource_sample_interval_seconds=0
        ):
            pass

        self.assertEqual(events_named(sink.events, "resource_sample"), [])
        run_end = phase_event(sink.events, "run", "end")
        self.assertIn("peak_rss_mb", run_end["attributes"])
        self.assertIn("cpu_count_logical", run_end["attributes"])

    def test_log_event_helpers_map_string_levels_to_otel_severity(self) -> None:
        sink = InMemoryEventSink()

        with observability_context("unit-test", sink=sink, run="test-run"):
            log_event("bogus", "fallback_info", logger_name="ignored")
            warning("warning_event")
            error("error_event")
            critical("critical_event")

        severities = {
            event["event_name"]: (event["severity_text"], event["severity_number"])
            for event in sink.events
        }
        self.assertEqual(severities["fallback_info"], ("INFO", 9))
        self.assertEqual(severities["warning_event"], ("WARN", 13))
        self.assertEqual(severities["error_event"], ("ERROR", 17))
        self.assertEqual(severities["critical_event"], ("FATAL", 21))

    def test_stage_adds_attributes_to_nested_events(self) -> None:
        sink = InMemoryEventSink()

        with (
            observability_context("unit-test", sink=sink, run="test-run"),
            stage("sync", tenant="acme"),
        ):
            info("staged_event")

        staged = events_named(sink.events, "staged_event")[0]
        self.assertEqual(staged["attributes"]["stage"], "sync")
        self.assertEqual(staged["attributes"]["tenant"], "acme")
        self.assertEqual(staged["attributes"]["command"], "unit-test")

    def test_startup_event_uses_explicit_git_commit(self) -> None:
        sink = InMemoryEventSink()

        with observability_context("unit-test", sink=sink, run="test-run"):
            startup_event(command="manual-startup", git_commit="abc1234")

        manual = next(
            event
            for event in events_named(sink.events, "startup")
            if event["attributes"]["command"] == "manual-startup"
        )
        self.assertEqual(manual["attributes"]["git_commit"], "abc1234")
        self.assertIsNone(manual["attributes"]["log_file"])

    def test_span_emits_debug_start_and_wide_end_events(self) -> None:
        sink = InMemoryEventSink()

        with (
            observability_context("unit-test", sink=sink, run="test-run"),
            span("work_unit", items=3) as extra,
        ):
            extra["processed"] = 2

        start = phase_event(sink.events, "work_unit", "start")
        self.assertEqual(start["severity_text"], "DEBUG")
        self.assertEqual(start["attributes"]["items"], 3)

        end = phase_event(sink.events, "work_unit", "end")
        self.assertEqual(end["severity_text"], "INFO")
        end_attributes = end["attributes"]
        self.assertEqual(end_attributes["items"], 3)
        self.assertEqual(end_attributes["processed"], 2)
        self.assertEqual(end_attributes["status"], "ok")
        self.assertIsNone(end_attributes["error.type"])
        self.assertGreaterEqual(end_attributes["duration_ms"], 0)

    def test_span_records_error_status_and_error_type(self) -> None:
        sink = InMemoryEventSink()

        with (
            observability_context("unit-test", sink=sink, run="test-run"),
            self.assertRaisesRegex(ValueError, "boom"),
            span("failing_unit"),
        ):
            raise ValueError("boom")

        end = phase_event(sink.events, "failing_unit", "end")
        self.assertEqual(end["severity_text"], "ERROR")
        self.assertEqual(end["attributes"]["status"], "error")
        self.assertEqual(end["attributes"]["error.type"], "ValueError")

    def test_span_can_lower_start_level_and_omit_success_status(self) -> None:
        sink = InMemoryEventSink()

        with (
            observability_context("unit-test", sink=sink, run="test-run", min_level="info"),
            span(
                "quiet_start",
                level="info",
                start_level="debug",
                omit_success_status=True,
            ),
        ):
            pass

        quiet_events = events_named(sink.events, "quiet_start")
        self.assertEqual(len(quiet_events), 1)
        attributes = quiet_events[0]["attributes"]
        self.assertEqual(attributes["phase"], "end")
        self.assertNotIn("status", attributes)
        self.assertNotIn("error.type", attributes)

    def test_span_context_adds_trace_and_span_fields(self) -> None:
        src.configure_open_telemetry(src.OpenTelemetrySettings(force_traces=True))
        sink = InMemoryEventSink()

        with observability_context("trace-test", sink=sink, run="test-run"), span("outer"):
            info("inside", answer=42)
            with span("inner"):
                pass

        run_start = phase_event(sink.events, "run", "start")
        outer_start = phase_event(sink.events, "outer", "start")
        outer_end = phase_event(sink.events, "outer", "end")
        inner_start = phase_event(sink.events, "inner", "start")
        inner_end = phase_event(sink.events, "inner", "end")
        inside = events_named(sink.events, "inside")[0]

        self.assertEqual(len(outer_start["trace_id"]), 32)
        self.assertEqual(len(outer_start["span_id"]), 16)
        self.assertEqual(outer_start["trace_id"], outer_end["trace_id"])
        self.assertEqual(outer_start["span_id"], outer_end["span_id"])
        self.assertEqual(outer_start["parent_span_id"], run_start["span_id"])

        self.assertEqual(inside["trace_id"], outer_start["trace_id"])
        self.assertEqual(inside["span_id"], outer_start["span_id"])

        self.assertEqual(inner_start["trace_id"], outer_start["trace_id"])
        self.assertEqual(inner_start["span_id"], inner_end["span_id"])
        self.assertEqual(len(inner_start["span_id"]), 16)
        self.assertNotEqual(inner_start["span_id"], outer_start["span_id"])
        self.assertEqual(inner_start["parent_span_id"], outer_start["span_id"])

    def test_log_event_adds_event_to_recording_otel_span(self) -> None:
        src.configure_open_telemetry(src.OpenTelemetrySettings(force_traces=True))
        provider = trace.get_tracer_provider()
        add_span_processor = getattr(provider, "add_span_processor", None)
        if not callable(add_span_processor):
            self.skipTest("global tracer provider does not accept span processors")
        exporter = InMemorySpanExporter()
        add_span_processor(SimpleSpanProcessor(exporter))

        with span("span_event_holder"):
            log_event("info", "observed_event", answer=42)

        holder = next(
            exported
            for exported in exporter.get_finished_spans()
            if exported.name == "span_event_holder"
        )
        self.assertIn("observed_event", [event.name for event in holder.events])

    def test_otel_helpers_return_current_w3c_traceparent_fields(self) -> None:
        src.configure_open_telemetry(src.OpenTelemetrySettings(force_traces=True))

        with span("traceparent_test"):
            traceparent = src.current_traceparent_header()
            self.assertIsNotNone(traceparent)
            assert traceparent is not None
            traceparent_parts = traceparent.split("-")

            self.assertRegex(traceparent, r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
            self.assertEqual(
                src.traceparent_fields(traceparent),
                {"trace_id": traceparent_parts[1], "span_id": traceparent_parts[2]},
            )


class CliLoggingHandlersTest(unittest.TestCase):
    def test_attaches_only_own_handlers_and_restores_prior_state(self) -> None:
        logger_name = "src_py_lib_test_handler_isolation"
        named_logger = logging.getLogger(logger_name)
        named_logger.setLevel(logging.WARNING)
        root_logger = logging.getLogger()
        customer_named_handler = RecordingLogHandler()
        customer_root_handler = RecordingLogHandler()
        named_logger.addHandler(customer_named_handler)
        root_logger.addHandler(customer_root_handler)
        root_handlers_before = list(root_logger.handlers)
        sink = InMemoryEventSink()

        try:
            with cli_logging_handlers(
                sink=sink, logger_names=(logger_name,), terminal_level="critical"
            ):
                added = [
                    handler
                    for handler in named_logger.handlers
                    if handler is not customer_named_handler
                ]
                self.assertEqual(
                    {type(handler) for handler in added},
                    {logging.StreamHandler, EventBridgeHandler},
                )
                self.assertEqual(list(root_logger.handlers), root_handlers_before)
                self.assertEqual(named_logger.level, logging.DEBUG)
                named_logger.info("customer handlers still see records")

            self.assertEqual(named_logger.handlers, [customer_named_handler])
            self.assertEqual(named_logger.level, logging.WARNING)
            self.assertEqual(list(root_logger.handlers), root_handlers_before)
        finally:
            named_logger.removeHandler(customer_named_handler)
            root_logger.removeHandler(customer_root_handler)
            named_logger.setLevel(logging.NOTSET)

        self.assertEqual(
            [record.getMessage() for record in customer_named_handler.records],
            ["customer handlers still see records"],
        )
        self.assertEqual(
            [record.getMessage() for record in customer_root_handler.records],
            ["customer handlers still see records"],
        )
        self.assertEqual([event["event_name"] for event in sink.events], ["log"])

    def test_event_bridge_forwards_human_log_records(self) -> None:
        sink = InMemoryEventSink()

        with cli_logging_handlers(
            sink=sink, logger_names=("src_py_lib_test_bridge",), terminal_level="critical"
        ):
            logging.getLogger("src_py_lib_test_bridge.module").info("Wrote %s", "x")

        event = events_named(sink.events, "log")[0]
        self.assertEqual(event["body"], "Wrote x")
        self.assertEqual(event["severity_text"], "INFO")
        self.assertEqual(event["attributes"]["logger"], "src_py_lib_test_bridge.module")

    def test_event_bridge_includes_exception_tracebacks(self) -> None:
        sink = InMemoryEventSink()
        logger_name = "src_py_lib_test_exception"

        with cli_logging_handlers(
            sink=sink, logger_names=(logger_name,), terminal_level="critical"
        ):
            try:
                raise ValueError("kaboom")
            except ValueError:
                logging.getLogger(logger_name).exception("operation failed")

        event = events_named(sink.events, "log")[0]
        self.assertEqual(event["body"], "operation failed")
        self.assertEqual(event["severity_text"], "ERROR")
        exception_text = event["attributes"]["exc_info"]
        self.assertIn("Traceback (most recent call last)", exception_text)
        self.assertIn("ValueError: kaboom", exception_text)

    def test_httpcore_response_headers_are_mined_and_redacted(self) -> None:
        sink = InMemoryEventSink()

        with cli_logging_handlers(
            sink=sink,
            logger_names=("src_py_lib_test_httpcore",),
            terminal_level="critical",
            suppress_http_dependency_logs=False,
        ):
            logging.getLogger("httpcore.http11").debug(
                "receive_response_headers.complete "
                "return_value=(b'HTTP/1.1', 200, b'OK', "
                "[(b'Zed', b'last'), (b'Content-Type', b'application/json'), "
                "(b'Set-Cookie', b'session=secret'), "
                "(b'X-Api-Key', b'secret'), (b'Alpha', b'first')])"
            )

        event = events_named(sink.events, "log")[0]
        self.assertEqual(event["body"], "receive_response_headers.complete")
        attributes = event["attributes"]
        self.assertEqual(attributes["logger"], "httpcore.http11")
        self.assertEqual(attributes["http_version"], "HTTP/1.1")
        self.assertEqual(attributes["status_code"], 200)
        self.assertEqual(attributes["reason_phrase"], "OK")
        self.assertEqual(
            list(attributes["headers"]),
            ["alpha", "content-type", "set-cookie", "x-api-key", "zed"],
        )
        self.assertEqual(
            attributes["headers"],
            {
                "alpha": "first",
                "content-type": "application/json",
                "set-cookie": "[redacted]",
                "x-api-key": "[redacted]",
                "zed": "last",
            },
        )

    def test_httpx_request_logs_are_demoted_to_debug_severity(self) -> None:
        sink = InMemoryEventSink()

        with cli_logging_handlers(
            sink=sink,
            logger_names=("src_py_lib_test_httpx",),
            terminal_level="critical",
            suppress_http_dependency_logs=False,
        ):
            logging.getLogger("httpx").info(
                'HTTP Request: POST https://api.linear.app/graphql "HTTP/1.1 200 OK"'
            )

        event = next(
            event
            for event in events_named(sink.events, "log")
            if str(event["body"]).startswith("HTTP Request:")
        )
        self.assertEqual(event["severity_text"], "DEBUG")
        self.assertEqual(event["severity_number"], 5)

    def test_http_dependency_logs_are_suppressed_by_default(self) -> None:
        sink = InMemoryEventSink()
        httpx_logger = logging.getLogger("httpx")
        httpx_handlers_before = list(httpx_logger.handlers)

        with cli_logging_handlers(
            sink=sink, logger_names=("src_py_lib_test_suppressed",), terminal_level="critical"
        ):
            self.assertEqual(list(httpx_logger.handlers), httpx_handlers_before)
            httpx_logger.info('HTTP Request: GET https://example.com "HTTP/1.1 200 OK"')
            logging.getLogger("httpcore.http11").debug(
                "receive_response_headers.complete return_value=()"
            )

        self.assertEqual(sink.events, [])


class CliRunContextTest(unittest.TestCase):
    def test_cli_run_context_writes_jsonl_run_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_file = Path(directory) / "events.json"
            config = ExampleConfig(token="secret-token")

            with src.logging(
                config,
                command="unit-test",
                git_cwd=__file__,
                logging_config=LoggingSettings(
                    terminal_level="critical",
                    log_file_level="debug",
                    log_file=log_file,
                    run="test-run",
                ),
            ) as context_log_file:
                self.assertEqual(context_log_file, log_file)
                info("inside_command", answer=42)

            rows = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]

            writing = events_named(rows, "log")[0]
            self.assertEqual(writing["body"], f"Writing log events to {log_file}.")

            run_start = phase_event(rows, "run", "start")
            self.assertEqual(run_start["severity_text"], "DEBUG")
            self.assertIn("process.pid", run_start["resource"])

            startup = events_named(rows, "startup")[0]
            self.assertEqual(startup["severity_text"], "INFO")
            self.assertEqual(startup["attributes"]["command"], "unit-test")
            self.assertEqual(startup["attributes"]["log_file"], str(log_file))
            self.assertEqual(startup["attributes"]["config"]["EXAMPLE_TOKEN"], "provided")
            self.assertIn("git_commit", startup["attributes"])

            inside = events_named(rows, "inside_command")[0]
            self.assertEqual(inside["attributes"]["command"], "unit-test")
            self.assertEqual(inside["attributes"]["run"], "test-run")
            self.assertEqual(inside["attributes"]["answer"], 42)

            run_end = phase_event(rows, "run", "end")
            self.assertEqual(run_end["severity_text"], "INFO")
            self.assertEqual(run_end["attributes"]["status"], "ok")

            for row in rows:
                self.assertLessEqual(set(row), set(EVENT_FIELD_ORDER))
                model_keys = [key for key in row if key in EVENT_FIELD_ORDER]
                self.assertEqual(model_keys, [key for key in EVENT_FIELD_ORDER if key in row])
                self.assertEqual(list(row["attributes"]), sorted(row["attributes"]))

    def test_cli_run_context_emits_run_summary_and_resets_http_metrics(self) -> None:
        attempts = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, json={"retry": True}, headers={"Retry-After": "0"})
            return httpx.Response(200, json={"ok": True})

        with tempfile.TemporaryDirectory() as directory:
            first_log_file = Path(directory) / "first.json"
            second_log_file = Path(directory) / "second.json"

            with src.logging(
                command="unit-test",
                logging_config=LoggingSettings(
                    terminal_level="critical",
                    log_file_level="debug",
                    log_file=first_log_file,
                    run="test-run",
                    resource_sample_interval_seconds=0,
                ),
                run_fields={"endpoint": "https://example.com"},
                run_summary=lambda: {"custom_count": 7},
            ):
                client = HTTPClient(
                    max_attempts=2,
                    retry_base_delay_seconds=0,
                    retry_max_delay_seconds=0,
                    transport=httpx.MockTransport(handler),
                )
                self.assertEqual(
                    client.json(
                        "POST",
                        "https://example.com/api",
                        json_body={"hello": "world"},
                    ),
                    {"ok": True},
                )

            rows = [
                json.loads(line) for line in first_log_file.read_text(encoding="utf-8").splitlines()
            ]
            run_end = phase_event(rows, "run", "end")
            attributes = run_end["attributes"]
            self.assertEqual(attributes["status"], "ok")
            self.assertEqual(attributes["exit_code"], 0)
            self.assertEqual(attributes["endpoint"], "https://example.com")
            self.assertEqual(attributes["custom_count"], 7)
            self.assertEqual(attributes["http.client.request.count"], 2)
            self.assertEqual(attributes["http.client.retry.count"], 1)
            self.assertEqual(attributes["http.client.response.2xx.count"], 1)
            self.assertEqual(attributes["http.client.response.4xx.count"], 1)
            self.assertEqual(attributes["http.client.response.429.count"], 1)
            self.assertEqual(attributes["http.client.transport_error.count"], 0)
            self.assertGreater(attributes["http.client.request.body.size.total"], 0)
            self.assertGreater(attributes["http.client.response.body.size.total"], 0)
            self.assertIn("cpu_count_logical", attributes)
            self.assertIn("peak_rss_mb", attributes)

            with src.logging(
                command="unit-test",
                logging_config=LoggingSettings(
                    terminal_level="critical",
                    log_file_level="debug",
                    log_file=second_log_file,
                    run="test-run-2",
                ),
            ):
                pass

            second_rows = [
                json.loads(line)
                for line in second_log_file.read_text(encoding="utf-8").splitlines()
            ]
            second_run_end = phase_event(second_rows, "run", "end")
            self.assertEqual(second_run_end["attributes"]["http.client.request.count"], 0)
            self.assertEqual(second_run_end["attributes"]["http.client.retry.count"], 0)

    def test_cli_run_context_records_system_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_file = Path(directory) / "events.json"

            with (
                self.assertRaises(SystemExit),
                src.logging(
                    command="unit-test",
                    logging_config=LoggingSettings(
                        logger_names=("src_py_lib_test_exit_code",),
                        terminal_level="critical",
                        log_file_level="debug",
                        log_file=log_file,
                        run="test-run",
                    ),
                ),
            ):
                raise SystemExit(3)

            rows = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
            run_end = phase_event(rows, "run", "end")
            self.assertEqual(run_end["severity_text"], "ERROR")
            self.assertEqual(run_end["attributes"]["status"], "error")
            self.assertEqual(run_end["attributes"]["error.type"], "SystemExit")
            self.assertEqual(run_end["attributes"]["exit_code"], 3)

    def test_src_log_level_env_controls_log_file_level(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_file = Path(directory) / "events.json"

            with (
                patch.dict("os.environ", {"SRC_LOG_LEVEL": "INFO"}),
                src.logging(
                    command="unit-test",
                    logging_config=LoggingSettings(
                        logger_names=("src_py_lib_test_log_level",),
                        terminal_level="critical",
                        log_file=log_file,
                        run="test-run",
                    ),
                ),
            ):
                debug("debug_event")
                info("info_event")

            rows = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
            names = [row["event_name"] for row in rows]
            self.assertNotIn("debug_event", names)
            self.assertIn("info_event", names)

    def test_cli_run_context_defaults_log_file_under_logs_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            logs_dir = Path(directory) / "logs"

            with src.logging(
                command="unit-test",
                logging_config=LoggingSettings(
                    logger_names=("src_py_lib_test_default_logs_dir",),
                    terminal_level="critical",
                    log_file_level="debug",
                    logs_dir=logs_dir,
                    run="test-run",
                ),
            ) as log_file:
                info("default_log_path")

            if log_file is None:
                self.fail("cli_run_context did not yield a default log file")
            self.assertEqual(log_file.parent, logs_dir)
            self.assertRegex(
                log_file.name,
                r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-\d{4}-test-run\.json$",
            )
            rows = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(events_named(rows, "default_log_path")), 1)


class OtelLogsSinkTest(unittest.TestCase):
    def test_otel_logs_sink_round_trips_events_through_logs_api(self) -> None:
        exporter = InMemoryLogRecordExporter()
        provider = LoggerProvider()
        provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
        set_logger_provider(provider)
        sink = OtelLogsSink()

        sink.emit(
            {
                "time_unix_nano": 123,
                "severity_text": "INFO",
                "severity_number": 9,
                "event_name": "unit_test_event",
                "body": "hello",
                "attributes": {"answer": 42},
            }
        )

        records = exporter.get_finished_logs()
        self.assertEqual(len(records), 1)
        record = records[0].log_record
        self.assertEqual(record.severity_text, "INFO")
        self.assertEqual(record.body, "hello")
        self.assertEqual(record.event_name, "unit_test_event")
        self.assertEqual(dict(record.attributes or {}), {"answer": 42})


class HTTPClientTest(unittest.TestCase):
    def test_json_request_adds_query_headers_and_decodes_object(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["authorization"] = request.headers["Authorization"]
            seen["user_agent"] = request.headers["User-Agent"]
            seen["body"] = request.content
            return httpx.Response(200, json={"ok": True})

        client = HTTPClient(
            timeout=12,
            max_attempts=1,
            max_connections=7,
            transport=httpx.MockTransport(handler),
        )
        payload = client.json(
            "POST",
            "https://example.com/api",
            headers={"Authorization": "Bearer token"},
            query={"limit": 10, "skip": None},
            json_body={"hello": "world"},
        )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(seen["url"], "https://example.com/api?limit=10")
        self.assertEqual(seen["authorization"], "Bearer token")
        self.assertEqual(seen["user_agent"], "src-py-lib")
        self.assertEqual(json.loads(seen["body"]), {"hello": "world"})
        self.assertEqual(client.max_connections, 7)

    def test_json_response_returns_payload_and_response_metadata(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"ok": True},
                headers={"X-Trace": "a" * 32},
            )

        client = HTTPClient(max_attempts=1, transport=httpx.MockTransport(handler))
        payload, response = client.json_response("GET", "https://example.com/api")

        self.assertEqual(payload, {"ok": True})
        self.assertIsInstance(response, HTTPResponse)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.header("X-Trace"), "a" * 32)

    def test_json_request_emits_structured_http_event(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"ok": True},
                headers={
                    "Zed": "last",
                    "Content-Type": "application/json",
                    "Set-Cookie": "session=secret",
                },
            )

        sink = InMemoryEventSink()
        with observability_context("unit-test", sink=sink, run="test-run", min_level="debug"):
            client = HTTPClient(max_attempts=1, transport=httpx.MockTransport(handler))
            payload = client.json(
                "POST",
                "https://user:pass@example.com/api?code=oauth",
                headers={"Authorization": "Bearer token"},
                query={"limit": 10, "access_token": "secret", "signature": "signed"},
                json_body={"hello": "world"},
            )

        self.assertEqual(payload, {"ok": True})
        http_request = phase_event(sink.events, "http_request", "end")
        attributes = http_request["attributes"]

        self.assertEqual(http_request["severity_text"], "DEBUG")
        self.assertEqual(events_named(sink.events, "log"), [])
        self.assertEqual(attributes["status_code"], 200)
        self.assertEqual(attributes["reason_phrase"], "OK")
        self.assertEqual(
            attributes["url"],
            "https://example.com/api?code=[redacted]&limit=10"
            "&access_token=[redacted]&signature=[redacted]",
        )
        self.assertEqual(attributes["request_bytes"], len(b'{"hello": "world"}'))
        self.assertEqual(attributes["request_headers"]["authorization"], "[redacted]")
        self.assertEqual(
            list(attributes["response_headers"]), sorted(attributes["response_headers"])
        )
        self.assertEqual(attributes["response_headers"]["content-type"], "application/json")
        self.assertEqual(attributes["response_headers"]["set-cookie"], "[redacted]")
        self.assertEqual(attributes["response_headers"]["zed"], "last")

    def test_json_request_wraps_timeouts(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timed out")

        client = HTTPClient(
            timeout=12,
            max_attempts=1,
            transport=httpx.MockTransport(handler),
        )

        with self.assertRaisesRegex(HTTPClientError, "read timed out"):
            client.json("POST", "https://example.com/api")

    def test_json_request_wraps_http_errors_with_body(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="rate limited", headers={"Retry-After": "0"})

        client = HTTPClient(
            max_attempts=1,
            transport=httpx.MockTransport(handler),
        )

        with self.assertRaisesRegex(HTTPClientError, "rate limited") as raised:
            client.json("GET", "https://user:pass@example.com/api?access_token=secret")

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.body, "rate limited")
        self.assertIn("https://example.com/api?access_token=[redacted]", str(raised.exception))
        self.assertNotIn("user:pass", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))


class ClientTest(unittest.TestCase):
    def test_sourcegraph_node_ids_convert_between_graphql_and_database_ids(self) -> None:
        self.assertEqual(42, decode_external_service_id("RXh0ZXJuYWxTZXJ2aWNlOjQy"))
        self.assertEqual("UmVwb3NpdG9yeTo5OQ==", encode_repository_id(99))
        self.assertEqual(99, decode_repository_id("UmVwb3NpdG9yeTo5OQ=="))
        self.assertEqual(99, src.decode_repository_id("UmVwb3NpdG9yeTo5OQ=="))

        with self.assertRaisesRegex(ValueError, "not a valid base64"):
            decode_repository_id("not base64")
        with self.assertRaisesRegex(ValueError, "not a Repository Node ID"):
            decode_repository_id("RXh0ZXJuYWxTZXJ2aWNlOjQy")
        with self.assertRaisesRegex(ValueError, "non-integer suffix"):
            decode_external_service_id("RXh0ZXJuYWxTZXJ2aWNlOmFiYw==")

    def test_normalize_sourcegraph_endpoint(self) -> None:
        self.assertEqual(
            normalize_sourcegraph_endpoint(" https://sourcegraph.example.com/ "),
            "https://sourcegraph.example.com",
        )
        self.assertEqual(
            normalize_sourcegraph_endpoint("http://localhost:3080/"),
            "http://localhost:3080",
        )
        with self.assertRaisesRegex(ValueError, "https:// URL"):
            normalize_sourcegraph_endpoint("http://localhost:3080", require_https=True)
        with self.assertRaisesRegex(ValueError, "http:// or https:// URL"):
            normalize_sourcegraph_endpoint("sourcegraph.example.com")

    def test_sourcegraph_client_builds_graphql_request(self) -> None:
        http = RecordingHTTP([{"data": {"currentUser": {"username": "alice"}}}])
        client = SourcegraphClient(" https://sourcegraph.example.com/ ", "token", http=http)
        data = client.graphql("query Viewer { currentUser { username } }")

        self.assertEqual(client.endpoint, "https://sourcegraph.example.com")
        self.assertEqual(data, {"currentUser": {"username": "alice"}})
        self.assertEqual(http.calls[0]["method"], "POST")
        self.assertEqual(http.calls[0]["url"], "https://sourcegraph.example.com/.api/graphql")
        self.assertEqual(http.calls[0]["headers"], {"Authorization": "token token"})

    def test_sourcegraph_client_graphql_can_disable_auto_pagination(self) -> None:
        http = RecordingHTTP(
            [
                {
                    "data": {
                        "users": {
                            "nodes": [{"username": "alice"}],
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                        }
                    }
                }
            ]
        )
        client = SourcegraphClient("https://sourcegraph.example.com", "token", http=http)
        query = """
query Users($first: Int!, $after: String) {
  users(first: $first, after: $after) {
    nodes { username }
    pageInfo { hasNextPage endCursor }
  }
}
"""

        data = client.graphql(query, page_size=1, follow_pages=False)

        self.assertEqual(json_dict(data.get("users"))["nodes"], [{"username": "alice"}])
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(http.calls[0]["json_body"]["variables"], {"first": 1})

    def test_sourcegraph_client_rejects_http_endpoint_by_default(self) -> None:
        with self.assertRaisesRegex(ValueError, "https:// URL"):
            SourcegraphClient("http://sourcegraph.example.com", "token")

        client = SourcegraphClient("http://localhost:3080", "token", allow_insecure_http=True)
        self.assertEqual(client.endpoint, "http://localhost:3080")

    def test_sourcegraph_client_streams_connection_nodes(self) -> None:
        http = RecordingHTTP(
            [
                {
                    "data": {
                        "users": {
                            "nodes": [{"username": "alice"}],
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                        }
                    }
                },
                {
                    "data": {
                        "users": {
                            "nodes": [{"username": "bob"}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                },
            ]
        )
        client = SourcegraphClient("https://sourcegraph.example.com", "token", http=http)
        nodes = list(
            client.stream_connection_nodes(
                """
                query Users($first: Int, $after: String) {
                    users(first: $first, after: $after) {
                        nodes { username }
                        pageInfo { hasNextPage endCursor }
                    }
                }
                """,
                connection_path=("users",),
                page_size=1,
            )
        )

        self.assertEqual(nodes, [{"username": "alice"}, {"username": "bob"}])
        first_body = json_dict(http.calls[0]["json_body"])
        second_body = json_dict(http.calls[1]["json_body"])
        self.assertEqual(first_body["variables"], {"first": 1, "after": None})
        self.assertEqual(second_body["variables"], {"first": 1, "after": "cursor-1"})

    def test_sourcegraph_client_validate_queries_current_user(self) -> None:
        http = RecordingHTTP([{"data": {"currentUser": {"username": "alice"}}}])
        client = SourcegraphClient("https://sourcegraph.example.com/", "token", http=http)

        self.assertEqual(client.validate(), {"username": "alice"})
        body = json_dict(http.calls[0]["json_body"])
        self.assertIn("SourcegraphClientValidate", str(body.get("query") or ""))
        self.assertIn("currentUser", str(body.get("query") or ""))

    def test_sourcegraph_debug_trace_mode_records_and_streams_jaeger_summary(self) -> None:
        src.configure_open_telemetry(src.OpenTelemetrySettings(force_traces=True))
        trace_id = "1" * 32
        span_id = "2" * 16
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/.api/graphql":
                return httpx.Response(
                    200,
                    json={"data": {"currentUser": {"username": "alice"}}},
                    headers={
                        "X-Trace": trace_id,
                        "X-Trace-Span": span_id,
                        "X-Trace-URL": f"https://jaeger.example.com/trace/{trace_id}",
                    },
                )
            self.assertEqual(request.url.path, f"/-/debug/jaeger/api/traces/{trace_id}")
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "spans": [
                                {
                                    "operationName": "GraphQL request",
                                    "duration": 120_000,
                                    "tags": [{"key": "graphql.operationName", "value": "Viewer"}],
                                },
                                {
                                    "operationName": "repo lookup",
                                    "duration": 30_000,
                                    "tags": [
                                        {"key": "error", "value": True},
                                        {"key": "otel.status_description", "value": "boom"},
                                    ],
                                },
                            ]
                        }
                    ]
                },
            )

        client = SourcegraphClient(
            "https://sourcegraph.example.com/",
            "token",
            http=HTTPClient(max_attempts=1, transport=httpx.MockTransport(handler)),
            fetch_sg_traces=True,
        )

        with span("sourcegraph_test"):
            self.assertEqual(
                client.graphql("query Viewer { currentUser { username } }", follow_pages=False),
                {"currentUser": {"username": "alice"}},
            )
        traces = client.drain_traces()
        summaries = list(client.stream_jaeger_trace_summaries(traces, retry_delays_seconds=(0,)))

        self.assertEqual(len(requests), 2)
        traceparent = requests[0].headers["traceparent"]
        traceparent_parts = traceparent.split("-")
        self.assertEqual(requests[0].headers["x-sourcegraph-request-trace"], "true")
        self.assertRegex(traceparent, r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
        self.assertEqual(traces[0].trace_id, trace_id)
        self.assertEqual(traces[0].span_id, span_id)
        self.assertEqual(traces[0].parent_trace_id, traceparent_parts[1])
        self.assertEqual(traces[0].parent_span_id, traceparent_parts[2])
        self.assertEqual(len(summaries), 1)
        self.assertTrue(summaries[0].jaeger_found)
        self.assertEqual(summaries[0].span_count, 2)
        self.assertEqual(summaries[0].hot_operations[0]["operation"], "GraphQL request")
        self.assertEqual(summaries[0].graphql_operations[0]["operation"], "Viewer")
        self.assertEqual(summaries[0].errored_spans[0]["description"], "boom")

    def test_sourcegraph_streams_jaeger_summaries_in_parallel(self) -> None:
        trace_ids = ("1" * 32, "2" * 32, "3" * 32)
        requested_trace_ids: list[str] = []
        first_batch_barrier = threading.Barrier(2, timeout=1)

        def handler(request: httpx.Request) -> httpx.Response:
            trace_id = request.url.path.rsplit("/", 1)[-1]
            requested_trace_ids.append(trace_id)
            if trace_id in trace_ids[:2]:
                first_batch_barrier.wait()
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "spans": [
                                {
                                    "operationName": f"trace {trace_id[0]}",
                                    "duration": 1_000,
                                    "tags": [],
                                }
                            ]
                        }
                    ]
                },
            )

        client = SourcegraphClient(
            "https://sourcegraph.example.com/",
            "token",
            http=HTTPClient(max_attempts=1, transport=httpx.MockTransport(handler)),
        )

        summaries = list(
            client.stream_jaeger_trace_summaries(
                [SourcegraphTrace(trace_id) for trace_id in trace_ids],
                retry_delays_seconds=(0,),
                parallelism=2,
            )
        )

        self.assertCountEqual(requested_trace_ids, trace_ids)
        self.assertCountEqual([summary.trace.trace_id for summary in summaries], trace_ids)

    def test_graphql_client_paginates_cursor_results(self) -> None:
        http = RecordingHTTP(
            [
                {
                    "data": {
                        "viewer": {
                            "items": {
                                "nodes": [{"id": "1"}],
                                "pageInfo": {
                                    "hasNextPage": True,
                                    "endCursor": "cursor-1",
                                },
                            }
                        }
                    }
                },
                {
                    "data": {
                        "viewer": {
                            "items": {
                                "nodes": [{"id": "2"}],
                                "pageInfo": {
                                    "hasNextPage": False,
                                    "endCursor": None,
                                },
                            }
                        }
                    }
                },
            ]
        )
        client = GraphQLClient("https://example.com/graphql", {}, "Example", http=http)
        query = """
query Items($first: Int!, $after: String, $userId: ID!) {
  viewer { items { nodes { id } pageInfo { hasNextPage endCursor } } }
}
"""

        data = client.execute(
            query,
            variables={"userId": "u1"},
            page_size=2,
        )
        nodes = json_list(json_dict(json_dict(data.get("viewer")).get("items")).get("nodes"))

        self.assertEqual(nodes, [{"id": "1"}, {"id": "2"}])
        self.assertEqual(
            http.calls[0]["json_body"]["variables"],
            {"userId": "u1", "first": 2, "after": None},
        )
        self.assertEqual(
            http.calls[1]["json_body"]["variables"],
            {"userId": "u1", "first": 2, "after": "cursor-1"},
        )

    def test_graphql_client_streams_connection_nodes(self) -> None:
        http = RecordingHTTP(
            [
                {
                    "data": {
                        "viewer": {
                            "items": {
                                "nodes": [{"id": "1"}],
                                "pageInfo": {
                                    "hasNextPage": True,
                                    "endCursor": "cursor-1",
                                },
                            }
                        }
                    }
                },
                {
                    "data": {
                        "viewer": {
                            "items": {
                                "nodes": [{"id": "2"}],
                                "pageInfo": {
                                    "hasNextPage": False,
                                    "endCursor": None,
                                },
                            }
                        }
                    }
                },
            ]
        )
        client = GraphQLClient("https://example.com/graphql", {}, "Example", http=http)
        query = """
query Items($first: Int!, $after: String, $userId: ID!) {
  viewer { items { nodes { id } pageInfo { hasNextPage endCursor } } }
}
"""

        nodes = list(
            client.stream_connection_nodes(
                query,
                variables={"userId": "u1"},
                connection_path=("viewer", "items"),
                page_size=2,
            )
        )

        self.assertEqual(nodes, [{"id": "1"}, {"id": "2"}])
        self.assertEqual(
            http.calls[0]["json_body"]["variables"],
            {"userId": "u1", "first": 2, "after": None},
        )
        self.assertEqual(
            http.calls[1]["json_body"]["variables"],
            {"userId": "u1", "first": 2, "after": "cursor-1"},
        )

    def test_stream_connection_nodes_accepts_execute_callback(self) -> None:
        calls: list[dict[str, Any]] = []
        responses: list[JSONDict] = [
            {
                "viewer": {
                    "items": {
                        "nodes": [{"id": "1"}],
                        "pageInfo": {
                            "hasNextPage": True,
                            "endCursor": "cursor-1",
                        },
                    }
                }
            },
            {
                "viewer": {
                    "items": {
                        "nodes": [{"id": "2"}],
                        "pageInfo": {
                            "hasNextPage": False,
                            "endCursor": None,
                        },
                    }
                }
            },
        ]

        def execute(query: str, variables: Mapping[str, Any] | None) -> JSONDict:
            calls.append({"query": query, "variables": dict(variables or {})})
            return responses.pop(0)

        query = """
query Items($first: Int!, $after: String, $userId: ID!) {
  viewer { items { nodes { id } pageInfo { hasNextPage endCursor } } }
}
"""

        nodes = list(
            stream_connection_nodes(
                execute,
                query,
                variables={"userId": "u1"},
                connection_path=("viewer", "items"),
                page_size=2,
            )
        )

        self.assertEqual(nodes, [{"id": "1"}, {"id": "2"}])
        self.assertEqual(
            [call["variables"] for call in calls],
            [
                {"userId": "u1", "first": 2, "after": None},
                {"userId": "u1", "first": 2, "after": "cursor-1"},
            ],
        )

    def test_graphql_client_emits_query_debug_events(self) -> None:
        http = RecordingHTTP(
            [
                {
                    "data": {
                        "viewer": {
                            "items": {
                                "nodes": [{"id": "1"}],
                                "pageInfo": {
                                    "hasNextPage": True,
                                    "endCursor": "cursor-1",
                                },
                            }
                        }
                    }
                },
                {
                    "data": {
                        "viewer": {
                            "items": {
                                "nodes": [{"id": "2"}],
                                "pageInfo": {
                                    "hasNextPage": False,
                                    "endCursor": None,
                                },
                            }
                        }
                    }
                },
            ]
        )
        client = GraphQLClient(
            "https://user:pass@example.com/graphql?access_token=secret&query=ok",
            {},
            "Example",
            http=http,
        )
        query = """
query Items($first: Int!, $after: String, $userId: ID!) {
  viewer { items { nodes { id } pageInfo { hasNextPage endCursor } } }
}
"""

        sink = InMemoryEventSink()
        with observability_context("unit-test", sink=sink, run="test-run", min_level="debug"):
            client.execute(query, variables={"userId": "u1"}, page_size=2)

        query_events = events_named(sink.events, "graphql_query")
        starts = [
            event["attributes"] for event in query_events if event["attributes"]["phase"] == "start"
        ]
        ends = [
            event["attributes"] for event in query_events if event["attributes"]["phase"] == "end"
        ]

        self.assertTrue(all(event["severity_text"] == "DEBUG" for event in query_events))
        self.assertEqual([attributes["query_name"] for attributes in starts], ["Items", "Items"])
        self.assertEqual([attributes["page_number"] for attributes in starts], [1, 2])
        self.assertEqual([attributes["page_size"] for attributes in starts], [2, 2])
        self.assertEqual([attributes["cursor_present"] for attributes in starts], [False, True])
        self.assertEqual(starts[0]["graphql_client"], "Example")
        self.assertEqual(
            starts[0]["url"],
            "https://example.com/graphql?access_token=[redacted]&query=ok",
        )
        self.assertEqual(starts[0]["variable_names"], ["after", "first", "userId"])
        self.assertEqual(ends[0]["response_fields"], ["viewer"])

    def test_graphql_client_requires_end_cursor_for_next_page(self) -> None:
        http = RecordingHTTP(
            [
                {
                    "data": {
                        "items": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": True, "endCursor": None},
                        }
                    }
                }
            ]
        )
        client = GraphQLClient("https://example.com/graphql", {}, "Example", http=http)
        query = """
query Items($first: Int!, $after: String) {
  items { nodes { id } pageInfo { hasNextPage endCursor } }
}
"""

        with self.assertRaisesRegex(GraphQLError, "endCursor"):
            client.execute(
                query,
                page_size=100,
            )

    def test_graphql_client_rejects_stalled_cursor(self) -> None:
        http = RecordingHTTP(
            [
                {
                    "data": {
                        "items": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                        }
                    }
                },
                {
                    "data": {
                        "items": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                        }
                    }
                },
            ]
        )
        client = GraphQLClient("https://example.com/graphql", {}, "Example", http=http)
        query = """
query Items($first: Int!, $after: String) {
  items { nodes { id } pageInfo { hasNextPage endCursor } }
}
"""

        with self.assertRaisesRegex(GraphQLError, "stalled"):
            client.execute(
                query,
                page_size=100,
            )

    def test_graphql_client_preserves_http_status_on_transport_errors(self) -> None:
        class FailingHTTP(RecordingHTTP):
            def json(
                self,
                method: str,
                url: str,
                *,
                headers: Mapping[str, str] | None = None,
                query: Mapping[str, str | int | float | bool | None] | None = None,
                json_body: object | None = None,
            ) -> dict[str, Any]:
                raise HTTPClientError("unavailable", status_code=503)

        client = GraphQLClient("https://example.com/graphql", {}, "Example", http=FailingHTTP())

        with self.assertRaises(GraphQLError) as raised:
            client.execute("query Viewer { viewer { login } }", follow_pages=False)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertFalse(raised.exception.is_application_error)

    def test_graphql_client_marks_application_errors(self) -> None:
        http = RecordingHTTP(
            [
                {
                    "data": {},
                    "errors": [{"message": "field does not exist"}],
                }
            ]
        )
        client = GraphQLClient("https://example.com/graphql", {}, "Example", http=http)

        with self.assertRaises(GraphQLError) as raised:
            client.execute("query Broken { missingField }", follow_pages=False)

        self.assertIsNone(raised.exception.status_code)
        self.assertTrue(raised.exception.is_application_error)

    def test_github_pr_ref_from_url(self) -> None:
        self.assertEqual(
            pr_ref_from_url("https://github.com/sourcegraph/amp/pull/1234"),
            "sourcegraph/amp#1234",
        )

    def test_github_client_defaults_to_github_dot_com(self) -> None:
        http = RecordingHTTP()
        client = GitHubClient("token", http=http)
        client.graphql("query Viewer { viewer { login } }")

        self.assertEqual(http.calls[0]["url"], "https://api.github.com/graphql")

    def test_github_client_can_target_github_enterprise(self) -> None:
        http = RecordingHTTP()
        client = GitHubClient("token", github_url="https://github.example.com", http=http)
        client.graphql("query Viewer { viewer { login } }")

        self.assertEqual(http.calls[0]["url"], "https://github.example.com/api/graphql")
        self.assertEqual(
            graphql_api_url("github.example.com"), "https://github.example.com/api/graphql"
        )

    def test_github_client_rejects_http_enterprise_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "https:// URL"):
            graphql_api_url("http://github.example.com")

    def test_github_client_validate_queries_viewer(self) -> None:
        http = RecordingHTTP([{"data": {"viewer": {"login": "alice"}}}])
        client = GitHubClient("token", http=http)

        self.assertEqual(client.validate(), {"login": "alice"})
        body = json_dict(http.calls[0]["json_body"])
        self.assertIn("GitHubClientValidate", str(body.get("query") or ""))

    def test_slack_client_validate_calls_auth_test(self) -> None:
        response = {"ok": True, "url": "https://example.slack.com/", "user_id": "U1"}
        http = RecordingHTTP([response])
        client = SlackClient("token", http=http)

        self.assertEqual(client.validate(), response)
        self.assertEqual(http.calls[0]["url"], "https://slack.com/api/auth.test")
        self.assertEqual(http.calls[0]["headers"], {"Authorization": "Bearer token"})

    def test_google_sheets_client_validate_fetches_metadata(self) -> None:
        metadata = {"sheets": [{"properties": {"sheetId": 1, "title": "Sheet1"}}]}
        http = RecordingHTTP([metadata])
        client = GoogleSheetsClient("spreadsheet-id", "token", quota_project="quota", http=http)

        self.assertEqual(client.validate(), metadata)
        self.assertEqual(
            http.calls[0]["url"],
            "https://sheets.googleapis.com/v4/spreadsheets/spreadsheet-id"
            "?fields=sheets.properties(sheetId,title,gridProperties)",
        )
        self.assertEqual(
            http.calls[0]["headers"],
            {"Authorization": "Bearer token", "X-Goog-User-Project": "quota"},
        )

    def test_one_password_client_validate_returns_authenticated_account(self) -> None:
        with patch("src_py_lib.clients.one_password.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                ["op", "whoami", "--format", "json"],
                0,
                stdout='{ "email": "alice@example.com", "account_uuid": "A1" }\n',
                stderr="",
            )

            self.assertEqual(
                OnePasswordClient().validate(),
                {"email": "alice@example.com", "account_uuid": "A1"},
            )

        run.assert_called_once_with(
            ["op", "whoami", "--format", "json"],
            check=True,
            text=True,
            capture_output=True,
        )

    def test_one_password_client_validate_requires_authentication(self) -> None:
        with patch("src_py_lib.clients.one_password.subprocess.run") as run:
            run.side_effect = subprocess.CalledProcessError(
                1,
                ["op", "whoami", "--format", "json"],
                stderr="not signed in",
            )

            with self.assertRaisesRegex(OnePasswordError, "not authenticated"):
                OnePasswordClient().validate()

    def test_one_password_client_signin_runs_signin_then_validates(self) -> None:
        with patch("src_py_lib.clients.one_password.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(["op", "signin"], 0),
                subprocess.CompletedProcess(
                    ["op", "whoami", "--format", "json"],
                    0,
                    stdout='{ "email": "alice@example.com" }\n',
                    stderr="",
                ),
            ]

            self.assertEqual(
                OnePasswordClient().signin(),
                {"email": "alice@example.com"},
            )

        self.assertEqual(run.call_count, 2)
        run.assert_any_call(["op", "signin"], check=True)
        run.assert_any_call(
            ["op", "whoami", "--format", "json"],
            check=True,
            text=True,
            capture_output=True,
        )

    def test_linear_client_builds_graphql_request(self) -> None:
        http = RecordingHTTP()
        with patch("src_py_lib.clients.linear.GraphQLClient") as client_cls:
            client_cls.return_value.execute.return_value = {
                "viewer": {"email": "alice@example.com"}
            }
            data = LinearClient("token", http=http).graphql(
                "query Viewer { viewer { email } }",
                {"first": 1},
                page_size=10,
            )

        self.assertEqual(data, {"viewer": {"email": "alice@example.com"}})
        client_cls.assert_called_once_with(
            url="https://api.linear.app/graphql",
            headers={"Authorization": "token"},
            label="Linear",
            http=http,
        )
        client_cls.return_value.execute.assert_called_once_with(
            "query Viewer { viewer { email } }",
            variables={"first": 1},
            page_size=10,
        )

    def test_linear_client_validate_queries_viewer(self) -> None:
        http = RecordingHTTP([{"data": {"viewer": {"email": "alice@example.com"}}}])
        client = LinearClient("token", http=http)

        self.assertEqual(client.validate(), {"email": "alice@example.com"})
        body = json_dict(http.calls[0]["json_body"])
        self.assertIn("LinearClientValidate", str(body.get("query") or ""))
        self.assertNotIn("\n    id\n", str(body.get("query") or ""))
        self.assertEqual(http.calls[0]["headers"], {"Authorization": "token"})

    def test_linear_client_validate_requires_viewer_email(self) -> None:
        http = RecordingHTTP([{"data": {"viewer": {}}}])
        client = LinearClient("token", http=http)

        with self.assertRaisesRegex(RuntimeError, "viewer.email"):
            client.validate()

    def test_linear_client_list_users_paginates(self) -> None:
        http = RecordingHTTP(
            [
                {
                    "data": {
                        "users": {
                            "nodes": [{"id": "U1", "name": "Alice"}],
                            "pageInfo": {
                                "hasNextPage": True,
                                "endCursor": "cursor-1",
                            },
                        }
                    }
                },
                {
                    "data": {
                        "users": {
                            "nodes": [{"id": "U2", "name": "Bob"}],
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": None,
                            },
                        }
                    }
                },
            ]
        )

        users = LinearClient("token", http=http).list_users(page_size=25)

        self.assertEqual([user["id"] for user in users], ["U1", "U2"])
        first_body = json_dict(http.calls[0]["json_body"])
        second_body = json_dict(http.calls[1]["json_body"])
        self.assertEqual(
            json_dict(first_body.get("variables")),
            {"first": 25, "after": None},
        )
        self.assertEqual(
            json_dict(second_body.get("variables")),
            {"first": 25, "after": "cursor-1"},
        )

    def test_json_cache_helpers_round_trip_and_parse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "cache.json"

            src.save_json_cache(path, {"b": {"name": "Bob"}, "a": {"name": "Alice"}})
            parsed = src.load_json_cache(path, parse=lambda value: str(value.get("name", "")))
            subset = src.load_json_subset(path, ["a", "missing"], parse=lambda value: value)

        self.assertEqual(parsed, {"a": "Alice", "b": "Bob"})
        self.assertEqual(subset, {"a": {"name": "Alice"}})

    def test_resolve_op_secret_ref_leaves_raw_values_alone(self) -> None:
        self.assertEqual(resolve_op_secret_ref(" raw-secret "), "raw-secret")

    def test_resolve_op_secret_ref_uses_one_password_client_for_refs(self) -> None:
        self.assertEqual(
            resolve_op_secret_ref("op://vault/item/field", client=FakeOnePasswordClient()),
            "resolved-secret",
        )

    def test_resolve_op_secret_ref_rejects_empty_values(self) -> None:
        with self.assertRaises(OnePasswordError):
            resolve_op_secret_ref("  ")


if __name__ == "__main__":
    unittest.main()
