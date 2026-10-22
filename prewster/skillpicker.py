"""Popover listing the Claude actions available for a pull request."""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, Gtk

from .ai import Skill

SCOPE_TITLES = {"builtin": "Built in", "custom": "Your actions", "user": "Your skills", "project": "Repository skills"}


class SkillPicker(Gtk.Popover):
    def __init__(self, on_pick: Callable[[Skill | None], None]):
        super().__init__()
        self.on_pick = on_pick
        self.set_size_request(380, -1)
        scroller = Gtk.ScrolledWindow(propagate_natural_height=True, max_content_height=520,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("navigation-sidebar")
        self.list.connect("row-activated", self._activated)
        scroller.set_child(self.list)
        self.set_child(scroller)

    def populate(self, skills: list[Skill]) -> None:
        self.list.remove_all()
        current = None
        for skill in skills:
            if skill.scope != current:
                current = skill.scope
                self.list.append(self._header(SCOPE_TITLES.get(current, current)))
            row = Adw.ActionRow(title=skill.name, subtitle=skill.description or skill.prompt, activatable=True)
            row.set_title_lines(1)
            row.set_subtitle_lines(2)
            row.skill = skill
            self.list.append(row)
        self.list.append(self._header("Chat"))
        row = Adw.ActionRow(title="Chat with Claude…", subtitle="Ask anything, follow up, let it edit when you allow it",
                            activatable=True)
        row.skill = None
        self.list.append(row)

    def _header(self, text: str) -> Gtk.ListBoxRow:
        label = Gtk.Label(label=text, xalign=0)
        label.add_css_class("caption-heading")
        label.add_css_class("dim-label")
        label.set_margin_top(8)
        label.set_margin_bottom(2)
        label.set_margin_start(12)
        row = Gtk.ListBoxRow(activatable=False, selectable=False, child=label)
        return row

    def _activated(self, _list, row) -> None:
        self.popdown()
        self.on_pick(getattr(row, "skill", None))
