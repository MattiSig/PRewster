"""Thin GitHub client: token discovery, GraphQL search, REST files and reviews, comments."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

API = "https://api.github.com"
USER_AGENT = "prewster/0.1"


class GitHubError(Exception):
    """Any failure talking to GitHub. status is the HTTP status, when there was one."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class AuthError(GitHubError):
    """No usable token, or the token was rejected."""


@dataclass
class PullRequest:
    repo: str
    number: int
    title: str
    url: str
    author: str
    author_avatar: str
    is_draft: bool
    created_at: datetime
    updated_at: datetime
    additions: int
    deletions: int
    changed_files: int
    review_decision: str | None
    mergeable: str
    head_ref: str
    base_ref: str
    body: str
    labels: list[tuple[str, str]] = field(default_factory=list)
    ci_state: str | None = None
    head_sha: str = ""
    reviewers: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"

    @property
    def owner(self) -> str:
        return self.repo.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.repo.split("/", 1)[1]

    @property
    def is_local(self) -> bool:
        """True for a branch in a local checkout rather than a pull request on GitHub."""
        return False


@dataclass
class Inbox:
    """One read of the notifications inbox."""
    changed: bool                 # False when GitHub answered 304 Not Modified
    last_modified: str | None     # send back as If-Modified-Since next time
    poll_interval: int            # seconds GitHub asks clients to wait (X-Poll-Interval)
    threads: dict[str, str] = field(default_factory=dict)  # pull request thread id -> updated_at


@dataclass
class ChangedFile:
    filename: str
    status: str
    additions: int
    deletions: int
    patch: str | None
    previous_filename: str | None = None
    blob_url: str = ""
    sha: str = ""  # the new blob; tells binary versions apart


@dataclass
class Comment:
    """A conversation comment, a review, or one comment in a review thread."""
    author: str
    avatar: str
    body: str
    created_at: datetime
    url: str
    state: str = ""  # reviews only: APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED


@dataclass
class ReviewThread:
    """Inline comments on one line of the diff."""
    path: str
    line: int | None  # None once the line is gone from the current diff (outdated)
    original_line: int | None
    side: str  # LEFT (old file) or RIGHT (new file)
    resolved: bool
    outdated: bool
    diff_hunk: str
    comments: list[Comment]

    @property
    def created_at(self) -> datetime:
        return self.comments[0].created_at


@dataclass
class Conversation:
    comments: list[Comment]  # conversation comments and reviews, oldest first
    threads: list[ReviewThread]


SEARCH_QUERY = """
query($q: String!, $n: Int!) {
  viewer { login avatarUrl }
  search(query: $q, type: ISSUE, first: $n) {
    issueCount
    nodes {
      ... on PullRequest {
        number title url isDraft createdAt updatedAt
        additions deletions changedFiles reviewDecision mergeable
        headRefName baseRefName bodyText
        author { login avatarUrl }
        repository { nameWithOwner }
        labels(first: 10) { nodes { name color } }
        commits(last: 1) { nodes { commit { oid statusCheckRollup { state } } } }
        reviewRequests(first: 20) {
          nodes { requestedReviewer { ... on User { login } ... on Team { name } } }
        }
      }
    }
  }
}
"""


_AUTHOR = "author { login avatarUrl }"
CONVERSATION_QUERY = f"""
query($owner: String!, $name: String!, $number: Int!) {{
  repository(owner: $owner, name: $name) {{
    pullRequest(number: $number) {{
      comments(last: 100) {{ nodes {{ {_AUTHOR} bodyText createdAt url }} }}
      reviews(last: 100) {{ nodes {{ {_AUTHOR} bodyText state submittedAt createdAt url }} }}
      reviewThreads(last: 100) {{
        nodes {{
          isResolved isOutdated path line originalLine diffSide
          comments(first: 50) {{ nodes {{ {_AUTHOR} bodyText createdAt url diffHunk }} }}
        }}
      }}
    }}
  }}
}}
"""


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def discover_token() -> str:
    """Use GITHUB_TOKEN / GH_TOKEN if set, otherwise ask the gh CLI."""
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(name, "").strip()
        if token:
            return token
    gh = shutil.which("gh")
    if not gh:
        raise AuthError("No GITHUB_TOKEN set and the gh CLI is not installed.")
    try:
        proc = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuthError(f"Could not run gh auth token: {exc}") from exc
    token = proc.stdout.strip()
    if proc.returncode != 0 or not token:
        raise AuthError("gh is not logged in. Run: gh auth login")
    return token


class GitHubClient:
    def __init__(self, token: str | None = None):
        self._token = token
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        })
        self.login: str | None = None
        self.avatar_url = ""

    # -- auth -------------------------------------------------------------

    def _ensure_token(self) -> None:
        if self._token is None:
            self._token = discover_token()
            self._session.headers["Authorization"] = f"Bearer {self._token}"

    def reset_token(self) -> None:
        self._token = None
        self._session.headers.pop("Authorization", None)

    # -- low level --------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        self._ensure_token()
        kwargs.setdefault("timeout", 30)
        try:
            resp = self._session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise GitHubError(f"Network error: {exc}") from exc
        if resp.status_code == 401:
            self.reset_token()
            raise AuthError("GitHub rejected the token (401). Run: gh auth login")
        if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
            raise GitHubError("GitHub API rate limit exceeded; try again later.")
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("message", "")
            except ValueError:
                pass
            raise GitHubError(f"GitHub returned {resp.status_code} {detail}".strip(), resp.status_code)
        return resp

    def graphql(self, query: str, variables: dict) -> dict:
        resp = self._request("POST", f"{API}/graphql", json={"query": query, "variables": variables})
        data = resp.json()
        if data.get("errors"):
            msg = "; ".join(e.get("message", "?") for e in data["errors"])
            raise GitHubError(f"GraphQL error: {msg}")
        return data["data"]

    def rest(self, method: str, path: str, **kwargs) -> requests.Response:
        return self._request(method, f"{API}{path}", **kwargs)

    def fetch_bytes(self, url: str) -> bytes:
        return self._request("GET", url).content

    # -- high level -------------------------------------------------------

    def notifications(self, last_modified: str | None = None) -> Inbox:
        """Conditional read of the notifications inbox: the newest 50 threads, read or not.

        Made for polling: a 304 (nothing changed since last_modified) does not count
        against the rate limit. Only pull request threads are returned. Raises
        GitHubError with status 403 or 404 when the token cannot read notifications
        (fine-grained personal access tokens, for one).
        """
        headers = {"If-Modified-Since": last_modified} if last_modified else {}
        resp = self.rest("GET", "/notifications", params={"all": "true", "per_page": 50}, headers=headers)
        try:
            interval = int(resp.headers.get("X-Poll-Interval", 60))
        except ValueError:
            interval = 60
        if resp.status_code == 304:
            return Inbox(False, last_modified, interval)
        threads = {str(t["id"]): str(t.get("updated_at", "")) for t in resp.json()
                   if (t.get("subject") or {}).get("type") == "PullRequest"}
        return Inbox(True, resp.headers.get("Last-Modified") or last_modified, interval, threads)

    def search_pull_requests(self, query: str, max_results: int = 50) -> list[PullRequest]:
        data = self.graphql(SEARCH_QUERY, {"q": query, "n": max_results})
        self.login = data["viewer"]["login"]
        self.avatar_url = data["viewer"].get("avatarUrl") or ""
        prs: list[PullRequest] = []
        for node in data["search"]["nodes"]:
            if not node or "number" not in node:
                continue
            commits = node.get("commits", {}).get("nodes") or []
            commit = commits[0]["commit"] if commits else {}
            rollup = commit.get("statusCheckRollup") or {}
            author = node.get("author") or {}
            reviewers = []
            for rr in (node.get("reviewRequests", {}).get("nodes") or []):
                who = rr.get("requestedReviewer") or {}
                reviewers.append(who.get("login") or who.get("name") or "?")
            prs.append(PullRequest(
                repo=node["repository"]["nameWithOwner"],
                number=node["number"],
                title=node["title"],
                url=node["url"],
                author=author.get("login", "ghost"),
                author_avatar=author.get("avatarUrl", ""),
                is_draft=bool(node.get("isDraft")),
                created_at=_parse_time(node["createdAt"]),
                updated_at=_parse_time(node["updatedAt"]),
                additions=node.get("additions", 0),
                deletions=node.get("deletions", 0),
                changed_files=node.get("changedFiles", 0),
                review_decision=node.get("reviewDecision"),
                mergeable=node.get("mergeable", "UNKNOWN"),
                head_ref=node.get("headRefName", ""),
                base_ref=node.get("baseRefName", ""),
                body=node.get("bodyText") or "",
                labels=[(l["name"], l["color"]) for l in (node.get("labels", {}).get("nodes") or [])],
                ci_state=rollup.get("state"),
                head_sha=commit.get("oid", ""),
                reviewers=reviewers,
            ))
        prs.sort(key=lambda p: p.updated_at, reverse=True)
        return prs

    def list_files(self, repo: str, number: int, max_files: int = 1000) -> list[ChangedFile]:
        files: list[ChangedFile] = []
        page = 1
        while len(files) < max_files:
            resp = self.rest("GET", f"/repos/{repo}/pulls/{number}/files",
                             params={"per_page": 100, "page": page})
            chunk = resp.json()
            for item in chunk:
                files.append(ChangedFile(
                    filename=item["filename"],
                    status=item.get("status", "modified"),
                    additions=item.get("additions", 0),
                    deletions=item.get("deletions", 0),
                    patch=item.get("patch"),
                    previous_filename=item.get("previous_filename"),
                    blob_url=item.get("blob_url", ""),
                    sha=item.get("sha") or "",
                ))
            if len(chunk) < 100:
                break
            page += 1
        return files

    def conversation(self, repo: str, number: int) -> Conversation:
        """Conversation comments, reviews and inline review threads (the newest 100 of each)."""
        owner, name = repo.split("/", 1)
        data = self.graphql(CONVERSATION_QUERY, {"owner": owner, "name": name, "number": number})
        pr = (data.get("repository") or {}).get("pullRequest") or {}

        def comment(node: dict, when: str = "createdAt") -> Comment:
            author = node.get("author") or {}
            return Comment(author=author.get("login", "ghost"), avatar=author.get("avatarUrl", ""),
                           body=(node.get("bodyText") or "").strip(),
                           created_at=_parse_time(node.get(when) or node["createdAt"]),
                           url=node.get("url", ""), state=node.get("state", ""))

        items = [comment(n) for n in (pr.get("comments") or {}).get("nodes") or [] if n]
        for node in (pr.get("reviews") or {}).get("nodes") or []:
            # A bare COMMENTED review only wraps inline comments, which show up as threads.
            if node and node.get("state") != "PENDING" and (node.get("bodyText") or node.get("state") != "COMMENTED"):
                items.append(comment(node, "submittedAt"))
        items.sort(key=lambda c: c.created_at)

        threads: list[ReviewThread] = []
        for node in (pr.get("reviewThreads") or {}).get("nodes") or []:
            nodes = [n for n in (node or {}).get("comments", {}).get("nodes") or [] if n]
            if not nodes:
                continue
            threads.append(ReviewThread(
                path=node["path"], line=node.get("line"), original_line=node.get("originalLine"),
                side=node.get("diffSide") or "RIGHT", resolved=bool(node.get("isResolved")),
                outdated=bool(node.get("isOutdated")), diff_hunk=nodes[0].get("diffHunk") or "",
                comments=[comment(n) for n in nodes]))
        threads.sort(key=lambda t: t.created_at)
        return Conversation(items, threads)

    def submit_review(self, repo: str, number: int, event: str, body: str = "") -> dict:
        """event is APPROVE, REQUEST_CHANGES or COMMENT."""
        payload: dict = {"event": event}
        if body.strip():
            payload["body"] = body.strip()
        elif event != "APPROVE":
            raise GitHubError("A comment is required for this review type.")
        resp = self.rest("POST", f"/repos/{repo}/pulls/{number}/reviews", json=payload)
        return resp.json()
