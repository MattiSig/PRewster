"""Your own work in local checkouts: branches ahead of the default branch, uncommitted edits."""

from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import CACHE_DIR
from .github import ChangedFile, PullRequest

MAX_UNTRACKED_BYTES = 512 * 1024
MAX_COMMITS_IN_BODY = 30


class LocalError(Exception):
    """git failed or the checkout has no usable base branch."""


@dataclass
class LocalBranch(PullRequest):
    path: str = ""  # the checkout (main working tree or a linked worktree)
    merge_base: str = ""  # the commit the diff is taken against
    ahead: int = 0  # commits on the branch since merge_base
    dirty: bool = False  # uncommitted or untracked changes

    @property
    def key(self) -> str:
        return f"{self.repo}@{self.head_ref}"

    @property
    def is_local(self) -> bool:
        return True


def _git(cwd: str | Path, *args: str, check: bool = True) -> str:
    try:
        # Not every file is UTF-8; a stray Latin-1 line must not sink the whole diff.
        proc = subprocess.run(["git", "-C", str(cwd), "-c", "core.quotePath=false", *args],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LocalError(f"git {args[0]} failed: {exc}") from exc
    if check and proc.returncode != 0:
        raise LocalError(f"git {args[0]} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout if proc.returncode == 0 else ""


def _worktrees(repo_path: Path) -> list[tuple[str, str]]:
    """(path, branch) for every live working tree of a repository; branch is "" when detached."""
    out: list[tuple[str, str]] = []
    path = branch = ""
    skip = False
    scratch = CACHE_DIR.resolve()  # git reports resolved paths; ~/.cache may be a symlink
    for line in _git(repo_path, "worktree", "list", "--porcelain").splitlines() + [""]:
        if line.startswith("worktree "):
            path, branch, skip = line[9:], "", False
        elif line.startswith("branch "):
            branch = line[7:].removeprefix("refs/heads/")
        elif line in ("bare", "prunable") or line.startswith("prunable "):
            skip = True
        elif not line and path:
            if not skip and os.path.isdir(path) and not Path(path).resolve().is_relative_to(scratch):
                out.append((path, branch))
            path = ""
    return out


def _status(path: str) -> list[str]:
    """`git status` entries as "XY path"; a rename's old path is dropped."""
    entries: list[str] = []
    tokens = iter(_git(path, "status", "--porcelain=v1", "-z").split("\0"))
    for token in tokens:
        if token:
            entries.append(token)
            if token[0] in "RC":
                next(tokens, None)
    return entries


def default_branch(path: str | Path) -> str:
    """The branch pull requests target: origin/HEAD, else the first of the usual names that exists."""
    head = _git(path, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False).strip()
    if head:
        return head
    for name in ("origin/main", "origin/master", "main", "master"):
        if _git(path, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}", check=False).strip():
            return name
    raise LocalError(f"no default branch found in {path}")


def inspect(repo: str, path: str, branch: str, author: str = "", avatar: str = "",
            since: datetime | None = None) -> LocalBranch | None:
    """Describe one checkout, or None when it has nothing beyond its default branch, or
    nothing touched since `since`."""
    base_ref = default_branch(path)
    head_sha = _git(path, "rev-parse", "HEAD").strip()
    merge_base = _git(path, "merge-base", "HEAD", base_ref, check=False).strip()
    if not merge_base:
        return None  # unrelated histories
    status = _status(path)
    if not branch and not status:
        return None  # a detached checkout with nothing uncommitted: a look at someone's commit, not your work
    stamps = [int(t) for t in _git(path, "log", "--format=%ct", f"{merge_base}..HEAD").split()]
    if not stamps and not status:
        return None

    # Last activity: the newest commit on the branch, or the most recently saved uncommitted file.
    times = [float(t) for t in stamps]
    for line in status:
        try:
            times.append(os.stat(os.path.join(path, line[3:].rstrip("/"))).st_mtime)
        except OSError:
            pass
    if not times:
        times.append(float(_git(path, "log", "-1", "--format=%ct").strip() or 0))
    newest = datetime.fromtimestamp(max(times), timezone.utc)
    if since is not None and newest < since:
        return None
    oldest = datetime.fromtimestamp(min(stamps), timezone.utc) if stamps else newest
    ahead = len(stamps)

    additions = deletions = 0
    numstat = _git(path, "diff", "--numstat", merge_base).splitlines()
    for line in numstat:
        added, deleted, _name = line.split("\t", 2)
        additions += int(added) if added.isdigit() else 0
        deletions += int(deleted) if deleted.isdigit() else 0
    untracked = [n for n in _git(path, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if n]

    subjects = _git(path, "log", f"--max-count={MAX_COMMITS_IN_BODY}", "--format=%s", f"{merge_base}..HEAD").splitlines()
    title = subjects[0] if subjects else "Uncommitted changes"
    body = "\n".join(f"• {s}" for s in subjects) or "No commits yet."
    if ahead > len(subjects):
        body += f"\n… and {ahead - len(subjects)} more"
    if status:
        body += f"\n\nUncommitted changes in {len(status)} file{'s' if len(status) != 1 else ''}."

    name = branch or head_sha[:9]
    upstream = _git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False).strip()
    url = (f"https://github.com/{repo}/compare/{base_ref.removeprefix('origin/')}...{name}"
           if upstream and branch else "")
    return LocalBranch(
        repo=repo, number=0, title=title, url=url, author=author or "you", author_avatar=avatar,
        is_draft=False, created_at=oldest, updated_at=newest, additions=additions, deletions=deletions,
        changed_files=len(numstat) + len(untracked), review_decision=None, mergeable="", head_ref=name,
        base_ref=base_ref, body=body, head_sha=head_sha, path=path, merge_base=merge_base, ahead=ahead,
        dirty=bool(status))


def list_branches(repos: dict[str, Path], author: str = "", avatar: str = "", days: int = 0) -> list[LocalBranch]:
    """Every checkout of the given repositories with work that is not on its default branch yet,
    touched in the last `days` days (0 for no limit)."""
    since = datetime.now(timezone.utc) - timedelta(days=days) if days > 0 else None
    jobs: list[tuple[str, str, str]] = []
    for repo, repo_path in repos.items():
        try:
            jobs += [(repo, path, branch) for path, branch in _worktrees(repo_path)]
        except LocalError:
            continue

    def one(job: tuple[str, str, str]) -> LocalBranch | None:
        try:
            return inspect(*job, author=author, avatar=avatar, since=since)
        except (LocalError, ValueError):
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        found = [b for b in pool.map(one, jobs) if b is not None]
    found.sort(key=lambda b: b.updated_at, reverse=True)
    return found


# -- diff ---------------------------------------------------------------------

def parse_diff(text: str) -> list[ChangedFile]:
    """Split `git diff` output into per-file patches shaped like the GitHub API's."""
    files: list[ChangedFile] = []
    cur: ChangedFile | None = None
    lines: list[str] = []
    in_hunks = False
    binary = False

    def finish() -> None:
        if cur is not None:
            cur.patch = None if binary else "\n".join(lines)
            files.append(cur)

    for line in text.split("\n"):
        if line.startswith("diff --git "):
            finish()
            cur, lines, in_hunks, binary = ChangedFile(_name_from_header(line[11:]), "modified", 0, 0, ""), [], False, False
        elif cur is None:
            continue
        elif in_hunks:
            if line.startswith("+"):
                cur.additions += 1
            elif line.startswith("-"):
                cur.deletions += 1
            elif not line.startswith((" ", "\\", "@@")):
                continue  # the trailing empty line
            lines.append(line)
        elif line.startswith("@@"):
            in_hunks = True
            lines.append(line)
        elif line.startswith("new file mode"):
            cur.status = "added"
        elif line.startswith("deleted file mode"):
            cur.status = "removed"
        elif line.startswith(("rename from ", "copy from ")):
            cur.status = "renamed" if line.startswith("rename") else "copied"
            cur.previous_filename = line.split(" from ", 1)[1]
        elif line.startswith(("rename to ", "copy to ")):
            cur.filename = line.split(" to ", 1)[1]
        elif line.startswith("+++ b/"):
            cur.filename = line[6:].rstrip("\t")
        elif line.startswith("index "):
            cur.sha = line[6:].split(" ")[0].partition("..")[2]  # the new blob, for binary files
        elif line.startswith("Binary files "):
            binary = True
    finish()
    return files


def _name_from_header(rest: str) -> str:
    """The path in "a/<p> b/<p>" (both sides equal unless renamed, which other lines cover)."""
    half = (len(rest) - 5) // 2
    name = rest[2:2 + half]
    if rest == f"a/{name} b/{name}":
        return name
    return rest.rpartition(" b/")[2] or rest


def _untracked(path: str) -> list[ChangedFile]:
    files: list[ChangedFile] = []
    for name in _git(path, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if not name:
            continue
        patch: str | None = None
        additions = 0
        try:
            full = os.path.join(path, name)
            if os.path.getsize(full) <= MAX_UNTRACKED_BYTES:
                with open(full, "rb") as fh:
                    data = fh.read()
                if b"\0" not in data:
                    body = data.decode("utf-8", errors="replace").split("\n")
                    if body and body[-1] == "":
                        body.pop()
                    additions = len(body)
                    patch = f"@@ -0,0 +1,{additions} @@\n" + "\n".join("+" + line for line in body) if body else ""
        except OSError:
            patch = None
        try:
            stat = os.stat(os.path.join(path, name))
            sha = f"{stat.st_size}:{stat.st_mtime_ns}"
        except OSError:
            sha = ""
        files.append(ChangedFile(name, "added", additions, 0, patch, sha=sha))
    return files


def list_files(branch: LocalBranch) -> list[ChangedFile]:
    """Committed, uncommitted and untracked changes since the merge-base, sorted by path."""
    diff = _git(branch.path, "diff", "--no-color", "--no-ext-diff", "--find-renames",
                "--src-prefix=a/", "--dst-prefix=b/", branch.merge_base)
    files = parse_diff(diff) + _untracked(branch.path)
    files.sort(key=lambda f: f.filename)
    return files
