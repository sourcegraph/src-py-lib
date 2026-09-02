"""GitHub GraphQL API client."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TypedDict, cast
from urllib.parse import urlsplit

from src_py_lib.clients.graphql import GraphQLClient, aliased_batched_query
from src_py_lib.utils.http import HTTPClient
from src_py_lib.utils.json_types import JSONDict, json_dict, json_dicts, json_list, json_str

DEFAULT_GITHUB_URL = "https://github.com"
DEFAULT_PR_BATCH_SIZE = 50
GITHUB_VALIDATE_QUERY = """
query GitHubClientValidate {
  viewer {
    login
  }
}
"""
PULL_REQUEST_SEARCH_QUERY = """
query SearchPullRequests($query: String!, $first: Int!, $after: String) {
  search(query: $query, type: ISSUE, first: $first, after: $after) {
    nodes {
      ... on PullRequest {
        title
        url
        state
        createdAt
        mergedAt
        closedAt
        author { login }
        repository { nameWithOwner }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""
REST_SEARCH_PAGE_SIZE = 100
REST_SEARCH_RESULT_CAP = 1000
PR_REF_RE = re.compile(r"^(?P<owner>[^/]+)/(?P<repo>[^/#]+)#(?P<number>\d+)$")
PR_URL_RE = re.compile(
    r"https?://[^/\s)>|]+/(?P<owner>[^/\s)>|]+)/(?P<repo>[^/\s)>|]+)/pull/(?P<number>\d+)"
)


class PullRequest(TypedDict):
    title: str
    url: str
    state: str
    createdAt: str
    mergedAt: str
    closedAt: str
    author: str


class SearchedPullRequest(PullRequest):
    repository: str  # owner/name


@dataclass
class GitHubClient:
    token: str
    github_url: str = DEFAULT_GITHUB_URL
    http: HTTPClient = field(default_factory=HTTPClient)

    @classmethod
    def from_gh_cli(
        cls, *, github_url: str = DEFAULT_GITHUB_URL, http: HTTPClient | None = None
    ) -> GitHubClient:
        token = gh_cli_token(github_url=github_url)
        if not token:
            raise RuntimeError("No GitHub token from `gh auth token`.")
        return cls(token=token, github_url=github_url, http=http or HTTPClient())

    def graphql(self, query: str, variables: JSONDict | None = None) -> JSONDict:
        return GraphQLClient(
            url=graphql_api_url(self.github_url),
            headers={"Authorization": f"bearer {self.token}"},
            label="GitHub",
            http=self.http,
            tolerate_partial_errors=True,
        ).execute(query, variables)

    def validate(self) -> JSONDict:
        """Validate the token with a cheap viewer query and return the viewer."""
        viewer = json_dict(self.graphql(GITHUB_VALIDATE_QUERY).get("viewer"))
        if not viewer.get("login"):
            raise RuntimeError("GitHub viewer response did not include viewer.login.")
        return viewer

    def get_pull_requests(
        self, refs: list[str], *, batch_size: int = DEFAULT_PR_BATCH_SIZE
    ) -> dict[str, PullRequest]:
        return cast(
            dict[str, PullRequest],
            aliased_batched_query(
                refs,
                batch_size=batch_size,
                build_alias=_build_pr_alias,
                parse_node=_project_pull_request,
                post=self.graphql,
            ),
        )

    def search_pull_requests(
        self, query: str, *, page_size: int = REST_SEARCH_PAGE_SIZE
    ) -> list[SearchedPullRequest]:
        """Return pull requests matching a GitHub search, e.g. `author:x created:>=2026-05-01`.

        `is:pr` is added when absent. GitHub search returns at most 1000 results;
        narrow the query by date when that cap is hit.
        """
        if "is:pr" not in query:
            query = f"is:pr {query}"
        data = self.graphql(PULL_REQUEST_SEARCH_QUERY, {"query": query, "first": page_size})
        return [
            {
                **_pull_request_fields(node),
                "repository": json_str(json_dict(node.get("repository")), "nameWithOwner"),
            }
            for node in json_dicts(json_dict(data.get("search")).get("nodes"))
            if node
        ]

    def search_commits(self, query: str) -> list[JSONDict]:
        """Return commits matching a GitHub commit search, e.g. `author:x author-date:>=2026-05-01`.

        Uses the REST search API (no GraphQL equivalent), capped at 1000 results.
        """
        commits: list[JSONDict] = []
        page = 1
        while True:
            data = self.rest_get(
                "search/commits",
                {"q": query, "per_page": REST_SEARCH_PAGE_SIZE, "page": page},
            )
            items = json_list(data.get("items"))
            commits.extend(json_dict(item) for item in items)
            total = data.get("total_count")
            if len(items) < REST_SEARCH_PAGE_SIZE or not isinstance(total, int):
                return commits
            if len(commits) >= min(total, REST_SEARCH_RESULT_CAP):
                return commits
            page += 1

    def rest_get(self, path: str, query: Mapping[str, str | int] | None = None) -> JSONDict:
        return self.http.json(
            "GET",
            f"{rest_api_url(self.github_url)}/{path.lstrip('/')}",
            headers={
                "Authorization": f"bearer {self.token}",
                "Accept": "application/vnd.github+json",
            },
            query=dict(query or {}),
        )


def graphql_api_url(github_url: str = DEFAULT_GITHUB_URL) -> str:
    """Return the GraphQL API URL for github.com or a GitHub Enterprise host."""
    normalized = _normalize_github_url(github_url)
    split = urlsplit(normalized)
    if split.hostname == "github.com":
        return f"{split.scheme}://api.github.com/graphql"
    return f"{normalized}/api/graphql"


def rest_api_url(github_url: str = DEFAULT_GITHUB_URL) -> str:
    """Return the REST API base URL for github.com or a GitHub Enterprise host."""
    normalized = _normalize_github_url(github_url)
    split = urlsplit(normalized)
    if split.hostname == "github.com":
        return f"{split.scheme}://api.github.com"
    return f"{normalized}/api/v3"


def gh_cli_token(*, github_url: str = DEFAULT_GITHUB_URL) -> str | None:
    """Return `gh auth token`, or None when gh is unavailable/not logged in."""
    split = urlsplit(_normalize_github_url(github_url))
    command = ["gh", "auth", "token"]
    if split.hostname and split.hostname != "github.com":
        command.extend(["--hostname", split.netloc])
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    except OSError:
        return None
    except subprocess.SubprocessError:
        return None
    token = result.stdout.strip()
    return token if result.returncode == 0 and token else None


def _normalize_github_url(github_url: str) -> str:
    stripped = github_url.strip().rstrip("/")
    if "://" not in stripped:
        stripped = f"https://{stripped}"
    split = urlsplit(stripped)
    if split.scheme != "https":
        raise ValueError(f"GitHub URL must be an https:// URL (got {split.scheme!r})")
    if not split.hostname:
        raise ValueError(f"could not parse hostname from GitHub URL {stripped!r}")
    return stripped


def parse_pr_ref(ref: str) -> tuple[str, str, int]:
    match = PR_REF_RE.match(ref)
    if not match:
        raise ValueError(f"invalid GitHub PR ref: {ref!r}")
    return match.group("owner"), match.group("repo"), int(match.group("number"))


def pr_ref_from_url(url: str) -> str | None:
    match = PR_URL_RE.search(url)
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}#{match.group('number')}"


def _build_pr_alias(_index: int, ref: str) -> str | None:
    try:
        owner, repo, number = parse_pr_ref(ref)
    except ValueError:
        return None
    return (
        f"repository(owner: {json.dumps(owner)}, name: {json.dumps(repo)}) "
        f"{{ pullRequest(number: {number}) "
        "{ title url state createdAt mergedAt closedAt author { login } } }"
    )


def _project_pull_request(node: JSONDict) -> PullRequest | None:
    pull_request = json_dict(node.get("pullRequest"))
    if not pull_request:
        return None
    return _pull_request_fields(pull_request)


def _pull_request_fields(pull_request: JSONDict) -> PullRequest:
    return {
        "title": json_str(pull_request, "title"),
        "url": json_str(pull_request, "url"),
        "state": json_str(pull_request, "state"),
        "createdAt": json_str(pull_request, "createdAt"),
        "mergedAt": json_str(pull_request, "mergedAt"),
        "closedAt": json_str(pull_request, "closedAt"),
        "author": json_str(json_dict(pull_request.get("author")), "login"),
    }
