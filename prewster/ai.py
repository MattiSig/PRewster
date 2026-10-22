"""Headless Claude Code: repo lookup, PR worktrees, skill discovery, streaming chats."""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
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
    local_prompt: str = ""  # used instead on a local branch, which has no PR number


BUILTIN = [
    Skill("Code review", "Correctness bugs in the diff, verified against the codebase", "builtin",
          "/code-review {number}", "/code-review"),
    Skill("Security review", "Security issues introduced by this branch", "builtin",
          "/security-review", "/security-review"),
    Skill("Summarize for review", "What changed, why, risks, and where to look first", "builtin",
          "You are helping a reviewer. Summarize pull request #{number} \"{title}\" in {repo} "
          "(branch {head} into {base}). Use `gh pr view {number}` and `gh pr diff {number}`, and read "
          "the surrounding code where it matters. Report: what it changes and why, risks or things that "
          "look wrong, and the files to read first. Be concrete and brief; use markdown.",
          "You are helping me check my own work before I open a pull request. Summarize the changes on "
          "branch {head} in {repo} compared with {base}: `git log {base}..HEAD` lists the commits, "
          "`git diff {base}...HEAD` shows them, `git diff HEAD` shows uncommitted edits and `git status` "
          "untracked files. Read the surrounding code where it matters. Report: what it changes and why, "
          "anything that looks wrong or unfinished, and what a reviewer is likely to ask. Be concrete and "
          "brief; use markdown."),
]
# Claude only ever loads your own settings: a checkout, even a local one, may hold someone
# else's code (a PR you checked out), whose .claude/settings.json could add hooks and permissions.
SETTING_SOURCES = "user"
# Added to allowed_tools for a turn when the chat allows edits.
EDIT_TOOLS = ["Edit", "Write", "MultiEdit", "NotebookEdit"]
# Tried in order when [ai] terminal is not set: (binary, arguments before the command).
TERMINALS = [("xdg-terminal-exec", []), ("ghostty", ["-e"]), ("kitty", []), ("alacritty", ["-e"]),
             ("foot", []), ("wezterm", ["start", "--"]), ("gnome-terminal", ["--"]), ("konsole", ["-e"]),
             ("xterm", ["-e"])]


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


def render_prompt(skill: Skill, pr: PullRequest) -> str:
    template = skill.local_prompt if pr.is_local and skill.local_prompt else skill.prompt
    text = template.format(number="" if pr.is_local else pr.number, repo=pr.repo, title=pr.title, url=pr.url,
                           base=pr.base_ref, head=pr.head_ref, author=pr.author)
    return re.sub(r"[ \t]+", " ", text).strip() if pr.is_local else text


def terminal_command(cfg: AIConfig) -> list[str] | None:
    if cfg.terminal.strip():
        return shlex.split(cfg.terminal)
    env = os.environ.get("TERMINAL", "").strip()
    if env and shutil.which(env):
        return [env, "-e"]
    for name, args in TERMINALS:
        if shutil.which(name):
            return [name, *args]
    return None


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
    """owner/repo from the origin URL in .git/config, read without spawning git."""
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
            return f"{m.group(1)}/{m.group(2)}" if m else None
    return None


class RepoIndex:
    """Maps owner/repo to a local checkout."""

    def __init__(self, cfg: AIConfig):
        self.cfg = cfg
        self._found: dict[str, Path] = {}  # lowercased owner/repo -> checkout
        self._names: dict[str, str] = {}  # lowercased -> as spelled in the origin URL
        self._lock = threading.Lock()
        self._scanned = False

    def scan(self) -> None:
        found: dict[str, Path] = {}
        names: dict[str, str] = {}
        skip = {"node_modules", ".cache", ".local", ".git", "worktrees"}
        for root in self.cfg.search_roots:
            base = Path(os.path.expanduser(root))
            if not base.is_dir():
                continue
            for dirpath, dirnames, _files in os.walk(base):
                depth = len(Path(dirpath).relative_to(base).parts)
                if ".git" in dirnames or (Path(dirpath) / ".git").is_file():
                    name = _origin_of(Path(dirpath) / ".git")
                    if name and name.lower() not in found:
                        found[name.lower()] = Path(dirpath)
                        names[name.lower()] = name
                    dirnames[:] = []
                    continue
                if depth >= 3:
                    dirnames[:] = []
                    continue
                dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        with self._lock:
            self._found = found
            self._names = names
            self._scanned = True

    def all(self) -> dict[str, Path]:
        """Every known checkout, explicit [ai.repos] entries first."""
        with self._lock:
            found = {self._names.get(k, k): v for k, v in self._found.items()}
        for key, path in self.cfg.repos.items():
            p = Path(os.path.expanduser(path))
            if p.is_dir():
                found = {k: v for k, v in found.items() if k.lower() != key.lower()}
                found[key] = p
        return found

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
    """A Claude Code conversation about one PR or branch: the first prompt, then follow-ups.

    Each turn is its own `claude -p` process; follow-ups resume the session by id, in the
    same checkout, so Claude remembers what it read and said.
    """

    __gsignals__ = {
        "text": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "activity": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # A follow-up was sent and its turn is running.
        "user": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # A turn ended: (ok, summary)
        "finished": (GObject.SignalFlags.RUN_FIRST, None, (bool, str)),
    }

    def __init__(self, cfg: AIConfig, index: RepoIndex, pr: PullRequest, skill: Skill, prompt: str):
        super().__init__()
        self.cfg = cfg
        self.index = index
        self.pr = pr
        self.skill = skill
        self.prompt = prompt
        self.transcript: list[tuple[str, str]] = []  # (kind, text) where kind is text/activity/user
        self.state = "pending"  # pending, running, done, failed, cancelled
        self.summary = ""
        self.final_text = ""  # Claude's last message of the latest turn, the part worth posting
        self.session_id: str | None = None
        self.session_started = False  # Claude has created the session, so it can be resumed
        self.cwd: str | None = pr.path if pr.is_local else None
        self.saved_path: Path | None = None
        self._turn_start = 0  # index in transcript where the latest turn's output begins
        self._block_has_text = False
        self.started = time.time()
        self.proc: subprocess.Popen | None = None
        self._cancel = False

    @property
    def output_text(self) -> str:
        return "".join(t for k, t in self.transcript[self._turn_start:] if k == "text").strip()

    @property
    def result_text(self) -> str:
        return (self.final_text or self.output_text).strip()

    @property
    def can_follow_up(self) -> bool:
        return self.session_started and self.state != "running"

    def start(self, allow_edits: bool = False, echo: bool = False) -> None:
        """Run the first prompt. echo shows it in the transcript (typed chats, not skills)."""
        if echo:
            self.transcript.append(("user", self.prompt))
        self._begin(self.prompt, allow_edits)

    def send(self, message: str, allow_edits: bool = False) -> None:
        """Ask a follow-up in the same session."""
        if self.state == "running":
            return
        self.transcript.append(("user", message))
        self.emit("user", message)
        self._begin(message, allow_edits)

    def _begin(self, prompt: str, allow_edits: bool) -> None:
        self.state = "running"
        self._cancel = False
        self.final_text = ""
        self._block_has_text = False
        self._turn_start = len(self.transcript)
        self.started = time.time()
        threading.Thread(target=self._work, args=(prompt, allow_edits), daemon=True).start()

    def cancel(self) -> None:
        self._cancel = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def open_in_terminal(self) -> str:
        """Continue the conversation in an interactive Claude Code session. Returns an error, or ""."""
        binary = claude_binary(self.cfg)
        if not binary:
            return CLAUDE_MISSING
        if not self.session_started or not self.cwd or not os.path.isdir(self.cwd):
            return "Nothing to continue yet: start a chat first"
        term = terminal_command(self.cfg)
        if term is None:
            return f'No terminal found. Set terminal = "your-terminal -e" under [ai] in {CONFIG_FILE}'
        # cd inside the terminal: not every terminal starts in its parent's directory.
        cmd = term + ["sh", "-c", 'cd "$1" && shift && exec "$@"', "sh", self.cwd,
                      binary, "--resume", self.session_id, "--setting-sources", SETTING_SOURCES]
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        try:
            subprocess.Popen(cmd, env=env, start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            return f"Could not start {term[0]}: {exc}"
        return ""

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

    def _workdir(self, log, fresh: bool) -> str:
        """The checkout Claude runs in: the local branch itself, or a scratch worktree of the PR.

        A new conversation resets the worktree to the PR head; follow-ups keep it as the
        last turn left it, unless it was cleaned up since.
        """
        if self.pr.is_local:
            return self.pr.path
        if not fresh and self.cwd and os.path.isdir(self.cwd):
            return self.cwd
        repo = ensure_repo(self.index, self.pr, log)
        if self._cancel:
            raise AIError("Cancelled")
        self.cwd = str(prepare_worktree(repo, self.pr, log))
        return self.cwd

    def _work(self, prompt: str, allow_edits: bool) -> None:
        log = lambda msg: on_main(self._activity, msg)  # noqa: E731
        try:
            binary = claude_binary(self.cfg)
            if not binary:
                raise AIError(CLAUDE_MISSING)
            resume = self.session_started
            cwd = self._workdir(log, fresh=not resume)
            if self._cancel:
                raise AIError("Cancelled")
            mode, tools = self.cfg.permission_mode, list(self.cfg.allowed_tools)
            if allow_edits:
                tools += [t for t in EDIT_TOOLS if t not in tools]
                if mode in ("dontAsk", "default", "plan"):
                    mode = "acceptEdits"
            if self.session_id is None:
                self.session_id = str(uuid.uuid4())
            cmd = [binary, "-p", "--output-format", "stream-json", "--verbose",
                   "--include-partial-messages", "--setting-sources", SETTING_SOURCES,
                   "--permission-mode", mode, "--max-turns", str(self.cfg.max_turns),
                   "--resume" if resume else "--session-id", self.session_id]
            if not self.cfg.mcp:
                cmd.append("--strict-mcp-config")
            if self.cfg.model:
                cmd += ["--model", self.cfg.model]
            if tools:
                cmd += ["--allowedTools", *tools]
            cmd += ["--", prompt]  # a message may start with "-"; this also ends --allowedTools
            env = os.environ.copy()
            env.pop("CLAUDECODE", None)
            edits = ", edits allowed" if allow_edits else ""
            log(f"claude -p {prompt[:80]!r} in {cwd}{edits}" if not resume else f"Follow-up in {cwd}{edits}")
            self.proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
                self.session_id = ev.get("session_id") or self.session_id
                self.session_started = True
                on_main(self._activity, f"Claude {ev.get('model', '')} started")
            elif kind == "result":
                result = ev
                if isinstance(ev.get("result"), str):
                    self.final_text = ev["result"]
        return result

    def _streamed_any(self) -> bool:
        """Text arrived earlier in this turn (as opposed to the conversation)."""
        return any(k == "text" for k, _ in self.transcript[self._turn_start:])

    # -- persistence ------------------------------------------------------

    def conversation_text(self) -> str:
        parts: list[str] = []
        for kind, text in self.transcript:
            if kind == "text":
                parts.append(text)
            elif kind == "user":
                text = " ".join(text.split())  # one line, so parse_conversation can find where it ends
                parts.append(f"\n\n---\n\n**You:** {text}\n\n" if parts else f"**You:** {text}\n\n")
        return "".join(parts).strip()

    def _save(self) -> None:
        """Write the whole conversation, one file per conversation, rewritten after each turn."""
        try:
            if self.saved_path is None:
                folder = RUNS / self.pr.repo.replace("/", "__")
                folder.mkdir(parents=True, exist_ok=True)
                slug = re.sub(r"[^a-z0-9]+", "-", self.skill.name.lower()).strip("-")
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                self.saved_path = folder / f"{_run_prefix(self.pr)}{stamp}-{slug}.md"
            header = (f"# {self.skill.name} on {self.pr.key}\n\n"
                      f"- when: {datetime.now().isoformat(timespec='seconds')}\n"
                      f"- state: {self.state}\n- summary: {self.summary}\n- prompt: {self.prompt!r}\n"
                      f"- session: {self.session_id if self.session_started else ''}\n- cwd: {self.cwd or ''}\n\n---\n\n")
            self.saved_path.write_text(header + self.conversation_text() + "\n")
        except OSError:
            pass


def _run_prefix(pr: PullRequest) -> str:
    """Saved conversation file names start with this: the PR number, or the local branch."""
    if pr.is_local:
        return "local-" + re.sub(r"[^A-Za-z0-9._-]+", "-", pr.head_ref) + "@"
    return f"{pr.number}-"


@dataclass
class SavedRun:
    title: str
    when: str
    state: str
    body: str
    prompt: str
    session: str
    cwd: str
    path: Path


def parse_conversation(body: str) -> list[tuple[str, str]]:
    """Turn a saved conversation (ClaudeRun.conversation_text) back into transcript entries."""
    entries: list[tuple[str, str]] = []
    for i, part in enumerate(body.split("\n\n---\n\n**You:** ")):
        if i == 0 and not part.startswith("**You:** "):
            if part:
                entries.append(("text", part))
            continue
        message, _, answer = part.removeprefix("**You:** ").partition("\n\n")
        entries.append(("user", message))
        if answer:
            entries.append(("text", answer))
    return entries


def load_last_run(pr: PullRequest) -> SavedRun | None:
    """The newest saved conversation for this PR or branch."""
    folder = RUNS / pr.repo.replace("/", "__")
    files = sorted(folder.glob(f"{_run_prefix(pr)}*.md")) if folder.exists() else []
    if not files:
        return None
    try:
        text = files[-1].read_text()
    except OSError:
        return None
    head, _, body = text.partition("\n---\n\n")
    title = head.splitlines()[0].lstrip("# ").strip() if head else files[-1].stem
    fields: dict[str, str] = {}
    for line in head.splitlines():
        if line.startswith("- "):
            key, _, value = line[2:].partition(":")
            fields[key.strip()] = value.strip()
    try:
        prompt = ast.literal_eval(fields.get("prompt", "''"))  # saved with !r
    except (ValueError, SyntaxError):
        prompt = fields.get("prompt", "")
    prompt = prompt if isinstance(prompt, str) else ""
    return SavedRun(title, fields.get("when", ""), fields.get("state", ""), body.strip(), prompt,
                    fields.get("session", ""), fields.get("cwd", ""), files[-1])


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
        return list_skills(self.cfg, Path(pr.path) if pr.is_local else self.index.lookup(pr.repo))

    def run(self, pr: PullRequest, skill: Skill, prompt: str | None = None, allow_edits: bool = False) -> ClaudeRun:
        """Start a conversation with a skill, or with a typed prompt (shown as your first message)."""
        run = ClaudeRun(self.cfg, self.index, pr, skill, prompt or render_prompt(skill, pr))
        self.runs[pr.key] = run
        run.start(allow_edits, echo=prompt is not None)
        return run

    def active(self, pr: PullRequest) -> ClaudeRun | None:
        return self.runs.get(pr.key)

    def restore(self, pr: PullRequest) -> ClaudeRun | None:
        """The newest saved conversation as a finished run, which can take follow-ups if it
        recorded its session."""
        saved = load_last_run(pr)
        if saved is None:
            return None
        name = saved.title.rsplit(" on ", 1)[0] if " on " in saved.title else saved.title
        run = ClaudeRun(self.cfg, self.index, pr, Skill(name, "", "saved", ""), saved.prompt)
        run.transcript = parse_conversation(saved.body)
        run._turn_start = max((i + 1 for i, (k, _t) in enumerate(run.transcript) if k == "user"), default=0)
        run.state = saved.state or "done"
        when = saved.when.replace("T", " ")
        run.summary = f"Saved {saved.state + ' ' if saved.state not in ('', 'done') else ''}run from {when}".strip()
        run.session_id = saved.session or None
        run.session_started = bool(saved.session)
        run.cwd = run.cwd or saved.cwd or None
        run.saved_path = saved.path
        self.runs[pr.key] = run
        return run
