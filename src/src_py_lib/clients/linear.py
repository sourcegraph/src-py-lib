"""Linear GraphQL API client."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from src_py_lib.clients.graphql import GraphQLClient
from src_py_lib.utils.config import Config, config_field
from src_py_lib.utils.http import HTTPClient
from src_py_lib.utils.json_types import JSONDict, JSONValue, json_dict

LINEAR_API_URL = "https://api.linear.app/graphql"
LINEAR_VALIDATE_QUERY = """
query LinearClientValidate {
  viewer {
    email
  }
}
"""


class LinearClientConfig(Config):
    """Config fields needed to build a Linear API client."""

    linear_api_token: str = config_field(
        "",
        env_var="LINEAR_API_TOKEN",
        cli_flag="--linear-api-token",
        metavar="TOKEN",
        help="Linear API token or op:// secret reference.",
        secret=True,
        required=True,
    )


@dataclass
class LinearClient:
    token: str
    http: HTTPClient = field(default_factory=HTTPClient)

    def graphql(
        self,
        query: str,
        variables: Mapping[str, JSONValue] | None = None,
        *,
        page_size: int | None = None,
    ) -> JSONDict:

        return GraphQLClient(
            url=LINEAR_API_URL,
            headers={"Authorization": self.token},
            label="Linear",
            http=self.http,
        ).execute(query, variables=variables, page_size=page_size)

    def validate(self) -> JSONDict:
        """Validate the token with a cheap viewer query and return the viewer."""
        viewer = json_dict(self.graphql(LINEAR_VALIDATE_QUERY).get("viewer"))
        if not viewer.get("email"):
            raise RuntimeError("Linear viewer response did not include viewer.email.")
        return viewer


def linear_client_from_config(
    config: LinearClientConfig, *, http: HTTPClient | None = None
) -> LinearClient:
    """Return a Linear API client from shared Linear Config fields."""
    if http is None:
        return LinearClient(config.linear_api_token)
    return LinearClient(config.linear_api_token, http=http)
