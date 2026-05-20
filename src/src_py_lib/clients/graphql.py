"""Shared GraphQL client primitives."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from src_py_lib.utils.http import HTTPClient, HTTPClientError
from src_py_lib.utils.json_types import JSONDict, json_dict

_OPERATION_NAME_RE = re.compile(r"\b(?:query|mutation|subscription)\s+(\w+)")


class GraphQLError(RuntimeError):
    """Raised for GraphQL transport or application errors."""


@dataclass
class GraphQLClient:
    """POST JSON GraphQL operations and return the `data` object."""

    url: str
    headers: dict[str, str]
    label: str
    http: HTTPClient = field(default_factory=HTTPClient)
    tolerate_partial_errors: bool = False

    def execute(self, query: str, variables: JSONDict | None = None) -> JSONDict:
        body = {"query": query, "variables": variables or {}}
        try:
            payload = self.http.json("POST", self.url, headers=self.headers, json_body=body)
        except HTTPClientError as exception:
            raise GraphQLError(f"{self.label} GraphQL request failed: {exception}") from exception
        errors = payload.get("errors")
        data = json_dict(payload.get("data"))
        if errors and not (self.tolerate_partial_errors and data):
            raise GraphQLError(f"{self.label} GraphQL errors: {errors}")
        return data


def operation_name(query: str) -> str:
    """Extract the operation name from a GraphQL document."""
    match = _OPERATION_NAME_RE.search(query)
    return match.group(1) if match else "anonymous"


def aliased_batched_query(
    keys: list[str],
    *,
    batch_size: int,
    build_alias: Callable[[int, str], str | None],
    parse_node: Callable[[JSONDict], object | None],
    post: Callable[[str], JSONDict],
) -> dict[str, object]:
    """Look up many keys with GraphQL aliases in fixed-size batches."""
    results: dict[str, object] = {}
    for chunk_start in range(0, len(keys), batch_size):
        chunk = keys[chunk_start : chunk_start + batch_size]
        parts: list[str] = []
        for index, key in enumerate(chunk):
            alias = build_alias(index, key)
            if alias is not None:
                parts.append(f"q{index}: {alias}")
        if not parts:
            continue
        data = post("query { " + " ".join(parts) + " }")
        for index, key in enumerate(chunk):
            node = json_dict(data.get(f"q{index}"))
            if not node:
                continue
            value = parse_node(node)
            if value is not None:
                results[key] = value
    return results
