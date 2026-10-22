"""Panel under the diff: a streaming Claude conversation you can follow up on."""

from __future__ import annotations

from gi.repository import Adw, GObject, Gtk, GtkSource, Pango

from .ai import ClaudeRun
from .github import PullRequest

USER_BG = {False: "rgba(53,132,228,0.10)", True: "rgba(120,174,237,0.14)"}  # by dark mode


class AIPanel(Gtk.Box):
    __gsignals__ = {
        "post-comment": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "close": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # A message to Claude: (text, allow edits). Continues the shown chat, or starts one.
        "send": (GObject.SignalFlags.RUN_FIRST, None, (str, bool)),
        "new-chat": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "open-terminal": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("ai-panel")
        self.run: ClaudeRun | None = None
        self.pr: PullRequest | None = None
        self._handlers: list[int] = []

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.add_css_class("toolbar")
        header.set_margin_start(6)
        header.set_margin_end(6)
        icon = Gtk.Image.new_from_icon_name("starred-symbolic")
        header.append(icon)
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.title.add_css_class("heading")
        header.append(self.title)
        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, hexpand=True)
        self.status.add_css_class("dim-label")
        self.status.add_css_class("caption")
        header.append(self.status)
        self.spinner = Adw.Spinner()
        self.spinner.set_visible(False)
        header.append(self.spinner)
        self.stop_btn = Gtk.Button(label="Stop")
        self.stop_btn.add_css_class("flat")
        self.stop_btn.connect("clicked", lambda *_: self.run and self.run.cancel())
        header.append(self.stop_btn)
        self.post_btn = Gtk.Button(label="Post as comment", tooltip_text="Post Claude's latest answer on the pull request")
        self.post_btn.add_css_class("flat")
        self.post_btn.connect("clicked", lambda *_: self.emit("post-comment", self.text()))
        header.append(self.post_btn)
        self.terminal_btn = Gtk.Button(icon_name="utilities-terminal-symbolic",
                                       tooltip_text="Continue this chat in interactive Claude Code")
        self.terminal_btn.add_css_class("flat")
        self.terminal_btn.connect("clicked", lambda *_: self.emit("open-terminal"))
        header.append(self.terminal_btn)
        self.new_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="New chat")
        self.new_btn.add_css_class("flat")
        self.new_btn.connect("clicked", lambda *_: self.emit("new-chat"))
        header.append(self.new_btn)
        close = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Hide panel")
        close.add_css_class("flat")
        close.connect("clicked", lambda *_: self.emit("close"))
        header.append(close)
        self.append(header)
        self.append(Gtk.Separator())

        lang = GtkSource.LanguageManager.get_default().get_language("markdown")
        self.buffer = GtkSource.Buffer.new_with_language(lang)
        self.buffer.set_highlight_matching_brackets(False)
        self.view = GtkSource.View.new_with_buffer(self.buffer)
        self.view.set_editable(False)
        self.view.set_cursor_visible(False)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.set_monospace(True)
        self.view.add_css_class("diff-view")
        for side in ("left", "right", "top"):
            getattr(self.view, f"set_{side}_margin")(12)
        self.view.set_bottom_margin(24)
        self.scroller = Gtk.ScrolledWindow(vexpand=True)
        self.scroller.set_child(self.view)
        self.append(self.scroller)
        self.append(Gtk.Separator())

        compose = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for side in ("top", "bottom", "start", "end"):
            getattr(compose, f"set_margin_{side}")(6)
        self.entry = Gtk.Entry(hexpand=True)
        self.entry.connect("activate", self._send)
        self.entry.connect("changed", lambda *_: self._update_send())
        compose.append(self.entry)
        self.edits_btn = Gtk.ToggleButton(icon_name="document-edit-symbolic",
                                          tooltip_text="Allow edits: Claude may change files in the checkout "
                                                       "(a scratch worktree for other people's pull requests)")
        self.edits_btn.add_css_class("flat")
        compose.append(self.edits_btn)
        self.send_btn = Gtk.Button(icon_name="mail-send-symbolic", tooltip_text="Send (Enter)")
        self.send_btn.connect("clicked", self._send)
        compose.append(self.send_btn)
        self.append(compose)

        self._activity_tag = self.buffer.create_tag("activity", style=Pango.Style.ITALIC, scale=0.9,
                                                    foreground="rgba(128,128,128,0.95)")
        self._user_tag = self.buffer.create_tag("user", weight=Pango.Weight.BOLD, pixels_above_lines=4,
                                                pixels_below_lines=4)
        self._end_mark = self.buffer.create_mark(None, self.buffer.get_end_iter(), False)
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self._apply_theme())
        self._apply_theme()
        self._set_running(False)

    # -- helpers ----------------------------------------------------------

    def _apply_theme(self) -> None:
        dark = Adw.StyleManager.get_default().get_dark()
        scheme = GtkSource.StyleSchemeManager.get_default().get_scheme("Adwaita-dark" if dark else "Adwaita")
        if scheme:
            self.buffer.set_style_scheme(scheme)
        self._user_tag.set_property("paragraph-background", USER_BG[dark])

    def text(self) -> str:
        """Claude's latest answer: what Post as comment posts."""
        if self.run:
            return self.run.result_text
        return ""

    def _set_running(self, running: bool) -> None:
        local = self.pr is not None and self.pr.is_local
        resumable = self.run is not None and self.run.session_started
        self.spinner.set_visible(running)
        self.stop_btn.set_visible(running)
        self.post_btn.set_visible(not running and not local and bool(self.text()))
        self.terminal_btn.set_visible(not running and resumable)
        self.new_btn.set_visible(not running and self.run is not None)
        self.entry.set_sensitive(not running)
        if running:
            placeholder = "Claude is working…"
        elif resumable:
            placeholder = "Ask a follow-up…"
        else:
            placeholder = f"Ask Claude about this {'branch' if local else 'pull request'}…"
        self.entry.set_placeholder_text(placeholder)
        self._update_send()

    def _update_send(self) -> None:
        self.send_btn.set_sensitive(self.entry.get_sensitive() and bool(self.entry.get_text().strip()))

    def _send(self, *_args) -> None:
        text = self.entry.get_text().strip()
        if not text or not self.entry.get_sensitive():
            return
        self.entry.set_text("")
        self.emit("send", text, self.edits_btn.get_active())

    def _append(self, text: str, tag=None) -> None:
        adj = self.scroller.get_vadjustment()
        at_bottom = adj.get_value() + adj.get_page_size() >= adj.get_upper() - 40
        end = self.buffer.get_end_iter()
        if tag:
            self.buffer.insert_with_tags(end, text, tag)
        else:
            self.buffer.insert(end, text)
        if at_bottom:
            self.view.scroll_to_mark(self._end_mark, 0.0, False, 0.0, 1.0)

    def _append_activity(self, line: str) -> None:
        end = self.buffer.get_end_iter()
        prefix = "" if end.starts_line() else "\n"
        self._append(f"{prefix}▸ {line}\n", self._activity_tag)

    def _append_text(self, text: str) -> None:
        self._append(text)

    def _append_user(self, text: str) -> None:
        end = self.buffer.get_end_iter()
        if self.buffer.get_char_count():
            self._append("\n" if end.starts_line() else "\n\n")
        self._append(f"You: {text}\n", self._user_tag)
        self._append("\n")
        self.view.scroll_to_mark(self._end_mark, 0.0, False, 0.0, 1.0)

    def _replay(self, kind: str, text: str) -> None:
        {"text": self._append_text, "activity": self._append_activity, "user": self._append_user}[kind](text)

    # -- public -----------------------------------------------------------

    def detach(self) -> None:
        if self.run:
            for hid in self._handlers:
                self.run.disconnect(hid)
        self._handlers = []
        self.run = None

    def _show_pr(self, pr: PullRequest) -> None:
        if self.pr is None or self.pr.key != pr.key:
            self.edits_btn.set_active(False)  # edits are allowed per pull request, never carried over
        self.pr = pr

    def attach(self, run: ClaudeRun) -> None:
        """Show a conversation: replay what it produced so far, then follow it live."""
        self.detach()
        self._show_pr(run.pr)
        self.run = run
        self.buffer.set_text("")
        self.title.set_label(f"{run.skill.name} · {run.pr.key}")
        for kind, text in run.transcript:
            self._replay(kind, text)
        self._handlers = [
            run.connect("text", lambda _r, t: self._append_text(t)),
            run.connect("activity", lambda _r, t: self._append_activity(t)),
            run.connect("user", lambda _r, t: self._started(t)),
            run.connect("finished", lambda _r, ok, s: self._finished(ok, s)),
        ]
        self.status.set_label("Running…" if run.state == "running" else run.summary)
        self._set_running(run.state == "running")

    def new_chat(self, pr: PullRequest, focus: bool = True) -> None:
        """An empty conversation; the first message sent starts it."""
        self.detach()
        self._show_pr(pr)
        self.buffer.set_text("")
        self.title.set_label(f"Chat · {pr.key}")
        where = "your checkout" if pr.is_local else "a scratch checkout of the pull request"
        self.status.set_label(f"Claude reads and runs in {where}")
        self._set_running(False)
        if focus:
            self.entry.grab_focus()

    def _started(self, message: str) -> None:
        self._append_user(message)
        self.status.set_label("Running…")
        self._set_running(True)

    def _finished(self, ok: bool, summary: str) -> None:
        self.status.set_label(summary)
        if not ok:
            self._append_activity(summary)
        self._set_running(False)
