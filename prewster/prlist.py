"""Sidebar lists: rows for pull requests and local branches, and the pane that holds them."""

from __future__ import annotations

import logging
from datetime import datetime

from gi.repository import Adw, GObject, Gtk, Pango

from .github import PullRequest
from .util import ci_badge, humanize, review_badge

log = logging.getLogger("prewster.list")


def badge(text: str, css: str) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.add_css_class("badge")
    label.add_css_class(css)
    return label


class PRRow(Gtk.ListBoxRow):
    def __init__(self, pr: PullRequest):
        super().__init__()
        self.pr = pr
        self.add_css_class("pr-row")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(6)
        box.set_margin_end(6)
        self.set_child(box)

        title = Gtk.Label(label=pr.title, xalign=0, wrap=True, lines=2,
                          ellipsize=Pango.EllipsizeMode.END, wrap_mode=Pango.WrapMode.WORD_CHAR)
        title.add_css_class("heading")
        box.append(title)

        self.subtitle = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.subtitle.add_css_class("caption")
        self.subtitle.add_css_class("dim-label")
        box.append(self.subtitle)
        self.refresh_time()

        badges = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        badges.set_margin_top(2)
        if pr.is_local:
            if pr.dirty:
                badges.append(badge("Uncommitted", "badge-warn"))
            if pr.ahead:
                badges.append(badge(f"{pr.ahead} commit{'s' if pr.ahead != 1 else ''}", "badge-info"))
        else:
            if pr.is_draft:
                badges.append(badge("Draft", "badge-muted"))
            text, css = ci_badge(pr.ci_state)
            badges.append(badge(text, css))
            rb = review_badge(pr.review_decision)
            if rb:
                badges.append(badge(*rb))
            if pr.mergeable == "CONFLICTING":
                badges.append(badge("Conflicts", "badge-err"))
        files = f" · {pr.changed_files} file{'s' if pr.changed_files != 1 else ''}" if pr.is_local else ""
        stats = Gtk.Label(label=f"+{pr.additions} −{pr.deletions}{files}")
        stats.add_css_class("caption")
        stats.add_css_class("dim-label")
        stats.add_css_class("numeric")
        badges.append(stats)
        box.append(badges)

    def refresh_time(self) -> None:
        pr = self.pr
        if pr.is_local:
            self.subtitle.set_label(f"{pr.repo}  ·  {pr.head_ref}  ·  {humanize(pr.updated_at)}")
            self.set_tooltip_text(pr.path)
        else:
            self.subtitle.set_label(f"{pr.repo} #{pr.number}  ·  {pr.author}  ·  {humanize(pr.updated_at)}")


class PRListPane(Gtk.Stack):
    """One sidebar list with its loading, empty and error states."""

    __gsignals__ = {
        # The selection changed: the PullRequest, or None when nothing is selected any more.
        "selected": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # A row was clicked or activated with the keyboard.
        "activated": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, empty_icon: str, empty_title: str, empty_description: str):
        super().__init__(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.loaded = False
        self.busy = False  # a load is running
        self._rebuilding = False
        self.updated: datetime | None = None
        loading = Adw.StatusPage(title="Checking…")
        loading.set_child(Adw.Spinner())
        self.add_named(loading, "loading")
        self.empty_page = Adw.StatusPage(icon_name=empty_icon, title=empty_title, description=empty_description)
        self.add_named(self.empty_page, "empty")
        self.error_page = Adw.StatusPage(icon_name="network-error-symbolic", title="Could not load the list")
        retry = Gtk.Button(label="Try again", halign=Gtk.Align.CENTER, action_name="app.refresh")
        retry.add_css_class("pill")
        self.error_page.set_child(retry)
        self.add_named(self.error_page, "error")

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.add_css_class("navigation-sidebar")
        self.list.connect("row-selected", self._on_row_selected)
        self.list.connect("row-activated", lambda *_: self.emit("activated"))
        scroller.set_child(self.list)
        self.add_named(scroller, "list")

    def selected(self) -> PullRequest | None:
        row = self.list.get_selected_row()
        return row.pr if isinstance(row, PRRow) else None

    def set_loading(self) -> None:
        if not self.loaded:
            self.set_visible_child_name("loading")

    def set_items(self, prs: list[PullRequest] | None, error: str, when: datetime | None = None) -> None:
        """Replace the rows, keeping the selection when that item is still listed.

        prs is None when the load failed; error is shown in place of the list if there is
        nothing to show, otherwise left to the caller to report.
        """
        self.loaded = True
        if prs is None:
            if not list(self.list):
                self.error_page.set_description(error)
                self.set_visible_child_name("error")
            return
        self.updated = when
        previous = self.selected()
        log.debug("%d items, previously selected %s", len(prs), previous.key if previous else None)
        # A focused ListBox selects the first row appended; that is not the user's choice.
        self._rebuilding = True
        self.list.remove_all()
        for pr in prs:
            self.list.append(PRRow(pr))
        self._rebuilding = False
        if not prs:
            self.set_visible_child_name("empty")
            self.emit("selected", None)
            return
        self.set_visible_child_name("list")
        if previous is None or not self.select_key(previous.key):
            self.list.unselect_all()
            self.emit("selected", None)

    def select_key(self, key: str) -> bool:
        for row in self.list:
            if isinstance(row, PRRow) and row.pr.key == key:
                if row.is_selected():
                    self.emit("selected", row.pr)  # select_row would not re-fire
                else:
                    self.list.select_row(row)
                return True
        return False

    def refresh_times(self) -> None:
        for row in self.list:
            if isinstance(row, PRRow):
                row.refresh_time()

    def _on_row_selected(self, _list, row: PRRow | None) -> None:
        if row is not None and not self._rebuilding:
            self.emit("selected", row.pr)
