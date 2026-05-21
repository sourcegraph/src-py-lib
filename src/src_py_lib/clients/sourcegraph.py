"""Sourcegraph GraphQL API client."""

from __future__ import annotations

from dataclasses import dataclass, field

from src_py_lib.clients.graphql import GraphQLClient
from src_py_lib.utils.config import Config, config_field
from src_py_lib.utils.http import HTTPClient
from src_py_lib.utils.json_types import JSONDict, json_dict

DEFAULT_SOURCEGRAPH_ENDPOINT = "https://sourcegraph.com"
SOURCEGRAPH_VALIDATE_QUERY = """
query SourcegraphClientValidate {
  currentUser {
    username
  }
}
"""


class SourcegraphClientConfig(Config):
    """Config fields needed to build a Sourcegraph API client."""

    src_endpoint: str = config_field(
        DEFAULT_SOURCEGRAPH_ENDPOINT,
        env_var="SRC_ENDPOINT",
        cli_flag="--src-endpoint",
        metavar="URL",
        help=f"Sourcegraph instance URL (default: {DEFAULT_SOURCEGRAPH_ENDPOINT}).",
    )
    src_access_token: str = config_field(
        "",
        env_var="SRC_ACCESS_TOKEN",
        cli_flag="--src-access-token",
        metavar="TOKEN",
        help="Sourcegraph access token or op:// secret reference.",
        secret=True,
        required=True,
    )


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

    def validate(self) -> JSONDict:
        """Validate the token with a cheap current user query and return the user."""
        current_user = json_dict(self.graphql(SOURCEGRAPH_VALIDATE_QUERY).get("currentUser"))
        if not current_user.get("username"):
            raise RuntimeError(
                "Sourcegraph current user response did not include currentUser.username."
            )
        return current_user

    def _client(self) -> GraphQLClient:
        return GraphQLClient(
            url=f"{self.endpoint.rstrip('/')}/.api/graphql",
            headers={"Authorization": f"token {self.token}"},
            label="Sourcegraph",
            http=self.http,
        )


def sourcegraph_client_from_config(config: SourcegraphClientConfig) -> SourcegraphClient:
    """Return a Sourcegraph API client from shared Sourcegraph Config fields."""
    return SourcegraphClient(
        endpoint=config.src_endpoint,
        token=config.src_access_token,
    )
