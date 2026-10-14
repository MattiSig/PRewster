"""Background polling, status file for waybar, and desktop notifications."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import datetime, timezone

from gi.repository import Gio, GLib, GObject

from .config import APP_ID, CACHE_DIR, STATUS_FILE, Config
from .github import GitHubClient, GitHubError, PullRequest


def on_main(fn, *args):
    """Run fn(*args) once on the GTK main loop."""
    def _call():
        fn(*args)
        return False
    GLib.idle_add(_call)


class Poller(GObject.Object):
    __gsignals__ = {
        # Emitted after every poll. Arguments: list of PullRequest, error message or "".
        "updated": (GObject.SignalFlags.RUN_FIRST, None, (object, str)),
        # Emitted when a poll starts.
        "polling": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, app: Gio.Application, client: GitHubClient, config: Config):
        super().__init__()
        self.app = app
        self.client = client
        self.config = config
        self.prs: list[PullRequest] = []
        self.error: str = ""
        self.last_update: datetime | None = None
        self._known: set[str] | None = self._load_known()
        self._busy = False
        self._timer_id = 0

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self.refresh()
        self._timer_id = GLib.timeout_add_seconds(self.config.poll_interval, self._tick)

    def stop(self) -> None:
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0

    def _tick(self) -> bool:
        self.refresh()
        return True

    def refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.emit("polling")
        threading.Thread(target=self._fetch, daemon=True).start()

    # -- worker -----------------------------------------------------------

    def _fetch(self) -> None:
        try:
            prs = self.client.list_review_requests(self.config.query, self.config.max_results)
            if not self.config.include_drafts:
                prs = [p for p in prs if not p.is_draft]
            on_main(self._apply, prs, "")
        except GitHubError as exc:
            on_main(self._apply, None, str(exc))
        except Exception as exc:  # noqa: BLE001 - surface anything in the UI
            on_main(self._apply, None, f"Unexpected error: {exc!r}")

    def _apply(self, prs: list[PullRequest] | None, error: str) -> None:
        self._busy = False
        self.last_update = datetime.now(timezone.utc)
        if prs is None:
            self.error = error
        else:
            self.error = ""
            self._notify_new(prs)
            self.prs = prs
            self._known = {p.key for p in prs}
        self._write_status()
        self._signal_waybar()
        self.emit("updated", self.prs, self.error)

    # -- notifications ----------------------------------------------------

    def _notify_new(self, prs: list[PullRequest]) -> None:
        if not self.config.notify:
            return
        if self._known is None:
            # First run without a cache: one summary rather than a burst.
            if prs:
                self._send_summary(prs)
            return
        new = [p for p in prs if p.key not in self._known]
        if not new:
            return
        if len(new) > 3:
            self._send_summary(new)
            return
        for pr in new:
            n = Gio.Notification.new(f"Review requested: {pr.title}")
            n.set_body(f"{pr.key} by {pr.author}")
            n.set_icon(Gio.ThemedIcon.new(APP_ID))
            n.set_default_action_and_target("app.show-pr", GLib.Variant("s", pr.key))
            n.add_button_with_target("Open in browser", "app.open-url", GLib.Variant("s", pr.url))
            self.app.send_notification(f"pr-{pr.key}", n)

    def _send_summary(self, prs: list[PullRequest]) -> None:
        n = Gio.Notification.new(f"{len(prs)} pull request{'s' if len(prs) != 1 else ''} awaiting your review")
        n.set_body("\n".join(f"{p.key}: {p.title}" for p in prs[:6]))
        n.set_icon(Gio.ThemedIcon.new(APP_ID))
        n.set_default_action("app.show")
        self.app.send_notification("pr-summary", n)

    # -- status file + waybar ---------------------------------------------

    def _load_known(self) -> set[str] | None:
        try:
            data = json.loads(STATUS_FILE.read_text())
            return {p["key"] for p in data.get("prs", [])}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_status(self) -> None:
        payload = {
            "updated": self.last_update.isoformat() if self.last_update else None,
            "count": len(self.prs),
            "error": self.error or None,
            "login": self.client.login,
            "poll_interval": self.config.poll_interval,
            "prs": [
                {
                    "key": p.key,
                    "repo": p.repo,
                    "number": p.number,
                    "title": p.title,
                    "author": p.author,
                    "url": p.url,
                    "updated_at": p.updated_at.isoformat(),
                    "is_draft": p.is_draft,
                    "ci": p.ci_state,
                }
                for p in self.prs
            ],
        }
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = STATUS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=1))
            os.replace(tmp, STATUS_FILE)
        except OSError:
            pass

    def _signal_waybar(self) -> None:
        sig = self.config.waybar_signal
        if sig <= 0:
            return
        try:
            subprocess.Popen(["pkill", f"-RTMIN+{sig}", "waybar"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
