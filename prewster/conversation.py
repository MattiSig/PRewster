"""Conversation tab: the description, comments, reviews and review threads of a pull request."""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, GObject, Gtk, Pango

from .github import Comment, Conversation, PullRequest, ReviewThread
from .util import AvatarLoader, esc, humanize

REVIEW_VERBS = {
    "APPROVED": ("approved these changes", "Approved", "badge-ok"),
    "CHANGES_REQUESTED": ("requested changes", "Changes requested", "badge-err"),
    "DISMISSED": ("left a review, since dismissed", "Dismissed", "badge-muted"),
    "COMMENTED": ("reviewed", "", ""),
}


def _badge(text: str, css: str) -> Gtk.Label:
    label = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
    label.add_css_class("badge")
    label.add_css_class(css)
    return label


def _header(avatars: AvatarLoader, author: str, avatar_url: str, verb: str, when, badges=()) -> Gtk.Box:
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    avatar = Adw.Avatar(size=22, text=author, show_initials=True, valign=Gtk.Align.CENTER)
    avatars.apply(avatar, avatar_url)
    row.append(avatar)
    label = Gtk.Label(xalign=0, use_markup=True, ellipsize=Pango.EllipsizeMode.END, hexpand=True)
    label.set_markup(f"<b>{esc(author)}</b> {esc(verb)}  <span alpha='60%'>· {esc(humanize(when))}</span>")
    label.set_tooltip_text(when.astimezone().strftime("%Y-%m-%d %H:%M"))
    row.append(label)
    for badge in badges:
        row.append(badge)
    return row


def _body(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0, wrap=True, selectable=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                      max_width_chars=100)
    label.set_margin_start(30)
    return label


def _card() -> Gtk.Box:
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    card.add_css_class("card")
    card.add_css_class("comment-card")
    return card


def comment_card(avatars: AvatarLoader, c: Comment) -> Gtk.Widget:
    card = _card()
    verb, badge_text, css = REVIEW_VERBS.get(c.state, ("commented", "", ""))
    card.append(_header(avatars, c.author, c.avatar, verb, c.created_at,
                        [_badge(badge_text, css)] if badge_text else []))
    if c.body:
        card.append(_body(c.body))
    return card


def thread_card(avatars: AvatarLoader, thread: ReviewThread,
                on_jump: Callable[[ReviewThread], None] | None = None) -> Gtk.Widget:
    """A review thread. With on_jump it is headed by a file:line link (Conversation tab);
    without, it is the compact form shown inside the diff, folded away once resolved."""
    card = _card()
    card.add_css_class("thread-card")
    badges = []
    if thread.outdated:
        badges.append(_badge("Outdated", "badge-warn"))
    if thread.resolved:
        badges.append(_badge("Resolved", "badge-ok"))
    if on_jump is not None:
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        line = thread.line or thread.original_line
        where = f"{thread.path}:{line}" if line else thread.path
        link = Gtk.Button(halign=Gtk.Align.START, hexpand=True, tooltip_text="Show in the diff")
        link.set_child(Gtk.Label(label=where, ellipsize=Pango.EllipsizeMode.START))
        link.add_css_class("flat")
        link.add_css_class("thread-link")
        link.connect("clicked", lambda *_: on_jump(thread))
        link.set_sensitive(not thread.outdated)
        top.append(link)
        for badge in badges:
            top.append(badge)
        card.append(top)
        hunk = [l for l in thread.diff_hunk.split("\n") if l and not l.startswith("@@")][-4:]
        if hunk:
            code = Gtk.Label(label="\n".join(hunk), xalign=0, selectable=True, ellipsize=Pango.EllipsizeMode.END)
            code.add_css_class("monospace")
            code.add_css_class("thread-hunk")
            card.append(code)
    for i, c in enumerate(thread.comments):
        card.append(_header(avatars, c.author, c.avatar, "commented" if i == 0 else "replied", c.created_at,
                            badges if on_jump is None and i == 0 else []))
        if c.body:
            card.append(_body(c.body))
    if on_jump is None and thread.resolved:
        n = len(thread.comments)
        expander = Gtk.Expander(label=f"Resolved thread · {n} comment{'s' if n != 1 else ''}")
        expander.add_css_class("thread-card")
        expander.set_child(card)
        return expander
    return card


class ConversationView(Gtk.ScrolledWindow):
    __gsignals__ = {
        # A thread's file:line link was clicked: (path, side, line)
        "jump": (GObject.SignalFlags.RUN_FIRST, None, (str, str, int)),
    }

    def __init__(self, avatars: AvatarLoader):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.avatars = avatars
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(self.box, f"set_margin_{side}")(14)
        clamp = Adw.Clamp(maximum_size=920, tightening_threshold=600, child=self.box)
        self.set_child(clamp)

    @staticmethod
    def count(conversation: Conversation | None) -> int:
        if conversation is None:
            return 0
        return len(conversation.comments) + sum(len(t.comments) for t in conversation.threads)

    def show(self, pr: PullRequest, conversation: Conversation | None, error: str = "") -> None:
        """Description first, then everything else oldest first. conversation None means loading."""
        while child := self.box.get_first_child():
            self.box.remove(child)
        card = _card()
        verb = "worked on this branch" if pr.is_local else "opened this pull request"
        card.append(_header(self.avatars, pr.author, pr.author_avatar, verb, pr.created_at))
        card.append(_body(pr.body.strip() or "No description provided."))
        self.box.append(card)

        if pr.is_local:
            return
        if conversation is None:
            if error:
                note = Gtk.Label(label=f"Could not load comments: {error}", xalign=0, wrap=True)
                note.add_css_class("error")
            else:
                note = Adw.Spinner(halign=Gtk.Align.CENTER)
            self.box.append(note)
            return
        events: list[tuple] = [(c.created_at, 0, comment_card(self.avatars, c)) for c in conversation.comments]
        events += [(t.created_at, 1, thread_card(self.avatars, t, self._jump)) for t in conversation.threads]
        events.sort(key=lambda e: (e[0], e[1]))
        for _when, _order, widget in events:
            self.box.append(widget)
        if not events:
            empty = Gtk.Label(label="No comments yet.", xalign=0)
            empty.add_css_class("dim-label")
            self.box.append(empty)
        self.get_vadjustment().set_value(0)

    def _jump(self, thread: ReviewThread) -> None:
        if thread.line is not None:
            self.emit("jump", thread.path, thread.side, thread.line)
