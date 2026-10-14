"""Per-file diff viewer: language-highlighted code with a diff gutter."""

from __future__ import annotations

import re

from gi.repository import Adw, Gtk, GtkSource, Pango

from .github import ChangedFile

LIGHT = {"add": "#dafbe1", "del": "#ffebe9", "hunk": "#ddf4ff", "meta": "#f6f8fa"}
DARK = {
    "add": "rgba(46,160,67,0.20)",
    "del": "rgba(248,81,73,0.20)",
    "hunk": "rgba(56,139,253,0.18)",
    "meta": "rgba(255,255,255,0.05)",
}
MARK_COLOR = {"add": ("#1a7f37", "#3fb950"), "del": ("#cf222e", "#f85149")}
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

STATUS_TEXT = {"added": "new file", "removed": "deleted file", "renamed": "renamed", "copied": "copied"}


class DiffGutter(GtkSource.GutterRendererText):
    """Old/new line numbers plus the +/- marker, rendered beside pure code."""

    def __init__(self, view: "DiffView"):
        super().__init__()
        self.diff = view
        self.set_xpad(8)
        self.set_xalign(1.0)
        self._sized = False

    def do_query_data(self, _lines: GtkSource.GutterLines, line: int) -> None:
        info = self.diff.line_info
        if line >= len(info):
            self.set_text("", -1)
            return
        kind, old, new = info[line]
        dark = self.diff.dark
        if kind in ("add", "del"):
            color = MARK_COLOR[kind][1 if dark else 0]
            mark = f"<span foreground='{color}' weight='bold'>{'+' if kind == 'add' else '−'}</span>"
        else:
            mark = " "
        o = str(old) if old is not None else ""
        n = str(new) if new is not None else ""
        self.set_markup(f"<span alpha='55%'>{o:>{self.diff.digits}} {n:>{self.diff.digits}}</span> {mark}", -1)

    def resize(self) -> None:
        width, _h = self.measure("0" * (2 * self.diff.digits + 3))
        self.set_size_request(width + 2 * self.get_xpad(), -1)


class DiffView(Gtk.ScrolledWindow):
    def __init__(self):
        super().__init__(hexpand=True, vexpand=True)
        self.buffer = GtkSource.Buffer()
        self.buffer.set_highlight_matching_brackets(False)
        self.view = GtkSource.View.new_with_buffer(self.buffer)
        self.view.set_editable(False)
        self.view.set_cursor_visible(False)
        self.view.set_monospace(True)
        self.view.set_show_line_numbers(False)
        self.view.set_highlight_current_line(False)
        self.view.set_wrap_mode(Gtk.WrapMode.NONE)
        self.view.set_left_margin(10)
        self.view.set_right_margin(12)
        self.view.set_top_margin(6)
        self.view.set_bottom_margin(48)
        self.view.set_tab_width(4)
        self.view.add_css_class("diff-view")
        self.set_child(self.view)

        self.line_info: list[tuple[str, int | None, int | None]] = []
        self.digits = 3
        self.dark = False
        self.gutter = DiffGutter(self)
        self.view.get_gutter(Gtk.TextWindowType.LEFT).insert(self.gutter, 0)

        self._tags = {name: self.buffer.create_tag(name) for name in ("add", "del", "hunk", "meta")}
        self._tags["meta"].set_property("style", Pango.Style.ITALIC)
        self._tags["hunk"].set_property("style", Pango.Style.ITALIC)
        self._manager = GtkSource.LanguageManager.get_default()

        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self._apply_theme())
        self._apply_theme()

    def _apply_theme(self) -> None:
        self.dark = Adw.StyleManager.get_default().get_dark()
        scheme = GtkSource.StyleSchemeManager.get_default().get_scheme("Adwaita-dark" if self.dark else "Adwaita")
        if scheme:
            self.buffer.set_style_scheme(scheme)
        colors = DARK if self.dark else LIGHT
        for name, tag in self._tags.items():
            tag.set_property("paragraph-background", colors[name])
        self.view.queue_draw()

    def clear(self) -> None:
        self.line_info = []
        self.buffer.set_language(None)
        self.buffer.set_text("")

    def show_file(self, f: ChangedFile) -> None:
        lang = self._manager.guess_language(f.filename, None)
        self.buffer.set_language(lang)

        lines: list[str] = []
        kinds: list[str | None] = []
        info: list[tuple[str, int | None, int | None]] = []
        meta = STATUS_TEXT.get(f.status)
        if f.status in ("renamed", "copied") and f.previous_filename:
            meta = f"{meta} from {f.previous_filename}"
        if meta:
            lines.append(meta)
            kinds.append("meta")
            info.append(("meta", None, None))
        if f.patch is None:
            lines.append("No text diff available: binary file, or the change is too large to show here.")
            kinds.append("meta")
            info.append(("meta", None, None))
        else:
            old = new = 0
            for raw in f.patch.split("\n"):
                m = HUNK_RE.match(raw)
                if m:
                    old, new = int(m.group(1)), int(m.group(3))
                    lines.append(raw)
                    kinds.append("hunk")
                    info.append(("hunk", None, None))
                elif raw.startswith("+"):
                    lines.append(raw[1:])
                    kinds.append("add")
                    info.append(("add", None, new))
                    new += 1
                elif raw.startswith("-"):
                    lines.append(raw[1:])
                    kinds.append("del")
                    info.append(("del", old, None))
                    old += 1
                elif raw.startswith("\\"):
                    lines.append(raw[2:])
                    kinds.append("meta")
                    info.append(("meta", None, None))
                else:
                    lines.append(raw[1:] if raw.startswith(" ") else raw)
                    kinds.append(None)
                    info.append(("ctx", old, new))
                    old += 1
                    new += 1
        self.line_info = info
        biggest = max([n for _k, o, n in info for n in (o, n) if n is not None] + [1])
        self.digits = max(2, len(str(biggest)))
        self.gutter.resize()
        self.buffer.set_text("\n".join(lines))

        i, n = 0, len(kinds)
        while i < n:
            kind = kinds[i]
            j = i
            while j + 1 < n and kinds[j + 1] == kind:
                j += 1
            if kind is not None:
                _ok, start = self.buffer.get_iter_at_line(i)
                _ok, end = self.buffer.get_iter_at_line(j)
                if not end.ends_line():
                    end.forward_to_line_end()
                self.buffer.apply_tag(self._tags[kind], start, end)
            i = j + 1
        self.get_vadjustment().set_value(0)
        self.get_hadjustment().set_value(0)
