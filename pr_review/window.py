"""Main window: sidebar of review requests + detail pane."""

from __future__ import annotations

import logging

from gi.repository import Adw, Gio, GLib, Gtk

from .github import PullRequest
from .prdetail import PRDetailView
from .prlist import PRRow
from .util import humanize

log = logging.getLogger("pr_review.window")


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="PR Review", default_width=1320, default_height=860)
        self.app = app
        self.set_icon_name(app.get_application_id())

        self.toast = Adw.ToastOverlay()
        self.set_content(self.toast)
        self.split = Adw.NavigationSplitView(min_sidebar_width=300, max_sidebar_width=440, sidebar_width_fraction=0.28)
        self.toast.set_child(self.split)

        # -- sidebar --
        sidebar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.sidebar_title = Adw.WindowTitle(title="Review requests")
        header.set_title_widget(self.sidebar_title)
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh (Ctrl+R)", action_name="app.refresh")
        header.pack_start(self.refresh_btn)

        menu = Gio.Menu()
        menu.append("Open on GitHub", "win.open-github")
        menu.append("Edit configuration", "app.open-config")
        menu.append("Clean up Claude worktrees", "app.cleanup-worktrees")
        menu.append("About PR Review", "app.about")
        menu.append("Quit", "app.quit")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True))
        sidebar.add_top_bar(header)

        self.list_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        loading = Adw.StatusPage(title="Checking GitHub…")
        loading.set_child(Adw.Spinner())
        self.list_stack.add_named(loading, "loading")
        empty = Adw.StatusPage(icon_name="emblem-ok-symbolic", title="All caught up",
                               description="No pull requests are waiting for your review.")
        self.list_stack.add_named(empty, "empty")
        self.error_page = Adw.StatusPage(icon_name="network-error-symbolic", title="Could not reach GitHub")
        retry = Gtk.Button(label="Try again", halign=Gtk.Align.CENTER, action_name="app.refresh")
        retry.add_css_class("pill")
        self.error_page.set_child(retry)
        self.list_stack.add_named(self.error_page, "error")

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.add_css_class("navigation-sidebar")
        self.list.connect("row-selected", self._on_row_selected)
        scroller.set_child(self.list)
        self.list_stack.add_named(scroller, "list")
        sidebar.set_content(self.list_stack)
        self.split.set_sidebar(Adw.NavigationPage(child=sidebar, title="Review requests"))

        # -- content --
        self.content_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        placeholder = Adw.ToolbarView()
        placeholder.add_top_bar(Adw.HeaderBar())
        placeholder.set_content(Adw.StatusPage(icon_name="dev.rewster.PRReview", title="Select a pull request",
                                               description="Pick a review request from the list to see its changes."))
        self.content_stack.add_named(placeholder, "placeholder")
        self.detail = PRDetailView(app.client, app.ai)
        self.detail.connect("toast", lambda _d, msg: self.show_toast(msg))
        self.detail.connect("reviewed", self._on_reviewed)
        self.content_stack.add_named(self.detail, "detail")
        self.split.set_content(Adw.NavigationPage(child=self.content_stack, title="Pull request"))

        # Window-level actions.
        open_github = Gio.SimpleAction.new("open-github", None)
        open_github.connect("activate", lambda *_: Gtk.UriLauncher.new(
            "https://github.com/pulls/review-requested").launch(self, None, None))
        self.add_action(open_github)

        # Collapse to a single pane on narrow windows (half a 1280px screen, for example).
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 1100sp"))
        narrow.add_setter(self.split, "collapsed", True)
        narrow.add_setter(self.detail.actions_box, "visible", False)
        narrow.add_setter(self.detail.review_menu_btn, "visible", True)
        self.add_breakpoint(narrow)

        self.connect("close-request", self._on_close_request)
        app.poller.connect("updated", self._on_updated)
        app.poller.connect("polling", lambda *_: self.refresh_btn.set_sensitive(False))
        GLib.timeout_add_seconds(30, self._tick_times)

        if app.poller.last_update is not None:
            self._on_updated(app.poller, app.poller.prs, app.poller.error)

    # -- helpers ----------------------------------------------------------

    def show_toast(self, message: str) -> None:
        self.toast.add_toast(Adw.Toast(title=message, timeout=4))

    def selected_key(self) -> str | None:
        row = self.list.get_selected_row()
        return row.pr.key if isinstance(row, PRRow) else None

    def show_pr(self, key: str) -> bool:
        for row in self.list:
            if isinstance(row, PRRow) and row.pr.key == key:
                log.debug("show_pr %s: selecting row", key)
                self.list.select_row(row)
                self.split.set_show_content(True)
                return True
        log.debug("show_pr %s: not in list (%d rows)", key, len(list(self.list)))
        self.show_toast(f"{key} is no longer waiting for your review")
        return False

    # -- signal handlers --------------------------------------------------

    def _on_updated(self, _poller, prs: list[PullRequest], error: str) -> None:
        self.refresh_btn.set_sensitive(True)
        when = self.app.poller.last_update
        self.sidebar_title.set_subtitle(f"Updated {humanize(when)}" if when else "")
        if error and not prs:
            self.error_page.set_description(error)
            self.list_stack.set_visible_child_name("error")
            return
        if error:
            self.show_toast(error)

        previous = self.selected_key()
        log.debug("updated: %d prs, error=%r, previously selected %s", len(prs), error, previous)
        self.list.remove_all()
        for pr in prs:
            self.list.append(PRRow(pr))
        if not prs:
            self.list_stack.set_visible_child_name("empty")
            self.content_stack.set_visible_child_name("placeholder")
            return
        self.list_stack.set_visible_child_name("list")

        restored = False
        if previous:
            for row in self.list:
                if row.pr.key == previous:
                    self.list.select_row(row)
                    restored = True
                    break
        if not restored:
            log.debug("updated: selection not restored, showing placeholder")
            self.content_stack.set_visible_child_name("placeholder")

    def _on_row_selected(self, _list, row: PRRow | None) -> None:
        log.debug("row selected: %s", row.pr.key if row else None)
        if row is None:
            return
        if self.detail.pr is None or self.detail.pr.key != row.pr.key:
            self.detail.show(row.pr)
            self.detail.populate_picker()
        self.content_stack.set_visible_child_name("detail")

    def _on_reviewed(self, _detail, key: str, event: str) -> None:
        self.app.poller.refresh()

    def _on_close_request(self, *_args) -> bool:
        # Keep polling in the background; the waybar module or `pr-review` brings it back.
        self.set_visible(False)
        return True

    def _tick_times(self) -> bool:
        for row in self.list:
            if isinstance(row, PRRow):
                row.refresh_time()
        when = self.app.poller.last_update
        if when:
            self.sidebar_title.set_subtitle(f"Updated {humanize(when)}")
        return True
