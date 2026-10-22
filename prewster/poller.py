"""Background polling, status file for waybar, and desktop notifications.

The review-request search is the source of truth. When the notifications inbox can be
read, it is checked every minute (unchanged checks are free) and any new or updated
pull request thread triggers a search right away, so the regular search can slow down
to a safety net for requests that were withdrawn or finished elsewhere.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from datetime import datetime, timezone

from gi.repository import Gio, GLib, GObject

from . import sound
from .config import APP_ID, CACHE_DIR, STATUS_FILE, Config
from .github import GitHubClient, GitHubError, Inbox, PullRequest

log = logging.getLogger("prewster.poller")

INBOX_INTERVAL = 60  # never read the inbox more often than this, whatever X-Poll-Interval says


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
        self.last_update: datetime | None = None  # last search
        self.watching = False  # the notifications inbox answers and is being watched
        self._known: set[str] | None = self._load_known()
        self._busy = False
        self._again = False  # a refresh was asked for while one was running
        self._stopped = False
        self._search_timer = 0
        self._inbox_timer = 0
        self._inbox_enabled = config.watch_notifications  # off for good after a 403/404
        self._inbox_interval = INBOX_INTERVAL
        self._inbox_modified: str | None = None
        self._inbox_threads: dict[str, str] | None = None  # None until the first read

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._stopped = False
        if self._inbox_enabled:
            self._check_inbox()  # its first answer runs the first search
        else:
            self.refresh()

    def stop(self) -> None:
        self._stopped = True
        for name in ("_search_timer", "_inbox_timer"):
            if getattr(self, name):
                GLib.source_remove(getattr(self, name))
                setattr(self, name, 0)

    @property
    def search_interval(self) -> int:
        return self.config.watched_poll_interval if self.watching else self.config.poll_interval

    def _schedule_search(self) -> None:
        if self._search_timer:
            GLib.source_remove(self._search_timer)
            self._search_timer = 0
        if not self._stopped:
            self._search_timer = GLib.timeout_add_seconds(self.search_interval, self._on_search_timer)

    def _on_search_timer(self) -> bool:
        self._search_timer = 0
        self.refresh()
        return False

    def refresh(self) -> None:
        if self._busy:
            self._again = True
            return
        self._busy = True
        self.emit("polling")
        threading.Thread(target=self._fetch, daemon=True).start()

    # -- worker -----------------------------------------------------------

    def _fetch(self) -> None:
        try:
            prs = self.client.search_pull_requests(self.config.query, self.config.max_results)
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
        self._schedule_search()
        if self._again:
            self._again = False
            self.refresh()

    # -- notifications inbox ----------------------------------------------

    def _check_inbox(self) -> bool:
        self._inbox_timer = 0
        threading.Thread(target=self._fetch_inbox, args=(self._inbox_modified,), daemon=True).start()
        return False

    def _fetch_inbox(self, last_modified: str | None) -> None:
        try:
            on_main(self._apply_inbox, self.client.notifications(last_modified), None)
        except Exception as exc:  # noqa: BLE001 - a failed check only pauses watching
            on_main(self._apply_inbox, None, exc)

    def _apply_inbox(self, inbox: Inbox | None, error: Exception | None) -> None:
        if self._stopped:
            return
        never_searched = self.last_update is None and not self._busy
        if inbox is None:
            self._set_watching(False)
            if isinstance(error, GitHubError) and error.status in (403, 404):
                log.warning("cannot read GitHub notifications (%s); searching every %d s instead",
                            error, self.config.poll_interval)
                self._inbox_enabled = False
            else:
                log.debug("notifications check failed, searching every %d s until it answers: %s",
                          self.config.poll_interval, error)
            if never_searched:
                self.refresh()
        else:
            self._inbox_interval = max(INBOX_INTERVAL, inbox.poll_interval)
            self._set_watching(True)
            if inbox.changed:
                seen, self._inbox_threads = self._inbox_threads, inbox.threads
                self._inbox_modified = inbox.last_modified
                if seen is None or any(seen.get(tid) != stamp for tid, stamp in inbox.threads.items()):
                    log.debug("pull request activity in the notifications inbox, searching")
                    self.refresh()
        if self.last_update is not None and not self._busy:
            self._write_status()  # heartbeat for the waybar module
        if self._inbox_enabled:
            self._inbox_timer = GLib.timeout_add_seconds(self._inbox_interval, self._check_inbox)

    def _set_watching(self, watching: bool) -> None:
        if watching == self.watching:
            return
        self.watching = watching
        log.debug("watching notifications: %s, searching every %d s", watching, self.search_interval)
        if self._search_timer:  # idle between searches: restart the wait at the new interval
            self._schedule_search()

    # -- notifications ----------------------------------------------------

    def _notify_new(self, prs: list[PullRequest]) -> None:
        if not self.config.notify:
            return
        new = prs if self._known is None else [p for p in prs if p.key not in self._known]
        if not new:
            return
        # First run without a cache, or a burst: one summary rather than a pile of popups.
        if self._known is None or len(new) > 3:
            self._send_summary(new)
        else:
            for pr in new:
                n = Gio.Notification.new(f"Review requested: {pr.title}")
                n.set_body(f"{pr.key} by {pr.author}")
                n.set_icon(Gio.ThemedIcon.new(APP_ID))
                n.set_default_action_and_target("app.show-pr", GLib.Variant("s", pr.key))
                n.add_button_with_target("Open in browser", "app.open-url", GLib.Variant("s", pr.url))
                self.app.send_notification(f"pr-{pr.key}", n)
        sound.play(self.config.sound)  # once per poll, however many arrived

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
            # When this file was last written: after every search and inbox check, so the
            # waybar module can tell a live app from a dead one. poll_interval is the gap
            # until the next write.
            "checked": datetime.now(timezone.utc).isoformat(),
            "count": len(self.prs),
            "error": self.error or None,
            "login": self.client.login,
            "poll_interval": self._inbox_interval if self._inbox_enabled else self.config.poll_interval,
            "watching_notifications": self.watching,
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
