# PR Review

A native Linux (GTK4 / libadwaita) app that watches GitHub for pull requests
where your review is requested, notifies you when a new one shows up, and
lets you read the diff and approve, request changes, or comment without
leaving your desktop. Ships with a waybar module so the pending count lives
in your bar, and can run headless Claude Code skills against a PR.

## Screenshots

![Main window](docs/screenshots/main.png)

The main window: review requests on the left, the selected pull request with its changed files and highlighted diff on the right, and Comment / Request changes / Approve in the header.

![Tray icon](docs/screenshots/tray.png)

The tray icon with the pending-review count badge, next to the usual system icons in waybar.

![Notification](docs/screenshots/notification.png)

The desktop notification sent when a new review request shows up.

## How it works

- **Auth**: uses the token from the `gh` CLI (`gh auth token`). No GitHub App,
  no extra tokens. `GITHUB_TOKEN` or `GH_TOKEN` override it if set. Approving
  needs the `repo` scope, which `gh auth login` grants by default.
- **Polling**: a single background process polls the GitHub search API every
  `poll_interval` seconds (default 120), sends a desktop notification for each
  new request, and writes `~/.cache/pr-review/status.json`.
- **Tray**: the app registers a StatusNotifierItem (works with waybar's `tray`
  module, KDE, and other SNI hosts). The icon gets a red count badge when
  reviews are pending, the tooltip lists them, left click opens the app, middle
  click refreshes, and the right-click menu lists the PRs plus Open / Refresh /
  Quit. Set `tray = false` to disable.
- **Waybar module (alternative)**: `pr-review-waybar` reads the status file and
  prints module JSON with the count as text. The app sends `SIGRTMIN+11` to
  waybar after each poll so it updates instantly.
- **Window**: closing the window hides it; polling continues. Clicking the
  waybar module, a notification, or running `pr-review` again brings it back.

## Requirements (Arch)

```
sudo pacman -S --needed python-gobject gtk4 libadwaita gtksourceview5 python-requests github-cli
gh auth login
```

## Install

```
./install.sh
```

This symlinks `pr-review` and `pr-review-waybar` into `~/.local/bin`, installs a
desktop entry and icon, and leaves the checkout in place (edits take effect on
the next launch). `./install.sh --uninstall` removes them.

### Waybar

With the tray icon (default) you only need waybar's `tray` module in a modules
list. On Omarchy the tray sits inside the `group/tray-expander` hover drawer;
replace that entry with `"tray"` in `modules-right` if you want it always
visible.

If you prefer a text count in the bar instead, add `"custom/pr-review"` to a
modules list and define it:

```jsonc
"custom/pr-review": {
  "exec": "pr-review-waybar",
  "return-type": "json",
  "interval": 30,
  "signal": 11,
  "on-click": "pr-review",
  "on-click-right": "xdg-open https://github.com/pulls/review-requested"
}
```

Then in `style.css`:

```css
#custom-pr-review { min-width: 12px; margin: 0 7.5px; }
#custom-pr-review.idle { opacity: 0.5; }
#custom-pr-review.stale, #custom-pr-review.error { opacity: 0.3; }
```

Restart waybar afterwards (`omarchy restart waybar` on Omarchy).

### Autostart (Hyprland)

```
exec-once = uwsm-app -- $HOME/.local/bin/pr-review --background
```

## Claude on a pull request

The **Claude** button in the PR header lists what you can run:

- built in: *Code review* (`/code-review N`), *Security review*, *Summarize for review*
- your actions from `[[ai.actions]]` in the config
- your user skills (`~/.claude/skills`, `~/.claude/commands`)
- the repository's own skills (`.claude/skills`, `.claude/commands` in the PR's repo)
- a custom prompt

Each run:

1. finds your local checkout of the repo by origin URL (scanning `search_roots`,
   or `[ai.repos]` if set), or clones it into `~/.cache/pr-review/repos` if you
   have none
2. fetches the PR head as `refs/pr-review/N` and checks it out in a detached
   worktree under `~/.cache/pr-review/worktrees` (your working tree is untouched)
3. runs `claude -p` there with `--output-format stream-json`, streaming text and
   tool activity into a panel under the diff

Runs keep going if you switch PRs; the panel follows the selected PR. Finished
output is saved under `~/.cache/pr-review/ai/` and can be posted as a PR comment
with one click. *Menu > Clean up Claude worktrees* removes the scratch
worktrees and refs.

By default Claude gets `--permission-mode dontAsk` with a read-only tool
allowlist and no MCP servers (see `[ai]` in the config). Set
`permission_mode = "acceptEdits"` if a skill needs to write inside the scratch
worktree.

Scripting: `gdbus call --session --dest dev.rewster.PRReview --object-path
/dev/rewster/PRReview --method org.gtk.Actions.Activate ai-run
'[<"owner/repo#N|Code review">]' '{}'` opens that PR and starts the named action.

Set `PR_REVIEW_DEBUG=1` to log list updates and runs to stderr.

## Configuration

`~/.config/pr-review/config.toml` is created on first run:

| key              | default                                               | meaning |
|------------------|-------------------------------------------------------|---------|
| `poll_interval`  | `120`                                                 | seconds between polls |
| `query`          | `is:pr is:open review-requested:@me archived:false`   | GitHub search query |
| `include_drafts` | `false`                                               | list draft PRs too |
| `notify`         | `true`                                                | desktop notifications |
| `waybar_signal`  | `11`                                                  | RTMIN signal sent to waybar (0 disables) |
| `max_results`    | `50`                                                  | PRs fetched per poll |

`review-requested:@me` includes team review requests. Use
`user-review-requested:@me` for direct requests only.

The `[ai]` table configures Claude runs: `claude` (binary), `permission_mode`,
`allowed_tools`, `max_turns`, `model`, `mcp`, `search_roots`, `[ai.repos]`
(explicit `"owner/repo" = "~/path"` checkouts) and `[[ai.actions]]` entries with
`name`, `description` and `prompt` (placeholders `{number} {repo} {title} {url}
{base} {head} {author}`; a prompt starting with `/` runs that skill).

## Usage

| action                     | how |
|----------------------------|-----|
| Refresh                    | Ctrl+R |
| Hide window (keep polling) | Ctrl+W or close |
| Quit entirely              | Ctrl+Q or menu > Quit |
| Submit review dialog       | Ctrl+Enter |
| Next / previous file       | Alt+Down / Alt+Up |
| Start hidden               | `pr-review --background` |

## Layout

```
pr_review/
  app.py          Adw.Application, actions, CSS, background mode
  window.py       sidebar list + detail pane
  prdetail.py     PR metadata, file list, review buttons
  diffview.py     per-file diff: language highlighting + line-number/marker gutter
  ai.py           repo lookup, PR worktrees, skill discovery, streaming claude -p runs
  aipanel.py      streaming output panel
  skillpicker.py  popover listing available Claude actions
  prlist.py       sidebar rows
  reviewdialog.py approve / request changes / comment dialog
  poller.py       polling thread, notifications, status file, waybar signal
  github.py       GraphQL search, REST files + reviews
  config.py       paths and config.toml
waybar/pr-review-waybar   waybar module script
data/                     desktop entry and icon
```
