"""Adw.Application: single instance, actions, notifications, background mode."""

from __future__ import annotations

import logging
import os
import sys

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import __version__
import threading

from .ai import AIService, cleanup_worktrees
from .config import APP_ID, APP_NAME, CONFIG_FILE, Config
from .github import GitHubClient
from .poller import Poller
from .tray import TrayIcon
from .window import MainWindow

CSS = """
.badge {
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 0.78em;
  font-weight: 600;
  color: var(--view-fg-color);
  background: color-mix(in srgb, currentColor 10%, transparent);
}
.badge-ok   { color: var(--success-color); background: color-mix(in srgb, var(--success-color) 15%, transparent); }
.badge-warn { color: var(--warning-color); background: color-mix(in srgb, var(--warning-color) 18%, transparent); }
.badge-err  { color: var(--error-color);   background: color-mix(in srgb, var(--error-color) 15%, transparent); }
.badge-info { color: var(--accent-color);  background: color-mix(in srgb, var(--accent-color) 15%, transparent); }
.badge-muted { opacity: 0.7; }
.file-status { font-family: monospace; padding: 1px 6px; min-width: 10px; }
.diff-view { font-size: 0.92em; }
.ai-panel { background: var(--view-bg-color); }
"""


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.add_main_option("background", ord("b"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Start polling without showing the window", None)
        self.add_main_option("version", ord("v"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Print the version and exit", None)
        self.connect("handle-local-options", self._on_local_options)
        self._background = False
        self.window: MainWindow | None = None
        self.config: Config | None = None
        self.client: GitHubClient | None = None
        self.poller: Poller | None = None

    # -- startup ----------------------------------------------------------

    def _on_local_options(self, _app, options: GLib.VariantDict) -> int:
        if options.contains("version"):
            print(f"{APP_NAME} {__version__}")
            return 0
        if options.contains("background"):
            try:
                self.register(None)
            except GLib.Error as exc:
                print(f"Could not register application: {exc.message}", file=sys.stderr)
                return 1
            if self.get_is_remote():
                return 0  # Already running: nothing to do.
            self._background = True
        return -1

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        logging.basicConfig(level=logging.DEBUG if os.environ.get("PR_REVIEW_DEBUG") else logging.WARNING,
                            format="%(asctime)s %(name)s %(levelname)s %(message)s")
        self.config = Config.load()
        self.client = GitHubClient()
        self.poller = Poller(self, self.client, self.config)
        self.ai = AIService(self.config.ai)
        self.tray: TrayIcon | None = None

        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._add_action("refresh", lambda *_: self.poller.refresh(), accel="<Control>r")
        self._add_action("quit", lambda *_: self.quit(), accel="<Control>q")
        self._add_action("show", lambda *_: self._present())
        self._add_action("show-pr", self._on_show_pr, param="s")
        self._add_action("open-url", lambda _a, p: Gtk.UriLauncher.new(p.get_string()).launch(self.window, None, None), param="s")
        self._add_action("open-config", lambda *_: Gio.AppInfo.launch_default_for_uri(CONFIG_FILE.as_uri(), None))
        self._add_action("about", self._on_about)
        self._add_action("cleanup-worktrees", lambda *_: self._cleanup_worktrees())
        # "owner/repo#N|Skill name": open that PR and start the named Claude action.
        self._add_action("ai-run", self._on_ai_run, param="s")
        self.set_accels_for_action("window.close", ["<Control>w"])

        self.hold()  # Keep running after the window is hidden.
        if self.config.tray:
            try:
                self.tray = TrayIcon(self)
                self.poller.connect("updated", lambda _p, prs, err: self.tray.update(prs, err))
            except Exception as exc:  # noqa: BLE001 - the tray is optional
                logging.getLogger("pr_review").warning("tray icon unavailable: %s", exc)
        self.poller.start()

    def do_activate(self) -> None:
        if self.window is None:
            self.window = MainWindow(self)
        if self._background:
            self._background = False
            return
        self._present()

    # -- actions ----------------------------------------------------------

    def _add_action(self, name: str, callback, param: str | None = None, accel: str | None = None) -> None:
        action = Gio.SimpleAction.new(name, GLib.VariantType.new(param) if param else None)
        action.connect("activate", callback)
        self.add_action(action)
        if accel:
            self.set_accels_for_action(f"app.{name}", [accel])

    def _present(self) -> None:
        if self.window is None:
            self.window = MainWindow(self)
        self.window.present()

    def _on_show_pr(self, _action, param: GLib.Variant) -> None:
        self._present()
        self.window.show_pr(param.get_string())

    def _on_ai_run(self, _action, param: GLib.Variant) -> None:
        key, _, skill = param.get_string().partition("|")
        self._present()
        if not self.window.show_pr(key):
            return
        detail = self.window.detail
        if skill and detail.pr is not None and detail.pr.key == key:
            detail.run_skill_named(skill)

    def _cleanup_worktrees(self) -> None:
        def work():
            try:
                n = cleanup_worktrees()
                msg = f"Removed {n} Claude worktree{'s' if n != 1 else ''}"
            except Exception as exc:  # noqa: BLE001
                msg = f"Cleanup failed: {exc}"
            GLib.idle_add(lambda: (self.window and self.window.show_toast(msg), False)[1])
        threading.Thread(target=work, daemon=True).start()

    def _on_about(self, *_args) -> None:
        about = Adw.AboutDialog(application_name=APP_NAME, application_icon=APP_ID, version=__version__,
                                developer_name="Matthías Sigurbjörnsson", comments="GitHub pull requests awaiting your review.",
                                license_type=Gtk.License.MIT_X11)
        about.present(self.window)
