#!/usr/bin/env bash
# Installs PRewster for the current user (symlinks, desktop entry, icon).
# Usage: ./install.sh [--uninstall]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
APP_ID="dev.rewster.PRewster"

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$BIN/prewster" "$BIN/prewster-waybar" "$APPS/$APP_ID.desktop" "$ICONS/$APP_ID.svg"
  echo "Removed PRewster. Config (~/.config/prewster) and cache (~/.cache/prewster) were left in place."
  exit 0
fi

missing=()
python3 - <<'PY' || missing+=("python-gobject gtk4 libadwaita gtksourceview5")
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); gi.require_version("GtkSource", "5")
from gi.repository import Gtk, Adw, GtkSource  # noqa: F401
PY
python3 -c "import requests" 2>/dev/null || missing+=("python-requests")
command -v gh >/dev/null || missing+=("github-cli")
if ((${#missing[@]})); then
  echo "Missing dependencies. Install with:  sudo pacman -S --needed ${missing[*]}" >&2
  exit 1
fi

mkdir -p "$BIN" "$APPS" "$ICONS"
ln -sfn "$HERE/bin/prewster" "$BIN/prewster"
ln -sfn "$HERE/waybar/prewster-waybar" "$BIN/prewster-waybar"
sed "s|^Exec=.*|Exec=$BIN/prewster|" "$HERE/data/$APP_ID.desktop" > "$APPS/$APP_ID.desktop"
cp "$HERE/prewster/icons/$APP_ID.svg" "$ICONS/$APP_ID.svg"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$APPS" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

if ! gh auth status >/dev/null 2>&1; then
  echo "Note: gh is not logged in. Run 'gh auth login' (the 'repo' scope is needed to approve PRs)."
fi

cat <<MSG
Installed:
  $BIN/prewster            launch the app (add --background to only poll)
  $BIN/prewster-waybar     waybar module script
  $APPS/$APP_ID.desktop

Optional integrations (see README.md):
  waybar:    add "custom/prewster" to ~/.config/waybar/config.jsonc
  autostart: exec-once = uwsm-app -- $BIN/prewster --background   (in ~/.config/hypr/autostart.conf)
MSG
