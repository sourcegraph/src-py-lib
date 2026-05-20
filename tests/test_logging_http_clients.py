"""Focused tests for logging, HTTP, and API-client primitives."""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
import urllib.request
from pathlib import Path
from typing import Any

from src_py_lib.clients.github import GitHubClient, graphql_api_url, pr_ref_from_url
from src_py_lib.clients.sourcegraph import SourcegraphClient
from src_py_lib.http import HTTPClient, RetryConfig
from src_py_lib.logging import LoggingConfig, configure_logging, emit_event, log_context


class FakeResponse:
    """Tiny context-manager response for HTTPClient tests."""

    status = 200

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


class RecordingHTTP(HTTPClient):
    """HTTPClient test double that records JSON request arguments."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []

    def json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        query: dict[str, str | int | float | bool | None] | None = None,
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
        return {"data": {"viewer": {"username": "alice"}}}


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
                with log_context(command="unit-test"):
                    emit_event("example", logger_name=logger_name, answer=42)
            finally:
                logger = logging.getLogger(logger_name)
                for handler in list(logger.handlers):
                    logger.removeHandler(handler)
                    handler.close()

            rows = [json.loads(line) for line in event_file.read_text().splitlines()]
            self.assertEqual(rows[-1]["event"], "example")
            self.assertEqual(rows[-1]["run_id"], "test-run")
            self.assertEqual(rows[-1]["command"], "unit-test")
            self.assertEqual(rows[-1]["answer"], 42)


class HTTPClientTest(unittest.TestCase):
    def test_json_request_adds_timeout_query_headers_and_decodes_object(self) -> None:
        seen: dict[str, Any] = {}

        def opener(request: urllib.request.Request, *, timeout: float) -> FakeResponse:
            seen["url"] = request.full_url
            seen["timeout"] = timeout
            seen["authorization"] = request.headers["Authorization"]
            seen["body"] = request.data
            return FakeResponse(b'{"ok": true}')

        client = HTTPClient(timeout_seconds=12, retry=RetryConfig(max_attempts=1), opener=opener)
        payload = client.json(
            "POST",
            "https://example.com/api",
            headers={"Authorization": "Bearer token"},
            query={"limit": 10, "skip": None},
            json_body={"hello": "world"},
        )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(seen["url"], "https://example.com/api?limit=10")
        self.assertEqual(seen["timeout"], 12)
        self.assertEqual(seen["authorization"], "Bearer token")
        self.assertEqual(json.loads(seen["body"]), {"hello": "world"})


class ClientTest(unittest.TestCase):
    def test_sourcegraph_client_builds_graphql_request(self) -> None:
        http = RecordingHTTP()
        client = SourcegraphClient("https://sourcegraph.example.com/", "token", http=http)
        data = client.graphql("query Viewer { viewer { username } }")

        self.assertEqual(data, {"viewer": {"username": "alice"}})
        self.assertEqual(http.calls[0]["method"], "POST")
        self.assertEqual(http.calls[0]["url"], "https://sourcegraph.example.com/.api/graphql")
        self.assertEqual(http.calls[0]["headers"], {"Authorization": "token token"})

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


if __name__ == "__main__":
    unittest.main()
