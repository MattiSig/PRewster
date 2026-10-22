"""Main window: collapsible sidebar (review requests, your PRs, local branches) + detail pane."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from . import local
from .github import GitHubError, PullRequest
from .poller import on_main
from .prdetail import PRDetailView
from .prlist import PRListPane
from .util import humanize

log = logging.getLogger("prewster.window")

MODES = {"reviews": "Review requests", "mine": "My pull requests", "local": "Local branches"}
STALE_AFTER = 60  # seconds before switching to Mine or Local reloads it


def _changed(old: PullRequest, new: PullRequest) -> bool:
    """For local branches: has the work moved on since it was shown?"""
    fields = ("head_sha", "additions", "deletions", "changed_files", "updated_at")
    return any(getattr(old, f) != getattr(new, f) for f in fields)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="PRewster", default_width=1320, default_height=860)
        self.app = app
        self.set_icon_name(app.get_application_id())

        self.toast = Adw.ToastOverlay()
        self.set_content(self.toast)
        self.split = Adw.OverlaySplitView(min_sidebar_width=300, max_sidebar_width=440, sidebar_width_fraction=0.28)
        self.toast.set_child(self.split)

        # -- sidebar --
        sidebar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.sidebar_title = Adw.WindowTitle(title=MODES["reviews"])
        header.set_title_widget(self.sidebar_title)
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh (Ctrl+R)", action_name="app.refresh")
        header.pack_start(self.refresh_btn)

        menu = Gio.Menu()
        menu.append("Open on GitHub", "win.open-github")
        menu.append("Edit configuration", "app.open-config")
        menu.append("Clean up Claude worktrees", "app.cleanup-worktrees")
        menu.append("About PRewster", "app.about")
        menu.append("Quit", "app.quit")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True))
        sidebar.add_top_bar(header)

        self.panes = Adw.ViewStack(vexpand=True)
        self.reviews = PRListPane("emblem-ok-symbolic", "All caught up", "No pull requests are waiting for your review.")
        self.mine = PRListPane("document-edit-symbolic", "No open pull requests",
                               "Pull requests you have open show up here, drafts included.")
        self.local = PRListPane("folder-symbolic", "No local changes",
                                "Checkouts under your search roots show up here while they are ahead of their "
                                "default branch or have uncommitted changes.")
        for name, title, pane in (("reviews", "Reviews", self.reviews), ("mine", "Mine", self.mine),
                                  ("local", "Local", self.local)):
            self.panes.add_titled(pane, name, title)
            pane.connect("selected", self._on_pane_selected)
            pane.connect("activated", lambda *_: self._close_overlay())
        switcher = Adw.InlineViewSwitcher(stack=self.panes, homogeneous=True)
        switcher.set_margin_start(8)
        switcher.set_margin_end(8)
        switcher.set_margin_bottom(6)
        sidebar.add_top_bar(switcher)
        sidebar.set_content(self.panes)
        self.split.set_sidebar(sidebar)

        # -- content --
        self.content_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        placeholder = Adw.ToolbarView()
        placeholder_header = Adw.HeaderBar()
        placeholder_header.pack_start(self._bind_sidebar_toggle(Gtk.ToggleButton()))
        placeholder.add_top_bar(placeholder_header)
        placeholder.set_content(Adw.StatusPage(icon_name="dev.rewster.PRewster", title="Select a pull request",
                                               description="Pick a pull request or a branch from the list to see its changes."))
        self.content_stack.add_named(placeholder, "placeholder")
        self.detail = PRDetailView(app.client, app.ai)
        self._bind_sidebar_toggle(self.detail.sidebar_btn)
        self.detail.connect("toast", lambda _d, msg: self.show_toast(msg))
        self.detail.connect("reviewed", self._on_reviewed)
        self.content_stack.add_named(self.detail, "detail")
        self.split.set_content(self.content_stack)

        # Window-level actions.
        open_github = Gio.SimpleAction.new("open-github", None)
        open_github.connect("activate", lambda *_: Gtk.UriLauncher.new(
            "https://github.com/pulls/review-requested").launch(self, None, None))
        self.add_action(open_github)
        toggle_sidebar = Gio.SimpleAction.new("toggle-sidebar", None)
        toggle_sidebar.connect("activate", lambda *_: self.split.set_show_sidebar(not self.split.get_show_sidebar()))
        self.add_action(toggle_sidebar)
        app.set_accels_for_action("win.toggle-sidebar", ["F9"])

        # On narrow windows (half a 1280px screen, for example) the sidebar floats over the
        # content and closes once something is picked.
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 1100sp"))
        narrow.add_setter(self.split, "collapsed", True)
        narrow.connect("apply", lambda *_: self.detail.set_narrow(True))
        narrow.connect("unapply", lambda *_: self.detail.set_narrow(False))
        self.add_breakpoint(narrow)
        self.split.connect("notify::collapsed", self._on_collapsed)
        self.panes.connect("notify::visible-child-name", self._on_mode_changed)

        self.connect("close-request", self._on_close_request)
        app.poller.connect("updated", self._on_updated)
        app.poller.connect("polling", lambda *_: self.refresh_btn.set_sensitive(False))
        GLib.timeout_add_seconds(30, self._tick_times)

        if app.poller.last_update is not None:
            self._on_updated(app.poller, app.poller.prs, app.poller.error)

    # -- helpers ----------------------------------------------------------

    def show_toast(self, message: str) -> None:
        self.toast.add_toast(Adw.Toast(title=message, timeout=4))

    def _bind_sidebar_toggle(self, button: Gtk.ToggleButton) -> Gtk.ToggleButton:
        button.set_icon_name("sidebar-show-symbolic")
        button.set_tooltip_text("Show or hide the list (F9)")
        self.split.bind_property("show-sidebar", button, "active",
                                 GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        return button

    def _close_overlay(self) -> None:
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def _current_pane(self) -> PRListPane:
        return self.panes.get_visible_child()

    def show_pr(self, key: str) -> bool:
        """Bring up a pull request from the review requests (notifications, tray menu)."""
        self.panes.set_visible_child_name("reviews")
        if self.reviews.select_key(key):
            log.debug("show_pr %s: selected", key)
            self._close_overlay()
            return True
        log.debug("show_pr %s: not in the list", key)
        self.show_toast(f"{key} is no longer waiting for your review")
        return False

    def reload(self) -> None:
        """Reload the visible list if it is Mine or Local (the poller keeps Reviews fresh)."""
        name = self.panes.get_visible_child_name()
        if name == "mine":
            cfg = self.app.config
            self._load(self.mine, lambda: self.app.client.search_pull_requests(cfg.mine_query, cfg.max_results))
        elif name == "local":
            self._load(self.local, self._find_local)

    def _find_local(self) -> list[PullRequest]:
        index = self.app.ai.index
        index.scan()  # pick up checkouts made since the last look
        return local.list_branches(index.all(), self.app.client.login or "", self.app.client.avatar_url,
                                   self.app.config.local_days)

    def _load(self, pane: PRListPane, fetch) -> None:
        if pane.busy:
            return
        pane.busy = True
        pane.set_loading()

        def work():
            try:
                on_main(done, fetch(), "")
            except (GitHubError, local.LocalError) as exc:
                on_main(done, None, str(exc))
            except Exception as exc:  # noqa: BLE001 - surface anything in the UI
                on_main(done, None, f"Unexpected error: {exc!r}")

        def done(items: list[PullRequest] | None, error: str) -> None:
            pane.busy = False
            if error and list(pane.list):
                self.show_toast(error)
            pane.set_items(items, error, datetime.now(timezone.utc))
            self._update_subtitle()

        threading.Thread(target=work, daemon=True).start()

    def _update_subtitle(self) -> None:
        when = self._current_pane().updated
        self.sidebar_title.set_subtitle(f"Updated {humanize(when)}" if when else "")

    def _show_item(self, pr: PullRequest | None) -> None:
        if pr is None:
            self.content_stack.set_visible_child_name("placeholder")
            return
        current = self.detail.pr
        if current is None or current.key != pr.key or (pr.is_local and _changed(current, pr)):
            self.detail.show(pr)
            self.detail.populate_picker()
        self.content_stack.set_visible_child_name("detail")

    # -- signal handlers --------------------------------------------------

    def _on_updated(self, _poller, prs: list[PullRequest], error: str) -> None:
        self.refresh_btn.set_sensitive(True)
        if error and prs:
            self.show_toast(error)
        self.reviews.set_items(prs if prs or not error else None, error, self.app.poller.last_update)
        self._update_subtitle()

    def _on_pane_selected(self, pane: PRListPane, pr: PullRequest | None) -> None:
        log.debug("selected: %s", pr.key if pr else None)
        if pane is self._current_pane():
            self._show_item(pr)

    def _on_mode_changed(self, *_args) -> None:
        name = self.panes.get_visible_child_name()
        pane = self._current_pane()
        self.sidebar_title.set_title(MODES[name])
        self._update_subtitle()
        stale = pane.updated is None or (datetime.now(timezone.utc) - pane.updated).total_seconds() > STALE_AFTER
        if pane is not self.reviews and stale:
            self.reload()
        self._show_item(pane.selected())

    def _on_collapsed(self, *_args) -> None:
        # Narrow window with nothing picked yet: show the list rather than an empty page.
        if self.split.get_collapsed() and self.content_stack.get_visible_child_name() == "placeholder":
            self.split.set_show_sidebar(True)

    def _on_reviewed(self, _detail, key: str, event: str) -> None:
        self.app.poller.refresh()

    def _on_close_request(self, *_args) -> bool:
        # Keep polling in the background; the waybar module or `prewster` brings it back.
        self.set_visible(False)
        return True

    def _tick_times(self) -> bool:
        for pane in (self.reviews, self.mine, self.local):
            pane.refresh_times()
        self._update_subtitle()
        return True
