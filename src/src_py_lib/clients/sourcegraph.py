"""Sourcegraph GraphQL API client."""

from __future__ import annotations

import base64
import collections
import json
import queue
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit

from src_py_lib.clients.graphql import GraphQLClient, stream_connection_nodes
from src_py_lib.utils.config import Config, config_field
from src_py_lib.utils.http import HTTPClient, HTTPClientError, HTTPResponse
from src_py_lib.utils.json_types import JSONDict, JSONValue, json_dict, json_list
from src_py_lib.utils.logging import submit_with_log_context
from src_py_lib.utils.telemetry import (
    current_traceparent_header,
    set_current_span_attributes,
    traceparent_fields,
)

SOURCEGRAPH_EXTERNAL_SERVICE_NODE_TYPE: Final[str] = "ExternalService"
SOURCEGRAPH_REPOSITORY_NODE_TYPE: Final[str] = "Repository"
REQUEST_TRACE_HEADER: Final[str] = "X-Sourcegraph-Request-Trace"
TRACEPARENT_HEADER: Final[str] = "traceparent"
TRACE_ID_RESPONSE_HEADER: Final[str] = "x-trace"
TRACE_SPAN_RESPONSE_HEADER: Final[str] = "x-trace-span"
TRACE_URL_RESPONSE_HEADER: Final[str] = "x-trace-url"
JAEGER_TRACE_RETRY_DELAYS_SECONDS: Final[tuple[float, ...]] = (0.0, 2.0, 5.0)
RETRYABLE_JAEGER_TRACE_STATUS_CODES: Final[frozenset[int]] = frozenset({404, 502, 503, 504})
SOURCEGRAPH_VALIDATE_QUERY = """
query SourcegraphClientValidate {
  currentUser {
    username
  }
}
"""


class SourcegraphJaegerTraceError(RuntimeError):
    """Raised when a Sourcegraph Jaeger/debug trace cannot be fetched."""


@dataclass(frozen=True)
class SourcegraphTrace:
    """Trace metadata Sourcegraph returned for one traced request."""

    trace_id: str
    span_id: str | None = None
    trace_url: str | None = None
    parent_trace_id: str | None = None
    parent_span_id: str | None = None

    def to_json(self) -> JSONDict:
        payload: JSONDict = {"trace_id": self.trace_id}
        if self.span_id is not None:
            payload["span_id"] = self.span_id
        if self.trace_url is not None:
            payload["trace_url"] = self.trace_url
        if self.parent_trace_id is not None:
            payload["parent_trace_id"] = self.parent_trace_id
        if self.parent_span_id is not None:
            payload["parent_span_id"] = self.parent_span_id
        return payload


@dataclass(frozen=True)
class SourcegraphJaegerTraceSummary:
    """Compact summary of one Sourcegraph Jaeger/debug trace."""

    trace: SourcegraphTrace
    jaeger_found: bool
    span_count: int = 0
    hot_operations: tuple[JSONDict, ...] = ()
    graphql_operations: tuple[JSONDict, ...] = ()
    errored_spans: tuple[JSONDict, ...] = ()
    error: str = ""

    def to_json(self) -> JSONDict:
        payload = self.trace.to_json()
        payload["jaeger_found"] = self.jaeger_found
        if not self.jaeger_found:
            payload["error"] = self.error
            return payload
        payload["span_count"] = self.span_count
        payload["hot_operations"] = [dict(operation) for operation in self.hot_operations]
        payload["graphql_operations"] = [dict(operation) for operation in self.graphql_operations]
        payload["errored_spans"] = [dict(span) for span in self.errored_spans]
        return payload


def normalize_sourcegraph_endpoint(endpoint: str, *, require_https: bool = False) -> str:
    """Return a stable Sourcegraph base URL, or raise ValueError."""
    normalized_endpoint = endpoint.strip().rstrip("/")
    endpoint_parts = urlsplit(normalized_endpoint)
    if require_https and endpoint_parts.scheme != "https":
        raise ValueError(
            f"Sourcegraph endpoint must be an https:// URL (got {endpoint_parts.scheme!r})"
        )
    if endpoint_parts.scheme not in {"http", "https"}:
        raise ValueError(
            "Sourcegraph endpoint must be an http:// or https:// URL "
            f"(got {endpoint_parts.scheme!r})"
        )
    if not endpoint_parts.hostname:
        raise ValueError(
            f"could not parse hostname from Sourcegraph endpoint {normalized_endpoint!r}"
        )
    return normalized_endpoint


def encode_sourcegraph_node_id(node_type: str, database_id: int) -> str:
    """Return a Sourcegraph opaque GraphQL Node ID for `node_type:database_id`."""
    raw = f"{node_type}:{database_id}".encode()
    return base64.b64encode(raw).decode()


def decode_sourcegraph_node_id(node_type: str, graphql_id: str) -> int:
    """Return the database ID from a Sourcegraph opaque GraphQL Node ID."""
    try:
        raw = base64.b64decode(graphql_id, validate=True).decode()
    except (ValueError, UnicodeDecodeError) as exception:
        raise ValueError(f"not a valid base64 GraphQL Node ID: {graphql_id!r}") from exception
    decoded_node_type, separator, database_id = raw.partition(":")
    if not separator or decoded_node_type != node_type:
        raise ValueError(f"not a {node_type} Node ID: {graphql_id!r} (decoded: {raw!r})")
    try:
        return int(database_id)
    except ValueError as exception:
        raise ValueError(
            f"{node_type} Node ID has non-integer suffix: {graphql_id!r} (decoded: {raw!r})"
        ) from exception


def decode_external_service_id(graphql_id: str) -> int:
    """Return the database ID from an opaque ExternalService GraphQL Node ID."""
    return decode_sourcegraph_node_id(SOURCEGRAPH_EXTERNAL_SERVICE_NODE_TYPE, graphql_id)


def encode_repository_id(database_id: int) -> str:
    """Return an opaque Repository GraphQL Node ID from a database ID."""
    return encode_sourcegraph_node_id(SOURCEGRAPH_REPOSITORY_NODE_TYPE, database_id)


def decode_repository_id(graphql_id: str) -> int:
    """Return the database ID from an opaque Repository GraphQL Node ID."""
    return decode_sourcegraph_node_id(SOURCEGRAPH_REPOSITORY_NODE_TYPE, graphql_id)


class SourcegraphClientConfig(Config):
    """Config fields needed to build a Sourcegraph API client."""

    src_endpoint: str = config_field(
        default="",
        env_var="SRC_ENDPOINT",
        cli_flag="--src-endpoint",
        metavar="URL",
        help=(
            "Sourcegraph instance URL\n"
            "Required as either env var or arg. Recommended to set SRC_ENDPOINT env var"
        ),
        help_group="Sourcegraph",
        required=True,
    )
    src_access_token: str = config_field(
        default="",
        env_var="SRC_ACCESS_TOKEN",
        cli_flag="--src-access-token",
        metavar="TOKEN",
        help=(
            "Sourcegraph access token, or op:// secret reference\n"
            "Required as either env var or arg. Recommended to set SRC_ACCESS_TOKEN env var"
        ),
        help_group="Sourcegraph",
        secret=True,
        required=True,
    )


@dataclass
class SourcegraphClient:
    """Small Sourcegraph GraphQL client.

    `endpoint` should be the instance base URL, for example
    `https://sourcegraph.example.com`.

    Plain HTTP endpoints are rejected unless `allow_insecure_http=True` is set
    for local development.

    Set `fetch_sg_traces=True` to ask Sourcegraph to retain traces for each
    GraphQL request. Traced requests are available through `drain_traces()` and
    can be fetched from the instance's Jaeger/debug endpoint with
    `stream_jaeger_trace_summaries()`.
    """

    endpoint: str
    token: str
    http: HTTPClient = field(default_factory=HTTPClient)
    fetch_sg_traces: bool = False
    allow_insecure_http: bool = False
    _traces: queue.Queue[SourcegraphTrace] = field(
        default_factory=lambda: queue.Queue[SourcegraphTrace](), init=False, repr=False
    )

    def __post_init__(self) -> None:
        self.endpoint = normalize_sourcegraph_endpoint(
            self.endpoint,
            require_https=not self.allow_insecure_http,
        )

    def graphql(
        self,
        query: str,
        variables: Mapping[str, JSONValue] | None = None,
        *,
        follow_pages: bool = True,
        page_size: int | None = None,
        first_variable: str = "first",
        after_variable: str = "after",
    ) -> JSONDict:
        """Execute one Sourcegraph GraphQL operation.

        Set `follow_pages=False` when the caller owns pagination, such as
        aliased queries with one cursor per alias.
        """
        return self._client().execute(
            query,
            variables,
            follow_pages=follow_pages,
            page_size=page_size,
            first_variable=first_variable,
            after_variable=after_variable,
        )

    def stream_connection_nodes(
        self,
        query: str,
        variables: Mapping[str, JSONValue] | None = None,
        *,
        connection_path: Sequence[str],
        page_size: int | None = None,
        first_variable: str = "first",
        after_variable: str = "after",
    ) -> Iterator[JSONDict]:
        """Stream one Sourcegraph GraphQL connection's nodes."""
        return stream_connection_nodes(
            self.graphql,
            query,
            variables,
            connection_path=connection_path,
            page_size=page_size,
            first_variable=first_variable,
            after_variable=after_variable,
        )

    def validate(self) -> JSONDict:
        """Validate the token with a cheap current user query and return the user."""
        current_user = json_dict(self.graphql(SOURCEGRAPH_VALIDATE_QUERY).get("currentUser"))
        if not current_user.get("username"):
            raise RuntimeError(
                "Sourcegraph current user response did not include currentUser.username."
            )
        return current_user

    def drain_traces(self) -> list[SourcegraphTrace]:
        """Return traced request metadata recorded since the last drain."""
        traces: list[SourcegraphTrace] = []
        while True:
            try:
                traces.append(self._traces.get_nowait())
            except queue.Empty:
                return traces

    def stream_jaeger_trace_summaries(
        self,
        traces: Iterable[SourcegraphTrace] | None = None,
        *,
        retry_delays_seconds: Sequence[float] = JAEGER_TRACE_RETRY_DELAYS_SECONDS,
        parallelism: int = 8,
    ) -> Iterator[SourcegraphJaegerTraceSummary]:
        """Yield compact Jaeger/debug summaries for traced Sourcegraph requests."""
        if parallelism < 1:
            raise ValueError("parallelism must be at least 1")
        pending_traces = list(self.drain_traces() if traces is None else traces)
        with ThreadPoolExecutor(
            max_workers=parallelism,
            thread_name_prefix="SourcegraphJaegerTrace",
        ) as executor:
            futures = [
                submit_with_log_context(
                    executor,
                    self.fetch_jaeger_trace_summary,
                    trace,
                    retry_delays_seconds=retry_delays_seconds,
                )
                for trace in pending_traces
            ]
            for future in as_completed(futures):
                yield future.result()

    def fetch_jaeger_trace_summary(
        self,
        trace: SourcegraphTrace | str,
        *,
        retry_delays_seconds: Sequence[float] = JAEGER_TRACE_RETRY_DELAYS_SECONDS,
    ) -> SourcegraphJaegerTraceSummary:
        """Fetch one Jaeger/debug trace and return a compact summary."""
        trace_metadata = trace if isinstance(trace, SourcegraphTrace) else SourcegraphTrace(trace)
        try:
            jaeger_trace = self.fetch_jaeger_trace(
                trace_metadata.trace_id,
                retry_delays_seconds=retry_delays_seconds,
            )
        except SourcegraphJaegerTraceError as error:
            return SourcegraphJaegerTraceSummary(
                trace=trace_metadata,
                jaeger_found=False,
                error=str(error),
            )
        return summarize_jaeger_trace(trace_metadata, jaeger_trace)

    def fetch_jaeger_trace(
        self,
        trace_id: str,
        *,
        retry_delays_seconds: Sequence[float] = JAEGER_TRACE_RETRY_DELAYS_SECONDS,
    ) -> JSONDict:
        """Fetch a raw Jaeger/debug trace from the Sourcegraph instance."""
        url = f"{self.endpoint}/-/debug/jaeger/api/traces/{trace_id}"
        last_error = "trace not found"
        for delay_seconds in retry_delays_seconds:
            if delay_seconds > 0:
                time.sleep(delay_seconds)
            try:
                payload = self.http.json("GET", url, headers=self._authorization_headers())
            except HTTPClientError as error:
                last_error = sourcegraph_trace_fetch_error(error)
                if (
                    error.status_code is None
                    or error.status_code in RETRYABLE_JAEGER_TRACE_STATUS_CODES
                ):
                    continue
                raise SourcegraphJaegerTraceError(last_error) from error
            for trace_value in json_list(payload.get("data")):
                jaeger_trace = json_dict(trace_value)
                if jaeger_trace:
                    return jaeger_trace
            errors = payload.get("errors")
            last_error = json.dumps(errors) if errors else "trace not found"
        raise SourcegraphJaegerTraceError(last_error)

    def _client(self) -> GraphQLClient:
        return GraphQLClient(
            url=f"{self.endpoint}/.api/graphql",
            headers=self._graphql_headers,
            label="Sourcegraph",
            http=self.http,
            response_hook=self._record_trace_response if self.fetch_sg_traces else None,
        )

    def _authorization_headers(self) -> dict[str, str]:
        return {"Authorization": f"token {self.token}"}

    def _graphql_headers(self) -> dict[str, str]:
        headers = self._authorization_headers()
        if self.fetch_sg_traces:
            headers[REQUEST_TRACE_HEADER] = "true"
            traceparent = current_traceparent_header()
            if traceparent is not None:
                headers[TRACEPARENT_HEADER] = traceparent
        return headers

    def _record_trace_response(
        self, response: HTTPResponse, request_headers: Mapping[str, str]
    ) -> None:
        trace = sourcegraph_trace_from_headers(response.headers, request_headers)
        if trace is not None:
            set_current_span_attributes(
                {
                    "sourcegraph.trace_id": trace.trace_id,
                    "sourcegraph.trace_url": trace.trace_url,
                    "sourcegraph.span_id": trace.span_id,
                }
            )
            self._traces.put(trace)


def sourcegraph_client_from_config(
    config: SourcegraphClientConfig,
    *,
    http: HTTPClient | None = None,
    fetch_sg_traces: bool = False,
) -> SourcegraphClient:
    """Return a Sourcegraph API client from shared Sourcegraph Config fields."""
    return SourcegraphClient(
        endpoint=config.src_endpoint,
        token=config.src_access_token,
        http=http or HTTPClient(),
        fetch_sg_traces=fetch_sg_traces,
    )


def sourcegraph_trace_from_headers(
    response_headers: Mapping[str, str], request_headers: Mapping[str, str]
) -> SourcegraphTrace | None:
    """Return Sourcegraph trace metadata from request/response headers."""
    trace_id = header_value(response_headers, TRACE_ID_RESPONSE_HEADER)
    if trace_id is None or not is_hex_identifier(trace_id, 32):
        return None
    span_id = header_value(response_headers, TRACE_SPAN_RESPONSE_HEADER)
    trace_url = header_value(response_headers, TRACE_URL_RESPONSE_HEADER)
    parent = traceparent_fields(header_value(request_headers, TRACEPARENT_HEADER))
    return SourcegraphTrace(
        trace_id=trace_id.lower(),
        span_id=span_id.lower() if span_id and is_hex_identifier(span_id, 16) else span_id,
        trace_url=trace_url,
        parent_trace_id=parent.get("trace_id"),
        parent_span_id=parent.get("span_id"),
    )


def is_hex_identifier(value: str, length: int) -> bool:
    """Return whether `value` is a non-zero hex identifier of `length` characters."""
    lowered = value.lower()
    return (
        len(lowered) == length
        and any(character != "0" for character in lowered)
        and all(character in "0123456789abcdef" for character in lowered)
    )


def header_value(headers: Mapping[str, str], name: str) -> str | None:
    """Return one header value by case-insensitive name."""
    lower_name = name.lower()
    for header_name, value in headers.items():
        if header_name.lower() == lower_name:
            return value
    return None


def sourcegraph_trace_fetch_error(error: HTTPClientError) -> str:
    """Return a concise, user-safe Jaeger trace fetch error."""
    if error.status_code is None:
        return str(error)
    return f"HTTP {error.status_code}" + (f": {error.body[:200]}" if error.body else "")


def summarize_jaeger_trace(
    trace_metadata: SourcegraphTrace, jaeger_trace: JSONDict
) -> SourcegraphJaegerTraceSummary:
    """Return a compact summary of one raw Jaeger trace payload."""
    spans = json_list(jaeger_trace.get("spans"))
    durations_by_operation: dict[str, list[float]] = collections.defaultdict(list)
    graphql_operations: collections.Counter[str] = collections.Counter()
    errored_spans: list[JSONDict] = []

    for span_value in spans:
        span = json_dict(span_value)
        if not span:
            continue
        operation = str(span.get("operationName") or "")
        duration_ms = float_value(span.get("duration")) / 1000.0
        durations_by_operation[operation].append(duration_ms)
        tags = jaeger_span_tags(span)
        operation_name = tags.get("graphql.operationName")
        if isinstance(operation_name, str):
            graphql_operations[operation_name] += 1
        if tags.get("error") in {True, "true", "True"}:
            errored_spans.append(
                {
                    "operation": operation,
                    "duration_ms": round(duration_ms, 1),
                    "description": json_scalar(tags.get("otel.status_description")),
                }
            )

    hot_operations: list[JSONDict] = [
        {
            "operation": operation,
            "count": len(durations),
            "sum_ms": round(sum(durations), 1),
            "max_ms": round(max(durations), 1),
        }
        for operation, durations in durations_by_operation.items()
    ]
    hot_operations.sort(key=jaeger_summary_operation_sum_ms, reverse=True)
    return SourcegraphJaegerTraceSummary(
        trace=trace_metadata,
        jaeger_found=True,
        span_count=len(spans),
        hot_operations=tuple(hot_operations[:10]),
        graphql_operations=tuple(
            {"operation": operation, "count": count}
            for operation, count in graphql_operations.most_common(10)
        ),
        errored_spans=tuple(errored_spans[:5]),
    )


def jaeger_summary_operation_sum_ms(operation: JSONDict) -> float:
    """Return the total duration for sorting compact Jaeger operation summaries."""
    return float_value(operation.get("sum_ms"))


def jaeger_span_tags(span: JSONDict) -> dict[str, object]:
    """Return Jaeger span tags keyed by tag name."""
    tags: dict[str, object] = {}
    for tag_value in json_list(span.get("tags")):
        tag = json_dict(tag_value)
        key = tag.get("key")
        if isinstance(key, str):
            tags[key] = tag.get("value")
    return tags


def float_value(value: object) -> float:
    """Return a JSON number as float, excluding booleans."""
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def json_scalar(value: object) -> JSONValue:
    """Return `value` if it is a JSON scalar; otherwise return None."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return None
