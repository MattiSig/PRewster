"""Sidebar rows for pull requests."""

from __future__ import annotations

from gi.repository import Gtk, Pango

from .github import PullRequest
from .util import ci_badge, humanize, review_badge


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
        if pr.is_draft:
            badges.append(badge("Draft", "badge-muted"))
        text, css = ci_badge(pr.ci_state)
        badges.append(badge(text, css))
        rb = review_badge(pr.review_decision)
        if rb:
            badges.append(badge(*rb))
        if pr.mergeable == "CONFLICTING":
            badges.append(badge("Conflicts", "badge-err"))
        stats = Gtk.Label(label=f"+{pr.additions} −{pr.deletions}")
        stats.add_css_class("caption")
        stats.add_css_class("dim-label")
        stats.add_css_class("numeric")
        badges.append(stats)
        box.append(badges)

    def refresh_time(self) -> None:
        pr = self.pr
        self.subtitle.set_label(f"{pr.repo} #{pr.number}  ·  {pr.author}  ·  {humanize(pr.updated_at)}")
