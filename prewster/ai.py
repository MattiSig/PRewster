"""Headless Claude Code: repo lookup, PR worktrees, skill discovery, streaming runs."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from gi.repository import GObject

from .config import CACHE_DIR, CONFIG_FILE, AIAction, AIConfig
from .github import PullRequest
from .poller import on_main

WORKTREES = CACHE_DIR / "worktrees"
CLONES = CACHE_DIR / "repos"
RUNS = CACHE_DIR / "ai"
GITHUB_URL = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?\s*$")
UUID_LIKE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-")


class AIError(Exception):
    """Anything that stops a run before Claude produced a result."""


@dataclass
class Skill:
    name: str
    description: str
    scope: str  # builtin, custom, user, project
    prompt: str  # template with {number} etc.


BUILTIN = [
    Skill("Code review", "Correctness bugs in the diff, verified against the codebase", "builtin",
          "/code-review {number}"),
    Skill("Security review", "Security issues introduced by this branch", "builtin",
          "/security-review"),
    Skill("Summarize for review", "What changed, why, risks, and where to look first", "builtin",
          "You are helping a reviewer. Summarize pull request #{number} \"{title}\" in {repo} "
          "(branch {head} into {base}). Use `gh pr view {number}` and `gh pr diff {number}`, and read "
          "the surrounding code where it matters. Report: what it changes and why, risks or things that "
          "look wrong, and the files to read first. Be concrete and brief; use markdown."),
]


# Where the Claude Code installers put the binary. Desktop sessions (autostart,
# app launchers) often start without ~/.local/bin on PATH.
CLAUDE_DIRS = ("~/.local/bin", "~/.claude/local", "~/.npm-global/bin", "~/.bun/bin")
CLAUDE_MISSING = ("Claude Code CLI not found on PATH. Set claude = \"/path/to/claude\" under [ai] in "
                  f"{str(CONFIG_FILE).replace(str(Path.home()), '~', 1)} and restart PRewster.")


def claude_binary(cfg: AIConfig) -> str | None:
    name = os.path.expanduser(cfg.claude)
    found = shutil.which(name)
    if found or os.sep in name:
        return found
    return shutil.which(name, path=os.pathsep.join(os.path.expanduser(d) for d in CLAUDE_DIRS))


def render_prompt(template: str, pr: PullRequest) -> str:
    return template.format(number=pr.number, repo=pr.repo, title=pr.title, url=pr.url,
                           base=pr.base_ref, head=pr.head_ref, author=pr.author)


# -- git helpers ------------------------------------------------------------

def git(repo: Path, *args: str, timeout: int = 300) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AIError(f"git {' '.join(args[:2])} failed: {exc}") from exc
    if proc.returncode != 0:
        raise AIError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def _origin_of(git_dir: Path) -> str | None:
    """Parse origin URL from .git/config without spawning git."""
    try:
        if git_dir.is_file():  # worktree pointer
            return None
        text = (git_dir / "config").read_text()
    except OSError:
        return None
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif section == '[remote "origin"]' and line.startswith("url"):
            _, _, url = line.partition("=")
            m = GITHUB_URL.search(url.strip())
            return f"{m.group(1)}/{m.group(2)}".lower() if m else None
    return None


class RepoIndex:
    """Maps owner/repo to a local checkout."""

    def __init__(self, cfg: AIConfig):
        self.cfg = cfg
        self._found: dict[str, Path] = {}
        self._lock = threading.Lock()
        self._scanned = False

    def scan(self) -> None:
        found: dict[str, Path] = {}
        skip = {"node_modules", ".cache", ".local", ".git", "worktrees"}
        for root in self.cfg.search_roots:
            base = Path(os.path.expanduser(root))
            if not base.is_dir():
                continue
            for dirpath, dirnames, _files in os.walk(base):
                depth = len(Path(dirpath).relative_to(base).parts)
                if ".git" in dirnames or (Path(dirpath) / ".git").is_file():
                    key = _origin_of(Path(dirpath) / ".git")
                    if key and key not in found:
                        found[key] = Path(dirpath)
                    dirnames[:] = []
                    continue
                if depth >= 3:
                    dirnames[:] = []
                    continue
                dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        with self._lock:
            self._found = found
            self._scanned = True

    def lookup(self, repo: str) -> Path | None:
        for key, path in self.cfg.repos.items():
            if key.lower() == repo.lower():
                p = Path(os.path.expanduser(path))
                return p if p.is_dir() else None
        with self._lock:
            if not self._scanned:
                pass
            hit = self._found.get(repo.lower())
        if hit is None and not self._scanned:
            self.scan()
            with self._lock:
                hit = self._found.get(repo.lower())
        clone = CLONES / repo.replace("/", "__")
        if hit is None and (clone / ".git").exists():
            return clone
        return hit


def ensure_repo(index: RepoIndex, pr: PullRequest, log) -> Path:
    path = index.lookup(pr.repo)
    if path:
        return path
    target = CLONES / pr.repo.replace("/", "__")
    log(f"No local checkout of {pr.repo}; cloning into {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    gh = shutil.which("gh")
    if not gh:
        raise AIError("gh is required to clone the repository")
    try:
        proc = subprocess.run([gh, "repo", "clone", pr.repo, str(target), "--", "--filter=blob:none"],
                              capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AIError(f"clone failed: {exc}") from exc
    if proc.returncode != 0:
        raise AIError(f"clone failed: {proc.stderr.strip()[-400:]}")
    return target


def prepare_worktree(repo_path: Path, pr: PullRequest, log) -> Path:
    ref = f"refs/prewster/{pr.number}"
    log(f"Fetching PR #{pr.number} head into {repo_path}")
    git(repo_path, "fetch", "--no-tags", "--quiet", "origin", f"+refs/pull/{pr.number}/head:{ref}")
    wt = WORKTREES / pr.repo.replace("/", "__") / str(pr.number)
    git(repo_path, "worktree", "prune")
    if (wt / ".git").exists():
        git(wt, "reset", "--hard", "--quiet", ref)
        git(wt, "clean", "-fdq")
    else:
        wt.parent.mkdir(parents=True, exist_ok=True)
        git(repo_path, "worktree", "add", "--detach", "--quiet", str(wt), ref)
    log(f"Worktree ready: {wt}")
    return wt


def cleanup_worktrees() -> int:
    """Remove every scratch worktree and the refs/prewster/* refs. Returns count removed."""
    removed = 0
    if not WORKTREES.exists():
        return 0
    for wt in sorted(p for p in WORKTREES.glob("*/*") if p.is_dir()):
        main: Path | None = None
        try:
            common = git(wt, "rev-parse", "--git-common-dir")
            main = Path(common).resolve().parent if common else None
        except AIError:
            pass
        try:
            if main:
                git(main, "worktree", "remove", "--force", str(wt))
            else:
                shutil.rmtree(wt, ignore_errors=True)
            removed += 1
        except AIError:
            shutil.rmtree(wt, ignore_errors=True)
            removed += 1
        if main:
            try:
                git(main, "worktree", "prune")
                for line in git(main, "for-each-ref", "--format=%(refname)", "refs/prewster/").splitlines():
                    git(main, "update-ref", "-d", line.strip())
            except AIError:
                pass
    shutil.rmtree(WORKTREES, ignore_errors=True)
    return removed


# -- skills -----------------------------------------------------------------

def _frontmatter(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    out: dict[str, str] = {}
    for line in text.split("\n", 1)[1].split("\n"):
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key.startswith(" "):
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _skills_in(base: Path, scope: str) -> list[Skill]:
    skills: list[Skill] = []
    for md in sorted((base / "skills").glob("*/SKILL.md")):
        name = md.parent.name
        if UUID_LIKE.match(name):
            continue
        fm = _frontmatter(md)
        skills.append(Skill(fm.get("name") or name, fm.get("description", "")[:140], scope, f"/{name} {{number}}"))
    for md in sorted((base / "commands").glob("*.md")):
        fm = _frontmatter(md)
        skills.append(Skill(md.stem, fm.get("description", "")[:140], scope, f"/{md.stem} {{number}}"))
    return skills


def list_skills(cfg: AIConfig, repo_path: Path | None) -> list[Skill]:
    skills = list(BUILTIN)
    skills += [Skill(a.name, a.description, "custom", a.prompt) for a in cfg.actions]
    skills += _skills_in(Path.home() / ".claude", "user")
    if repo_path:
        skills += _skills_in(repo_path / ".claude", "project")
    return skills


# -- running ----------------------------------------------------------------

def describe_tool(name: str, inp: dict) -> str:
    inp = inp or {}
    if name == "Read":
        return f"Read {inp.get('file_path', '')}"
    if name == "Grep":
        return f"Grep {inp.get('pattern', '')!r} {inp.get('path', '')}".rstrip()
    if name == "Glob":
        return f"Glob {inp.get('pattern', '')}"
    if name == "Bash":
        return f"$ {str(inp.get('command', ''))[:160]}"
    if name == "Agent":
        return f"Agent: {inp.get('description') or inp.get('prompt', '')[:80]}"
    if name == "Skill":
        return f"/{inp.get('skill', '')} {inp.get('args', '')}".rstrip()
    if name in ("Edit", "Write", "MultiEdit"):
        return f"{name} {inp.get('file_path', '')}"
    return name


class ClaudeRun(GObject.Object):
    __gsignals__ = {
        "text": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "activity": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "finished": (GObject.SignalFlags.RUN_FIRST, None, (bool, str)),
    }

    def __init__(self, cfg: AIConfig, index: RepoIndex, pr: PullRequest, skill: Skill, prompt: str):
        super().__init__()
        self.cfg = cfg
        self.index = index
        self.pr = pr
        self.skill = skill
        self.prompt = prompt
        self.transcript: list[tuple[str, str]] = []  # (kind, text) where kind is text/activity
        self.state = "pending"  # pending, running, done, failed, cancelled
        self.summary = ""
        self.final_text = ""  # Claude's last message, the part worth posting
        self._block_has_text = False
        self.started = time.time()
        self.proc: subprocess.Popen | None = None
        self._cancel = False

    @property
    def output_text(self) -> str:
        return "".join(t for k, t in self.transcript if k == "text").strip()

    @property
    def result_text(self) -> str:
        return (self.final_text or self.output_text).strip()

    def start(self) -> None:
        self.state = "running"
        threading.Thread(target=self._work, daemon=True).start()

    def cancel(self) -> None:
        self._cancel = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    # -- main-thread emitters ---------------------------------------------

    def _text(self, text: str) -> None:
        self.transcript.append(("text", text))
        self.emit("text", text)

    def _activity(self, text: str) -> None:
        self.transcript.append(("activity", text))
        self.emit("activity", text)

    def _finish(self, ok: bool, summary: str) -> None:
        self.state = "done" if ok else ("cancelled" if self._cancel else "failed")
        self.summary = summary
        self._save()
        self.emit("finished", ok, summary)

    # -- worker -----------------------------------------------------------

    def _work(self) -> None:
        log = lambda msg: on_main(self._activity, msg)  # noqa: E731
        try:
            binary = claude_binary(self.cfg)
            if not binary:
                raise AIError(CLAUDE_MISSING)
            repo = ensure_repo(self.index, self.pr, log)
            if self._cancel:
                raise AIError("Cancelled")
            wt = prepare_worktree(repo, self.pr, log)
            if self._cancel:
                raise AIError("Cancelled")
            cmd = [binary, "-p", self.prompt, "--output-format", "stream-json", "--verbose",
                   "--include-partial-messages", "--no-session-persistence",
                   # Only the reviewer's own settings: the checkout is the PR author's and
                   # could otherwise contribute .claude/settings.json hooks and permissions.
                   "--setting-sources", "user",
                   "--permission-mode", self.cfg.permission_mode, "--max-turns", str(self.cfg.max_turns)]
            if not self.cfg.mcp:
                cmd.append("--strict-mcp-config")
            if self.cfg.model:
                cmd += ["--model", self.cfg.model]
            if self.cfg.allowed_tools:
                cmd += ["--allowedTools", *self.cfg.allowed_tools]
            env = os.environ.copy()
            env.pop("CLAUDECODE", None)
            log(f"claude -p {self.prompt[:80]!r} in {wt}")
            self.proc = subprocess.Popen(cmd, cwd=wt, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         stdin=subprocess.DEVNULL, text=True, env=env)
            stderr_chunks: list[str] = []
            threading.Thread(target=lambda: stderr_chunks.append(self.proc.stderr.read()), daemon=True).start()
            result = self._consume(self.proc.stdout)
            rc = self.proc.wait()
            if self._cancel:
                on_main(self._finish, False, "Cancelled")
                return
            if result is None:
                err = "".join(stderr_chunks).strip()[-600:]
                raise AIError(f"claude exited with code {rc}" + (f": {err}" if err else ""))
            ok = not result.get("is_error")
            elapsed = result.get("duration_ms", (time.time() - self.started) * 1000) / 1000
            parts = [f"{'Done' if ok else 'Failed'} in {elapsed:.0f}s"]
            if result.get("num_turns"):
                parts.append(f"{result['num_turns']} turns")
            if result.get("total_cost_usd") is not None:
                parts.append(f"${result['total_cost_usd']:.2f}")
            if not ok and result.get("result") and not self.output_text:
                on_main(self._text, str(result["result"]))
            on_main(self._finish, ok, " · ".join(parts))
        except AIError as exc:
            on_main(self._finish, False, str(exc))
        except Exception as exc:  # noqa: BLE001
            on_main(self._finish, False, f"Unexpected error: {exc!r}")

    def _consume(self, stream) -> dict | None:
        result: dict | None = None
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("type")
            nested = bool(ev.get("parent_tool_use_id"))
            if kind == "stream_event" and not nested:
                inner = ev.get("event") or {}
                if inner.get("type") == "content_block_delta":
                    delta = inner.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        if not self._block_has_text and self._streamed_any():
                            on_main(self._text, "\n\n")
                        self._block_has_text = True
                        on_main(self._text, delta["text"])
                elif inner.get("type") == "content_block_stop":
                    self._block_has_text = False
            elif kind == "assistant":
                for block in (ev.get("message") or {}).get("content") or []:
                    if block.get("type") == "tool_use":
                        prefix = "    " if nested else ""
                        on_main(self._activity, prefix + describe_tool(block.get("name", "?"), block.get("input") or {}))
                    elif block.get("type") == "text" and nested is False and not self._streamed_any():
                        on_main(self._text, block.get("text", ""))
            elif kind == "system" and ev.get("subtype") == "init":
                on_main(self._activity, f"Claude {ev.get('model', '')} started")
            elif kind == "result":
                result = ev
                if isinstance(ev.get("result"), str):
                    self.final_text = ev["result"]
        return result

    def _streamed_any(self) -> bool:
        return any(k == "text" for k, _ in self.transcript)

    # -- persistence ------------------------------------------------------

    def _save(self) -> None:
        try:
            folder = RUNS / self.pr.repo.replace("/", "__")
            folder.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^a-z0-9]+", "-", self.skill.name.lower()).strip("-")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            body = self.result_text
            header = (f"# {self.skill.name} on {self.pr.key}\n\n"
                      f"- when: {datetime.now().isoformat(timespec='seconds')}\n"
                      f"- state: {self.state}\n- summary: {self.summary}\n- prompt: {self.prompt!r}\n\n---\n\n")
            (folder / f"{self.pr.number}-{stamp}-{slug}.md").write_text(header + body + "\n")
        except OSError:
            pass


def load_last_run(pr: PullRequest) -> tuple[str, str, str] | None:
    """Return (title, when, body) of the newest saved run for this PR."""
    folder = RUNS / pr.repo.replace("/", "__")
    files = sorted(folder.glob(f"{pr.number}-*.md")) if folder.exists() else []
    if not files:
        return None
    try:
        text = files[-1].read_text()
    except OSError:
        return None
    head, _, body = text.partition("\n---\n\n")
    title = head.splitlines()[0].lstrip("# ").strip() if head else files[-1].stem
    when = state = ""
    for line in head.splitlines():
        if line.startswith("- when:"):
            when = line.partition(":")[2].strip()
        elif line.startswith("- state:"):
            state = line.partition(":")[2].strip()
    if state and state != "done":
        title = f"{title} ({state})"
    return title, when, body.strip()


class AIService:
    """Holds config, the repo index and all live runs."""

    def __init__(self, cfg: AIConfig):
        self.cfg = cfg
        self.index = RepoIndex(cfg)
        self.runs: dict[str, ClaudeRun] = {}
        threading.Thread(target=self.index.scan, daemon=True).start()

    @property
    def available(self) -> bool:
        return claude_binary(self.cfg) is not None

    def skills_for(self, pr: PullRequest) -> list[Skill]:
        return list_skills(self.cfg, self.index.lookup(pr.repo))

    def run(self, pr: PullRequest, skill: Skill, prompt: str | None = None) -> ClaudeRun:
        run = ClaudeRun(self.cfg, self.index, pr, skill, prompt or render_prompt(skill.prompt, pr))
        self.runs[pr.key] = run
        run.start()
        return run

    def active(self, pr: PullRequest) -> ClaudeRun | None:
        return self.runs.get(pr.key)
