"""Waybar custom-module output for PRewster.

Reads the status file the app writes after every check and prints waybar JSON.
Wire it up with "return-type": "json" and "signal": 11 (see README).
"""

import html
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

STATUS = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "prewster" / "status.json"
ICON = ""  # nf-oct-git_pull_request


def emit(text: str, tooltip: str, css_class: str) -> None:
    print(json.dumps({"text": text, "tooltip": html.escape(tooltip), "class": css_class, "alt": css_class}))
    sys.exit(0)


def main() -> None:
    try:
            data = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        emit(ICON, "PRewster is not running\nClick to start it", "stale")

    age = None
    stamp = data.get("checked") or data.get("updated")
    if stamp:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()
        except ValueError:
            age = None
    interval = int(data.get("poll_interval") or 120)
    if age is None or age > 3 * interval + 60:
        emit(ICON, "PRewster is not running\nClick to start it", "stale")

    count = int(data.get("count") or 0)
    prs = data.get("prs") or []
    if data.get("error"):
        emit(ICON, f"PRewster: {data['error']}", "error")
    if count == 0:
        emit(ICON, "No pull requests waiting for your review", "idle")

    lines = [f"{p['key']}  {p['title']}  — {p['author']}" for p in prs[:12]]
    if count > len(lines):
        lines.append(f"…and {count - len(lines)} more")
    title = f"{count} pull request{'s' if count != 1 else ''} waiting for your review"
    emit(f"{ICON} {count}", title + "\n\n" + "\n".join(lines), "pending")


if __name__ == "__main__":
    main()
