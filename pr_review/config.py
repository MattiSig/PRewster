"""Configuration and well-known paths."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

APP_ID = "dev.rewster.PRReview"
APP_NAME = "PR Review"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "pr-review"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "pr-review"
CONFIG_FILE = CONFIG_DIR / "config.toml"
STATUS_FILE = CACHE_DIR / "status.json"

DEFAULT_ALLOWED_TOOLS = [
    "Read", "Grep", "Glob", "LS", "Agent", "Skill", "TodoWrite", "WebSearch", "WebFetch",
    "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git status:*)",
    "Bash(git blame:*)", "Bash(git branch:*)", "Bash(git merge-base:*)", "Bash(git rev-parse:*)",
    "Bash(gh pr view:*)", "Bash(gh pr diff:*)", "Bash(gh pr checks:*)",
    "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(wc:*)", "Bash(find:*)", "Bash(rg:*)",
]
DEFAULT_SEARCH_ROOTS = ["~/product", "~/personal", "~/Projects", "~/work", "~/src", "~/code", "~/dev", "~/hr"]

DEFAULT_CONFIG = """\
# PR Review configuration. Restart the app after editing.

# Seconds between GitHub polls.
poll_interval = 120

# GitHub search query for the pull requests to track.
# "review-requested:@me" covers direct requests and team requests.
query = "is:pr is:open review-requested:@me archived:false"

# Show draft pull requests in the list.
include_drafts = false

# Send a desktop notification when a new review request appears.
notify = true

# Real-time signal sent to waybar after each poll (pkill -RTMIN+<n> waybar).
waybar_signal = 11

# Maximum number of pull requests to fetch per poll (1-100).
max_results = 50

[ai]
# Headless Claude Code runs. Each run checks the PR head out into a scratch
# worktree under ~/.cache/pr-review/worktrees and runs `claude -p` there.
claude = "claude"

# Permission mode for the run: "dontAsk" denies anything not in allowed_tools,
# "acceptEdits" lets Claude edit files inside the scratch worktree.
permission_mode = "dontAsk"

# Tools Claude may use without asking. Claude Code permission syntax.
allowed_tools = [
  "Read", "Grep", "Glob", "LS", "Agent", "Skill", "TodoWrite", "WebSearch", "WebFetch",
  "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git status:*)",
  "Bash(git blame:*)", "Bash(git branch:*)", "Bash(git merge-base:*)", "Bash(git rev-parse:*)",
  "Bash(gh pr view:*)", "Bash(gh pr diff:*)", "Bash(gh pr checks:*)",
  "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(wc:*)", "Bash(find:*)", "Bash(rg:*)",
]

# Stop a run after this many agent turns.
max_turns = 100

# Model override, empty for the default.
model = ""

# Load MCP servers from your Claude settings. Off keeps runs fast and cheap.
mcp = false

# Directories scanned (3 levels deep) for local checkouts, matched by origin URL.
search_roots = ["~/product", "~/personal", "~/Projects", "~/work", "~/src", "~/code", "~/dev", "~/hr"]

# Explicit checkout locations, used before the scan. Repos not found anywhere
# are cloned into ~/.cache/pr-review/repos.
[ai.repos]
# "owner/repo" = "~/src/repo"

# Extra actions in the Claude menu. Placeholders: {number} {repo} {title} {url}
# {base} {head} {author}. A prompt starting with "/" runs that skill.
# [[ai.actions]]
# name = "Matti review"
# description = "Run the repo's matti-review skill on this PR"
# prompt = "/matti-review {number}"
"""


@dataclass
class AIAction:
    name: str
    prompt: str
    description: str = ""


@dataclass
class AIConfig:
    claude: str = "claude"
    permission_mode: str = "dontAsk"
    allowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS))
    max_turns: int = 100
    model: str = ""
    mcp: bool = False
    search_roots: list[str] = field(default_factory=lambda: list(DEFAULT_SEARCH_ROOTS))
    repos: dict[str, str] = field(default_factory=dict)
    actions: list[AIAction] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "AIConfig":
        cfg = cls()
        for key in ("claude", "permission_mode", "model"):
            if isinstance(data.get(key), str):
                setattr(cfg, key, data[key])
        if isinstance(data.get("allowed_tools"), list):
            cfg.allowed_tools = [str(t) for t in data["allowed_tools"]]
        if isinstance(data.get("search_roots"), list):
            cfg.search_roots = [str(r) for r in data["search_roots"]]
        if isinstance(data.get("max_turns"), int):
            cfg.max_turns = max(1, data["max_turns"])
        if isinstance(data.get("mcp"), bool):
            cfg.mcp = data["mcp"]
        if isinstance(data.get("repos"), dict):
            cfg.repos = {str(k): str(v) for k, v in data["repos"].items()}
        for item in data.get("actions") or []:
            if isinstance(item, dict) and item.get("name") and item.get("prompt"):
                cfg.actions.append(AIAction(str(item["name"]), str(item["prompt"]), str(item.get("description", ""))))
        return cfg


@dataclass
class Config:
    poll_interval: int = 120
    query: str = "is:pr is:open review-requested:@me archived:false"
    include_drafts: bool = False
    notify: bool = True
    waybar_signal: int = 11
    max_results: int = 50
    ai: AIConfig = field(default_factory=AIConfig)

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if not CONFIG_FILE.exists():
            try:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                CONFIG_FILE.write_text(DEFAULT_CONFIG)
            except OSError:
                pass
            return cfg
        try:
            data = tomllib.loads(CONFIG_FILE.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            return cfg
        for key in ("poll_interval", "query", "include_drafts", "notify", "waybar_signal", "max_results"):
            if key in data and isinstance(data[key], type(getattr(cfg, key))):
                setattr(cfg, key, data[key])
        cfg.poll_interval = max(30, int(cfg.poll_interval))
        cfg.max_results = min(100, max(1, int(cfg.max_results)))
        if isinstance(data.get("ai"), dict):
            cfg.ai = AIConfig.from_dict(data["ai"])
        return cfg
