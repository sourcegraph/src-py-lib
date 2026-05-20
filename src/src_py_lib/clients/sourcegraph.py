"""Sourcegraph GraphQL API client."""

from __future__ import annotations

from dataclasses import dataclass, field

from src_py_lib.clients.graphql import GraphQLClient
from src_py_lib.http import HTTPClient
from src_py_lib.json_types import JSONDict


@dataclass
class SourcegraphClient:
    """Small Sourcegraph GraphQL client.

    `endpoint` should be the instance base URL, for example
    `https://sourcegraph.example.com`.
    """

    endpoint: str
    token: str
    http: HTTPClient = field(default_factory=HTTPClient)

    def graphql(self, query: str, variables: JSONDict | None = None) -> JSONDict:
        return self._client().execute(query, variables)

    def _client(self) -> GraphQLClient:
        return GraphQLClient(
            url=f"{self.endpoint.rstrip('/')}/.api/graphql",
            headers={"Authorization": f"token {self.token}"},
            label="Sourcegraph",
            http=self.http,
        )
