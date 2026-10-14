"""Panel that shows a streaming Claude run under the diff."""

from __future__ import annotations

from gi.repository import Adw, GObject, Gtk, GtkSource, Pango

from .ai import ClaudeRun


class AIPanel(Gtk.Box):
    __gsignals__ = {
        "post-comment": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "close": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("ai-panel")
        self.run: ClaudeRun | None = None
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
        self.post_btn = Gtk.Button(label="Post as comment")
        self.post_btn.add_css_class("flat")
        self.post_btn.connect("clicked", lambda *_: self.emit("post-comment", self.text()))
        header.append(self.post_btn)
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

        self._activity_tag = self.buffer.create_tag("activity", style=Pango.Style.ITALIC, scale=0.9,
                                                    foreground="rgba(128,128,128,0.95)")
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

    def text(self) -> str:
        if self.run:
            return self.run.result_text
        return self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), True).strip()

    def _set_running(self, running: bool) -> None:
        self.spinner.set_visible(running)
        self.stop_btn.set_visible(running)
        self.post_btn.set_visible(not running and bool(self.text()))

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

    # -- public -----------------------------------------------------------

    def detach(self) -> None:
        if self.run:
            for hid in self._handlers:
                self.run.disconnect(hid)
        self._handlers = []
        self.run = None

    def attach(self, run: ClaudeRun) -> None:
        """Show a run: replay what it produced so far, then follow it live."""
        self.detach()
        self.run = run
        self.buffer.set_text("")
        self.title.set_label(f"{run.skill.name} · {run.pr.key}")
        for kind, text in run.transcript:
            (self._append_text if kind == "text" else self._append_activity)(text)
        if run.state == "running":
            self.status.set_label("Running…")
            self._set_running(True)
            self._handlers = [
                run.connect("text", lambda _r, t: self._append_text(t)),
                run.connect("activity", lambda _r, t: self._append_activity(t)),
                run.connect("finished", lambda _r, ok, s: self._finished(ok, s)),
            ]
        else:
            self.status.set_label(run.summary)
            self._set_running(False)

    def _finished(self, ok: bool, summary: str) -> None:
        self.status.set_label(summary)
        if not ok:
            self._append_activity(summary)
        self._set_running(False)

    def show_saved(self, title: str, when: str, body: str) -> None:
        self.detach()
        self.buffer.set_text(body)
        self.title.set_label(title)
        self.status.set_label(f"Saved run from {when.replace('T', ' ')}" if when else "Saved run")
        self._set_running(False)
