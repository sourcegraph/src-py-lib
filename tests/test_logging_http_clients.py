"""Focused tests for logging, HTTP, and API-client primitives."""

from __future__ import annotations

import argparse
import io
import json
import logging
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

from src_py_lib.clients.github import GitHubClient, graphql_api_url, pr_ref_from_url
from src_py_lib.clients.google_sheets import GoogleSheetsClient
from src_py_lib.clients.graphql import GraphQLClient, GraphQLError, introspect_schema
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
    sourcegraph_client_from_config,
)
from src_py_lib.utils.config import (
    Config,
    ConfigError,
    add_config_arguments,
    config_env_file_from_args,
    config_field,
    config_overrides_from_args,
    config_parse_args,
    config_snapshot,
    load_config,
    load_config_env_file,
    load_config_from_args,
    resolve_config_refs,
)
from src_py_lib.utils.http import HTTPClient, HTTPClientError
from src_py_lib.utils.json_types import JSONDict, json_dict, json_list
from src_py_lib.utils.logging import (
    LoggingConfig,
    configure_logging,
    emit_event,
    log_context,
    startup_event,
)


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
        "",
        env_var="EXAMPLE_TOKEN",
        cli_flag="--token",
        metavar="TOKEN",
        help="Example token.",
        secret=True,
    )
    page_size: int = config_field(
        25,
        env_var="EXAMPLE_PAGE_SIZE",
        cli_flag="--page-size",
        metavar="N",
        help="Example page size.",
    )
    include_archived: bool = config_field(
        False,
        env_var="EXAMPLE_INCLUDE_ARCHIVED",
        cli_flag="--include-archived",
        help="Include archived examples.",
    )
    output_dir: Path = config_field(
        Path("out"),
        env_var="EXAMPLE_OUTPUT_DIR",
        cli_flag="--output-dir",
        metavar="PATH",
        help="Example output directory.",
    )
    labels: tuple[str, ...] = config_field(
        (),
        env_var="EXAMPLE_LABELS",
        cli_flag="--labels",
        metavar="CSV",
        help="Example labels.",
    )


class RequiredConfig(Config):
    """Config model with a required secret field."""

    token: str = config_field(
        "",
        env_var="REQUIRED_TOKEN",
        cli_flag="--token",
        metavar="TOKEN",
        help="Required token.",
        secret=True,
        required=True,
    )
    name: str = config_field(
        "",
        env_var="REQUIRED_NAME",
        cli_flag="--name",
        metavar="NAME",
        help="Non-secret required config name.",
    )


class LinearExampleConfig(LinearClientConfig):
    """Config model composed from Linear client fields and app fields."""

    page_size: int = config_field(
        25,
        env_var="LINEAR_EXAMPLE_PAGE_SIZE",
        cli_flag="--page-size",
        metavar="N",
        help="Example page size.",
    )


class SourcegraphExampleConfig(SourcegraphClientConfig):
    """Config model composed from Sourcegraph client fields and app fields."""

    repo_query: str = config_field(
        "",
        env_var="SOURCEGRAPH_EXAMPLE_REPO_QUERY",
        cli_flag="--repo-query",
        metavar="QUERY",
        help="Example Sourcegraph repository query.",
    )


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

        self.assertEqual(config.src_endpoint, "https://sourcegraph.com")
        self.assertEqual(config.src_access_token, "test-token")
        self.assertEqual(config.repo_query, "repo:example")
        self.assertEqual(client.endpoint, "https://sourcegraph.com")
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
            self.assertEqual(
                config_snapshot(config),
                {
                    "EXAMPLE_TOKEN": "provided",
                    "EXAMPLE_PAGE_SIZE": 40,
                    "EXAMPLE_INCLUDE_ARCHIVED": True,
                    "EXAMPLE_OUTPUT_DIR": str(base_dir / "from-cli"),
                    "EXAMPLE_LABELS": ["gamma", "delta"],
                },
            )

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


class LoggingTest(unittest.TestCase):
    def test_structured_event_file_includes_context_and_sanitized_terminal_omits_event(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            event_file = Path(directory) / "events.jsonl"
            logger_name = "src_py_lib_test_logging"
            configure_logging(
                LoggingConfig(
                    logger_name=logger_name,
                    terminal_level=logging.INFO,
                    event_file=event_file,
                    run_id="test-run",
                )
            )
            try:
                startup_event(
                    command="unit-test",
                    logger_name=logger_name,
                    git_commit="abc1234",
                )
                with log_context(command="unit-test"):
                    emit_event("example", logger_name=logger_name, answer=42)
            finally:
                logger = logging.getLogger(logger_name)
                for handler in list(logger.handlers):
                    logger.removeHandler(handler)
                    handler.close()

            rows = [json.loads(line) for line in event_file.read_text().splitlines()]
            startup = next(row for row in rows if row["event"] == "startup")
            self.assertEqual(startup["git_commit"], "abc1234")
            self.assertFalse(any("git_commit" in row for row in rows if row["event"] != "startup"))
            self.assertEqual(rows[-1]["event"], "example")
            self.assertEqual(rows[-1]["run_id"], "test-run")
            self.assertEqual(rows[-1]["command"], "unit-test")
            self.assertEqual(rows[-1]["answer"], 42)


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
            client.json("GET", "https://example.com/api")

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.body, "rate limited")


class ClientTest(unittest.TestCase):
    def test_sourcegraph_client_builds_graphql_request(self) -> None:
        http = RecordingHTTP([{"data": {"currentUser": {"username": "alice"}}}])
        client = SourcegraphClient("https://sourcegraph.example.com/", "token", http=http)
        data = client.graphql("query Viewer { currentUser { username } }")

        self.assertEqual(data, {"currentUser": {"username": "alice"}})
        self.assertEqual(http.calls[0]["method"], "POST")
        self.assertEqual(http.calls[0]["url"], "https://sourcegraph.example.com/.api/graphql")
        self.assertEqual(http.calls[0]["headers"], {"Authorization": "token token"})

    def test_sourcegraph_client_validate_queries_current_user(self) -> None:
        http = RecordingHTTP([{"data": {"currentUser": {"username": "alice"}}}])
        client = SourcegraphClient("https://sourcegraph.example.com/", "token", http=http)

        self.assertEqual(client.validate(), {"username": "alice"})
        body = json_dict(http.calls[0]["json_body"])
        self.assertIn("SourcegraphClientValidate", str(body.get("query") or ""))
        self.assertIn("currentUser", str(body.get("query") or ""))

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
