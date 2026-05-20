"""Linear GraphQL API client."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TypedDict, cast

from src_py_lib.clients.graphql import GraphQLClient, aliased_batched_query
from src_py_lib.utils.http import HTTPClient
from src_py_lib.utils.json_types import JSONDict, json_dict, json_list, json_str

LINEAR_API_URL = "https://api.linear.app/graphql"
DEFAULT_ISSUE_BATCH_SIZE = 40


class LinearIssue(TypedDict):
    identifier: str
    title: str
    url: str
    state: str
    creator: str


@dataclass
class LinearClient:
    token: str
    http: HTTPClient = field(default_factory=HTTPClient)

    def graphql(self, query: str, variables: JSONDict | None = None) -> JSONDict:
        return GraphQLClient(
            url=LINEAR_API_URL,
            headers={"Authorization": self.token},
            label="Linear",
            http=self.http,
        ).execute(query, variables)

    def get_issues(
        self, issue_ids: list[str], *, batch_size: int = DEFAULT_ISSUE_BATCH_SIZE
    ) -> dict[str, LinearIssue]:
        return cast(
            dict[str, LinearIssue],
            aliased_batched_query(
                issue_ids,
                batch_size=batch_size,
                build_alias=_build_issue_alias,
                parse_node=_project_issue,
                post=self.graphql,
            ),
        )


def _build_issue_alias(_index: int, issue_id: str) -> str:
    return (
        f"issue(id: {json.dumps(issue_id)}) "
        "{ identifier title url state { name } creator { name } }"
    )


def _project_issue(node: JSONDict) -> LinearIssue | None:
    identifier = json_str(node, "identifier")
    title = json_str(node, "title")
    url = json_str(node, "url")
    if not (identifier or title or url):
        return None
    return {
        "identifier": identifier,
        "title": title,
        "url": url,
        "state": json_str(json_dict(node.get("state")), "name"),
        "creator": json_str(json_dict(node.get("creator")), "name"),
    }


def project_users(data: JSONDict) -> list[JSONDict]:
    """Project a Linear `users` connection response to its node list."""
    return [json_dict(node) for node in json_list(json_dict(data.get("users")).get("nodes"))]
