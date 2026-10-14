"""Dialog for submitting a review (approve / request changes / comment) or a Claude prompt."""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, Gtk

KINDS = {
    "APPROVE": ("Approve", "Approve pull request", "Comment (optional)", "suggested-action"),
    "REQUEST_CHANGES": ("Request changes", "Request changes", "Explain what needs to change", "destructive-action"),
    "COMMENT": ("Comment", "Leave a review comment", "Comment", "suggested-action"),
    "PROMPT": ("Run", "Ask Claude about this pull request", "What should Claude do? It runs in a checkout of the PR branch.", "suggested-action"),
}


class ReviewDialog(Adw.Dialog):
    def __init__(self, event: str, pr_key: str, on_submit: Callable[[str, str], None], initial_text: str = ""):
        super().__init__()
        self.event = event
        self.on_submit = on_submit
        button_text, title, hint, style = KINDS[event]
        self.set_title(title)
        self.set_content_width(620 if initial_text else 560)
        self.set_content_height(520 if initial_text else 380)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        header.set_title_widget(Adw.WindowTitle(title=title, subtitle=pr_key))
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.submit = Gtk.Button(label=button_text)
        self.submit.add_css_class(style)
        self.submit.connect("clicked", self._submit)
        header.pack_end(self.submit)
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(12)
        label = Gtk.Label(label=hint, xalign=0, wrap=True)
        label.add_css_class("dim-label")
        box.append(label)

        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroller.add_css_class("card")
        self.text = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        for side in ("top", "bottom", "left", "right"):
            getattr(self.text, f"set_{side}_margin")(10)
        if initial_text:
            self.text.get_buffer().set_text(initial_text)
        self.text.get_buffer().connect("changed", lambda *_: self._update_submit())
        scroller.set_child(self.text)
        box.append(scroller)

        tip = Gtk.Label(label="Ctrl+Enter to submit", xalign=1)
        tip.add_css_class("caption")
        tip.add_css_class("dim-label")
        box.append(tip)

        toolbar.set_content(box)
        self.set_child(toolbar)
        self.set_focus(self.text)

        shortcuts = Gtk.ShortcutController()
        shortcuts.set_scope(Gtk.ShortcutScope.MANAGED)
        shortcuts.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control>Return"),
            Gtk.CallbackAction.new(lambda *_: self._submit() or True)))
        self.add_controller(shortcuts)
        self._update_submit()

    def body(self) -> str:
        buf = self.text.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)

    def _update_submit(self) -> None:
        self.submit.set_sensitive(self.event == "APPROVE" or bool(self.body().strip()))

    def _submit(self, *_args) -> None:
        if not self.submit.get_sensitive():
            return
        body = self.body()
        self.close()
        self.on_submit(self.event, body)
