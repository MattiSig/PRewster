<p align="center">
  <img src="docs/logo.svg" alt="PRewster" width="200">
</p>

<h1 align="center">PRewster</h1>

<p align="center">
  <strong>The rooster that crows when a pull request needs your review.</strong><br>
  A native Linux (GTK4 / libadwaita) monitor for GitHub review requests: tray icon, desktop notifications,<br>
  in-app diffs with approve / request changes, and headless Claude Code skills on any PR.
</p>

<p align="center">
  <a href="https://github.com/MattiSig/PRewster/releases"><img alt="Release" src="https://img.shields.io/github/v/release/MattiSig/PRewster?include_prereleases&label=release&color=f59e0b"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-1e293b.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white">
  <img alt="GTK 4 / libadwaita" src="https://img.shields.io/badge/GTK_4-libadwaita-4A86CF">
  <img alt="Linux" src="https://img.shields.io/badge/platform-Linux-FCC624?logo=linux&logoColor=black">
</p>

---

## Screenshots

![Main window](docs/screenshots/main.png)

The main window: review requests on the left, the selected pull request with its changed files and highlighted diff on the right, and Comment / Request changes / Approve in the header.

![Tray icon](docs/screenshots/tray.png)

The tray icon with the pending-review count badge, next to the usual system icons in waybar.

![Notification](docs/screenshots/notification.png)

The desktop notification sent when a new review request appears. Clicking it opens that PR in the app.

## Features

- **Pings you** the moment someone requests your review: a desktop notification with a click-through to the PR, a rooster's crow (quiet in do-not-disturb), and a tray icon whose badge shows how many are waiting.
- **Reads the diff natively**: per-file view, syntax highlighting by language, old/new line numbers and change markers in a gutter, keyboard navigation between files. A file gets a tick once you have scrolled to its end, and keeps it until a new push changes that file.
- **Shows the conversation**: the description, comments and reviews in a Conversation tab, and review threads inline under the lines they were left on.
- **Reviews from the app**: approve, request changes or comment without opening a browser.
- **Previews your own work**: your open pull requests under *Mine*, and the branches in your local checkouts that are ahead of their default branch or have uncommitted changes under *Local*, with the same diff view and Claude actions.
- **Chats with Claude Code about the PR**: built-in code review, security review and summary actions, plus your own and the repository's skills, streamed into a panel under the diff. Follow up in the same conversation, let Claude edit when you allow it, or continue in an interactive session in your terminal. Output can be posted as a comment in one click.
- **Stays out of the way**: a single background process, closing the window just hides it, and everything is configurable in one TOML file.
- **Uses what you already have**: authentication comes from the `gh` CLI, no GitHub App or extra tokens.

## Quick start

Arch Linux (Omarchy and friends):

```
sudo pacman -S --needed python-gobject python-cairo python-requests gtk4 libadwaita gtksourceview5 librsvg github-cli
gh auth login
git clone https://github.com/MattiSig/PRewster.git && cd PRewster
./install.sh
prewster
```

`install.sh` symlinks `prewster` and `prewster-waybar` into `~/.local/bin`, installs a desktop entry and icon, and leaves the checkout in place so `git pull` updates the app. `./install.sh --uninstall` removes them.

Other ways to install:

- **Arch package**: `packaging/arch/PKGBUILD` builds a proper package from a release tag (`makepkg -si` inside that folder).
- **pip / uv**: `pip install --user .` or `uv tool install --python-preference system .` (the GTK bindings must come from your distribution, so use the system Python).

### Autostart (Hyprland)

```
exec-once = uwsm-app -- $HOME/.local/bin/prewster --background
```

## How it works

- **Auth**: uses the token from the `gh` CLI (`gh auth token`). `GITHUB_TOKEN` or `GH_TOKEN` override it if set. Approving needs the `repo` scope, which `gh auth login` grants by default. The token lives only in memory.
- **Polling**: a single background process searches GitHub for review requests, sends a desktop notification for each new one, and writes `~/.cache/prewster/status.json`. GitHub has no push channel for a user's own notifications, so instead it watches your notifications inbox: a conditional read every 60 s (the rate GitHub asks for; unchanged reads cost nothing against the rate limit), and a search as soon as a pull request thread there is new or updated, so new requests show up within about a minute. The search stays the source of truth and still runs every `watched_poll_interval` seconds (default 300) to drop requests that were withdrawn or reviewed elsewhere. If the token can't read notifications (fine-grained personal access tokens can't), or `watch_notifications = false`, it searches every `poll_interval` seconds (default 120) instead.
- **Tray**: the app registers a StatusNotifierItem (works with waybar's `tray` module, KDE, and other SNI hosts). The icon gets a red count badge when reviews are pending, the tooltip lists them, left click opens the app, middle click refreshes, and the right-click menu lists the PRs plus Open / Refresh / Quit. Set `tray = false` to disable.
- **Waybar module (alternative)**: `prewster-waybar` reads the status file and prints module JSON with the count as text. The app sends `SIGRTMIN+11` to waybar after each search so it updates instantly.
- **Window**: closing the window hides it; polling continues. Clicking the tray icon, a notification, or running `prewster` again brings it back. The sidebar folds away with the button at the top left (or F9), and floats over the pull request on narrow windows.
- **Mine and Local**: *Mine* searches `mine_query` (your open pull requests, drafts included) when you open it and on refresh. *Local* lists the checkouts PRewster knows from `search_roots` and `[ai.repos]`, including their linked worktrees, whose branch is ahead of the default branch (`origin/HEAD`) or has uncommitted changes, touched in the last `local_days` days. Its diff is taken against the merge-base and includes uncommitted edits and untracked files; the commit list stands in for the description.

### Waybar

With the tray icon (default) you only need waybar's `tray` module in a modules list. On Omarchy the tray sits inside the `group/tray-expander` hover drawer; replace that entry with `"tray"` in `modules-right` if you want it always visible.

If you prefer a text count in the bar instead, add `"custom/prewster"` to a modules list and define it:

```jsonc
"custom/prewster": {
  "exec": "prewster-waybar",
  "return-type": "json",
  "interval": 30,
  "signal": 11,
  "on-click": "prewster",
  "on-click-right": "xdg-open https://github.com/pulls/review-requested"
}
```

Then in `style.css`:

```css
#custom-prewster { min-width: 12px; margin: 0 7.5px; }
#custom-prewster.idle { opacity: 0.5; }
#custom-prewster.stale, #custom-prewster.error { opacity: 0.3; }
```

Restart waybar afterwards (`omarchy restart waybar` on Omarchy).

## Claude on a pull request

The **Claude** button in the PR header lists what you can run:

- built in: *Code review* (`/code-review N`), *Security review*, *Summarize for review*
- your actions from `[[ai.actions]]` in the config
- your user skills (`~/.claude/skills`, `~/.claude/commands`)
- the repository's own skills (`.claude/skills`, `.claude/commands` in the PR's repo)
- *Chat with Claude*, which opens the panel for you to type in

Each run:

1. finds your local checkout of the repo by origin URL (scanning `search_roots`, or `[ai.repos]` if set), or clones it into `~/.cache/prewster/repos` if you have none
2. fetches the PR head as `refs/prewster/N` and checks it out in a detached worktree under `~/.cache/prewster/worktrees` (your working tree is untouched)
3. runs `claude -p` there with `--output-format stream-json`, streaming text and tool activity into a panel under the diff

Runs keep going if you switch PRs; the panel follows the selected PR. *Menu > Clean up Claude worktrees* removes the scratch worktrees and refs.

Every run is a conversation. Type in the box under the output to follow up: the next turn resumes the same Claude session (`claude -p --resume`) in the same checkout, so Claude remembers what it read and said. With nothing running, the box starts a new chat; the **+** button clears the panel for one. The pencil button allows edits for the turns you send while it is on (`acceptEdits`, plus Edit and Write); it is off by default and resets when you switch pull requests. The terminal button continues the conversation in interactive Claude Code, in a terminal (`terminal` under `[ai]`, else `xdg-terminal-exec`, `$TERMINAL` or a common terminal), for anything the headless allowlist won't do. Conversations are saved under `~/.cache/prewster/ai/` with their session, so they can be continued after a restart, and Claude's latest answer can be posted as a PR comment with one click.

On a local branch Claude runs in your checkout itself, and the built-in actions review the branch against its default branch instead of a PR number.

Claude runs with `--setting-sources user` (in the terminal too, and on local branches, which may be someone's PR you checked out), so only your own Claude settings and hooks apply and nothing from the checkout's `.claude/` directory is loaded. By default it also gets `--permission-mode dontAsk` with a tool allowlist and no MCP servers (see `[ai]` in the config). The allowlist is a prefix match, so entries like `Bash(find:*)` and `Bash(rg:*)` are powerful; trim them if you review PRs from people you don't trust. Set `permission_mode = "acceptEdits"` if a skill needs to write inside the scratch worktree.

Scripting: `gdbus call --session --dest dev.rewster.PRewster --object-path /dev/rewster/PRewster --method org.gtk.Actions.Activate ai-run '[<"owner/repo#N|Code review">]' '{}'` opens that PR and starts the named action.

Set `PREWSTER_DEBUG=1` to log list updates and runs to stderr.

## Configuration

`~/.config/prewster/config.toml` is created on first run:

| key              | default                                               | meaning |
|------------------|-------------------------------------------------------|---------|
| `poll_interval`  | `120`                                                 | seconds between searches when the notifications inbox isn't watched |
| `watch_notifications` | `true`                                           | watch the notifications inbox and search when PR activity shows up there |
| `watched_poll_interval` | `300`                                          | seconds between safety-net searches while the inbox is watched |
| `query`          | `is:pr is:open review-requested:@me archived:false`   | GitHub search query |
| `mine_query`     | `is:pr is:open author:@me archived:false`             | search for the *Mine* list |
| `local_days`     | `14`                                                  | *Local* lists checkouts touched in this many days (0: all) |
| `include_drafts` | `false`                                               | list draft PRs too |
| `notify`         | `true`                                                | desktop notifications |
| `sound`          | `rooster`                                             | sound played with a notification: `rooster`, a path to a sound file, or `""` for none |
| `waybar_signal`  | `11`                                                  | RTMIN signal sent to waybar (0 disables) |
| `max_results`    | `50`                                                  | PRs fetched per poll |
| `tray`           | `true`                                                | system tray icon |

`review-requested:@me` includes team review requests. Use `user-review-requested:@me` for direct requests only.

The sound plays once per poll, however many requests arrived, and is skipped while notifications are silenced (mako's `do-not-disturb` mode, dunst or swaync paused, GNOME banners off). It is played with `pw-play`, `paplay` or `canberra-gtk-play`, whichever is installed. `prewster --test-sound` plays the configured sound.

The `[ai]` table configures Claude runs: `claude` (binary), `permission_mode`, `allowed_tools`, `max_turns`, `model`, `mcp`, `terminal` (for continuing a chat interactively, e.g. `"ghostty -e"`), `search_roots` (also the places *Local* looks), `[ai.repos]` (explicit `"owner/repo" = "~/path"` checkouts) and `[[ai.actions]]` entries with `name`, `description` and `prompt` (placeholders `{number} {repo} {title} {url} {base} {head} {author}`; a prompt starting with `/` runs that skill).

## Usage

| action                     | how |
|----------------------------|-----|
| Refresh                    | Ctrl+R |
| Hide window (keep polling) | Ctrl+W or close |
| Quit entirely              | Ctrl+Q or menu > Quit |
| Submit review dialog       | Ctrl+Enter |
| Next / previous file       | Alt+Down / Alt+Up |
| Show / hide the sidebar    | F9 or the sidebar button |
| Send a message to Claude   | Enter in the panel's box |
| Start hidden               | `prewster --background` |
| Hear the crow              | `prewster --test-sound` |

## Releases

Tagging `vX.Y.Z` on `master` runs the release workflow, which builds the sdist and wheel and publishes a GitHub Release with generated notes. Bump `__version__` in `prewster/__init__.py` and `pkgver` in the PKGBUILD first.

## Layout

```
prewster/
  app.py          Adw.Application, actions, CSS, background mode
  window.py       collapsible sidebar (Reviews / Mine / Local) + detail pane
  prdetail.py     PR metadata, files and conversation tabs, review buttons, Claude chat
  diffview.py     per-file diff: highlighting, line-number/marker gutter, inline review threads
  conversation.py description, comments, reviews and review threads
  prlist.py       sidebar rows and list panes
  local.py        local checkouts: branches ahead of their default branch, their diffs
  viewed.py       files you have scrolled to the end of
  reviewdialog.py approve / request changes / comment dialog
  skillpicker.py  popover listing available Claude actions
  aipanel.py      streaming chat panel
  ai.py           repo lookup, PR worktrees, skill discovery, resumable claude -p chats
  tray.py         StatusNotifierItem + DBusMenu tray icon
  poller.py       polling thread, notifications, status file, waybar signal
  sound.py        notification sound, do-not-disturb check
  github.py       GraphQL search and comments, notifications inbox, REST files + reviews
  waybar.py       waybar custom-module output
  config.py       paths and config.toml
  icons/          app icon (SVG)
  sounds/         rooster crow (Ogg Vorbis)
data/             desktop entry
docs/             logo and screenshots
packaging/arch/   PKGBUILD
```

## License

MIT, see [LICENSE](LICENSE). The rooster crow is trimmed from a public domain recording by alys on pdsounds.org ([Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Medium_rooster_crowing.ogg)).
