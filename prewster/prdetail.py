"""Detail pane: PR metadata, changed-file list, diff, review actions, Claude runs."""

from __future__ import annotations

import logging
import threading

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from .ai import CLAUDE_MISSING, AIService, Skill, load_last_run
from .aipanel import AIPanel
from .diffview import DiffView
from .github import ChangedFile, GitHubClient, GitHubError, PullRequest
from .poller import on_main
from .reviewdialog import ReviewDialog
from .skillpicker import SkillPicker
from .util import ci_badge, esc, humanize, review_badge, text_color_for

STATUS_LETTER = {
    "added": ("A", "badge-ok"),
    "removed": ("D", "badge-err"),
    "renamed": ("R", "badge-info"),
    "copied": ("C", "badge-info"),
    "modified": ("M", "badge-warn"),
    "changed": ("M", "badge-warn"),
    "unchanged": ("·", "badge-muted"),
}

_avatar_cache: dict[str, Gdk.Texture] = {}
log = logging.getLogger("prewster.detail")


def _badge(text: str, css: str) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.add_css_class("badge")
    label.add_css_class(css)
    return label


class FileRow(Gtk.ListBoxRow):
    def __init__(self, index: int, f: ChangedFile):
        super().__init__()
        self.index = index
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)
        letter, css = STATUS_LETTER.get(f.status, ("M", "badge-warn"))
        status = _badge(letter, css)
        status.add_css_class("file-status")
        status.set_valign(Gtk.Align.CENTER)
        box.append(status)

        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        head, _, tail = f.filename.rpartition("/")
        name = Gtk.Label(label=tail or f.filename, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        names.append(name)
        if head:
            folder = Gtk.Label(label=head, xalign=0, ellipsize=Pango.EllipsizeMode.START)
            folder.add_css_class("caption")
            folder.add_css_class("dim-label")
            names.append(folder)
        box.append(names)

        stats = Gtk.Label(label=f"+{f.additions} −{f.deletions}")
        stats.add_css_class("caption")
        stats.add_css_class("dim-label")
        stats.add_css_class("numeric")
        stats.set_valign(Gtk.Align.CENTER)
        box.append(stats)
        self.set_child(box)
        self.set_tooltip_text(f.filename)


class PRDetailView(Gtk.Box):
    __gsignals__ = {
        # A review was submitted successfully: (pr key, event)
        "reviewed": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        # Something to show in a toast.
        "toast": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, client: GitHubClient, ai: AIService):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.client = client
        self.ai = ai
        self.pr: PullRequest | None = None
        self._load_token = 0
        toolbar = Adw.ToolbarView(vexpand=True)
        self.append(toolbar)

        # Header bar with actions.
        header = Adw.HeaderBar()
        self.title = Adw.WindowTitle()
        header.set_title_widget(self.title)

        browser = Gtk.Button(icon_name="external-link-symbolic", tooltip_text="Open on GitHub")
        browser.connect("clicked", lambda *_: self._open_in_browser())
        header.pack_end(browser)

        self.actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.approve_btn = Gtk.Button(label="Approve")
        self.approve_btn.add_css_class("suggested-action")
        self.approve_btn.connect("clicked", lambda *_: self._review("APPROVE"))
        changes_btn = Gtk.Button(label="Request changes")
        changes_btn.connect("clicked", lambda *_: self._review("REQUEST_CHANGES"))
        comment_btn = Gtk.Button(label="Comment")
        comment_btn.connect("clicked", lambda *_: self._review("COMMENT"))
        self.actions_box.append(comment_btn)
        self.actions_box.append(changes_btn)
        self.actions_box.append(self.approve_btn)
        header.pack_end(self.actions_box)

        # Compact replacement for the three buttons on narrow windows.
        review_menu = Gio.Menu()
        review_menu.append("Approve", "detail.review::APPROVE")
        review_menu.append("Request changes", "detail.review::REQUEST_CHANGES")
        review_menu.append("Comment", "detail.review::COMMENT")
        self.review_menu_btn = Gtk.MenuButton(menu_model=review_menu, always_show_arrow=True, visible=False)
        self.review_menu_btn.set_child(Adw.ButtonContent(icon_name="emblem-ok-symbolic", label="Review"))
        self.review_menu_btn.add_css_class("suggested-action")
        header.pack_end(self.review_menu_btn)
        group = Gio.SimpleActionGroup()
        review_action = Gio.SimpleAction.new("review", GLib.VariantType.new("s"))
        review_action.connect("activate", lambda _a, p: self._review(p.get_string()))
        group.add_action(review_action)
        self.insert_action_group("detail", group)

        self.picker = SkillPicker(self._on_skill_picked)
        self.claude_btn = Gtk.MenuButton(popover=self.picker, always_show_arrow=True)
        claude_content = Adw.ButtonContent(icon_name="starred-symbolic", label="Claude")
        self.claude_btn.set_child(claude_content)
        if not ai.available:
            self.claude_btn.set_sensitive(False)
            self.claude_btn.set_tooltip_text(CLAUDE_MISSING)
        else:
            self.claude_btn.set_tooltip_text("Run a Claude Code skill on this pull request")
        header.pack_start(self.claude_btn)
        toolbar.add_top_bar(header)

        # Metadata block.
        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        meta.set_margin_top(10)
        meta.set_margin_bottom(10)
        meta.set_margin_start(14)
        meta.set_margin_end(14)

        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.avatar = Adw.Avatar(size=28, show_initials=True)
        self.avatar.set_valign(Gtk.Align.CENTER)
        row1.append(self.avatar)
        self.branch_label = Gtk.Label(xalign=0, use_markup=True, ellipsize=Pango.EllipsizeMode.MIDDLE, hexpand=True)
        row1.append(self.branch_label)
        self.badges = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.badges.set_valign(Gtk.Align.CENTER)
        row1.append(self.badges)
        meta.append(row1)

        self.labels_box = Adw.WrapBox(child_spacing=6, line_spacing=6)
        meta.append(self.labels_box)

        self.body_expander = Gtk.Expander(label="Description")
        body_scroller = Gtk.ScrolledWindow(max_content_height=260, propagate_natural_height=True)
        self.body_label = Gtk.Label(xalign=0, wrap=True, selectable=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        self.body_label.set_margin_top(6)
        self.body_label.set_margin_bottom(6)
        body_scroller.set_child(self.body_label)
        self.body_expander.set_child(body_scroller)
        meta.append(self.body_expander)

        # Files + diff.
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=300, vexpand=True)
        self.paned.set_shrink_start_child(False)
        self.paned.set_shrink_end_child(False)

        files_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        files_top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        files_top.set_margin_top(4)
        files_top.set_margin_start(12)
        files_top.set_margin_end(4)
        self.files_header = Gtk.Label(xalign=0, hexpand=True)
        self.files_header.add_css_class("caption-heading")
        self.files_header.add_css_class("dim-label")
        files_top.append(self.files_header)
        prev_btn = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Previous file (Alt+Up)")
        prev_btn.add_css_class("flat")
        prev_btn.connect("clicked", lambda *_: self._step_file(-1))
        next_btn = Gtk.Button(icon_name="go-down-symbolic", tooltip_text="Next file (Alt+Down)")
        next_btn.add_css_class("flat")
        next_btn.connect("clicked", lambda *_: self._step_file(1))
        files_top.append(prev_btn)
        files_top.append(next_btn)
        files_box.append(files_top)
        files_scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.files_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.files_list.add_css_class("navigation-sidebar")
        self.files_list.connect("row-selected", self._on_file_selected)
        files_scroller.set_child(self.files_list)
        files_box.append(files_scroller)
        files_box.set_size_request(220, -1)
        self.paned.set_start_child(files_box)

        self.diff = DiffView()
        self.paned.set_end_child(self.diff)

        self.stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        loading = Adw.StatusPage(title="Loading changes…")
        loading.set_child(Adw.Spinner())
        self.stack.add_named(loading, "loading")
        self.stack.add_named(self.paned, "diff")
        self.error_page = Adw.StatusPage(icon_name="dialog-error-symbolic", title="Could not load changes")
        retry = Gtk.Button(label="Retry", halign=Gtk.Align.CENTER)
        retry.add_css_class("pill")
        retry.connect("clicked", lambda *_: self.pr and self._load_files(self.pr))
        self.error_page.set_child(retry)
        self.stack.add_named(self.error_page, "error")

        # Claude output panel below the diff.
        self.ai_panel = AIPanel()
        self.ai_panel.set_visible(False)
        self.ai_panel.connect("close", lambda *_: self.ai_panel.set_visible(False))
        self.ai_panel.connect("post-comment", self._post_ai_comment)
        self.vpaned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        self.vpaned.set_start_child(self.stack)
        self.vpaned.set_end_child(self.ai_panel)
        self.vpaned.set_shrink_start_child(False)
        self.vpaned.set_shrink_end_child(False)
        self.vpaned.set_resize_end_child(False)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(meta)
        content.append(Gtk.Separator())
        content.append(self.vpaned)
        toolbar.set_content(content)

        shortcuts = Gtk.ShortcutController()
        shortcuts.set_scope(Gtk.ShortcutScope.MANAGED)
        for trigger, delta in (("<Alt>Down", 1), ("<Alt>Up", -1)):
            shortcuts.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(trigger),
                Gtk.CallbackAction.new(lambda *_, d=delta: self._step_file(d) or True)))
        self.add_controller(shortcuts)
        self.files: list[ChangedFile] = []

    # -- public -----------------------------------------------------------

    def show(self, pr: PullRequest) -> None:
        self.pr = pr
        self.title.set_title(pr.title)
        self.title.set_subtitle(f"{pr.repo} #{pr.number}")
        self.branch_label.set_markup(
            f"<b>{esc(pr.author)}</b> wants to merge <tt>{esc(pr.head_ref)}</tt> into <tt>{esc(pr.base_ref)}</tt>"
            f"  <span alpha='60%'>· opened {esc(humanize(pr.created_at))}</span>")
        self.avatar.set_text(pr.author)
        self.avatar.set_custom_image(None)
        self._load_avatar(pr.author_avatar)

        for child in list(self.badges):
            self.badges.remove(child)
        if pr.is_draft:
            self.badges.append(_badge("Draft", "badge-muted"))
        self.badges.append(_badge(*ci_badge(pr.ci_state)))
        rb = review_badge(pr.review_decision)
        if rb:
            self.badges.append(_badge(*rb))
        if pr.mergeable == "CONFLICTING":
            self.badges.append(_badge("Merge conflicts", "badge-err"))
        stats = Gtk.Label(label=f"+{pr.additions} −{pr.deletions} · {pr.changed_files} files")
        stats.add_css_class("caption")
        stats.add_css_class("dim-label")
        stats.add_css_class("numeric")
        self.badges.append(stats)

        for child in list(self.labels_box):
            self.labels_box.remove(child)
        for name, color in pr.labels:
            lab = Gtk.Label(use_markup=True)
            lab.set_markup(f"<span bgcolor='#{esc(color)}' fgcolor='{text_color_for(color)}'> {esc(name)} </span>")
            lab.add_css_class("caption")
            self.labels_box.append(lab)
        self.labels_box.set_visible(bool(pr.labels))

        body = pr.body.strip()
        self.body_label.set_label(body or "No description provided.")
        self.body_expander.set_expanded(False)
        self.approve_btn.set_sensitive(not pr.is_draft)
        self.approve_btn.set_tooltip_text("Draft pull requests cannot be approved" if pr.is_draft else None)
        self._load_files(pr)
        self._show_ai_state(pr)

    # -- files ------------------------------------------------------------

    def _load_files(self, pr: PullRequest) -> None:
        self._load_token += 1
        token = self._load_token
        self.stack.set_visible_child_name("loading")
        self.files = []
        self.files_list.remove_all()
        self.diff.clear()

        def work():
            try:
                files = self.client.list_files(pr.repo, pr.number)
                on_main(self._files_loaded, token, files, "")
            except GitHubError as exc:
                on_main(self._files_loaded, token, None, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _files_loaded(self, token: int, files: list[ChangedFile] | None, error: str) -> None:
        if token != self._load_token:
            return
        if files is None:
            self.error_page.set_description(error)
            self.stack.set_visible_child_name("error")
            return
        self.files = files
        self.files_header.set_label(f"{len(files)} changed file{'s' if len(files) != 1 else ''}")
        for i, f in enumerate(files):
            self.files_list.append(FileRow(i, f))
        self.stack.set_visible_child_name("diff")
        first = self.files_list.get_row_at_index(0)
        if first is not None:
            self.files_list.select_row(first)

    def _on_file_selected(self, _list: Gtk.ListBox, row: FileRow | None) -> None:
        if row is not None and row.index < len(self.files):
            self.diff.show_file(self.files[row.index])

    def _step_file(self, delta: int) -> None:
        row = self.files_list.get_selected_row()
        index = (row.index if isinstance(row, FileRow) else -1) + delta
        target = self.files_list.get_row_at_index(index)
        if target is not None:
            self.files_list.select_row(target)
            target.grab_focus()

    # -- avatar -----------------------------------------------------------

    def _load_avatar(self, url: str) -> None:
        if not url:
            return
        if url in _avatar_cache:
            self.avatar.set_custom_image(_avatar_cache[url])
            return

        def work():
            try:
                data = self.client.fetch_bytes(url + ("&" if "?" in url else "?") + "s=64")
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
            except Exception:  # noqa: BLE001 - avatars are decorative
                return
            on_main(self._avatar_loaded, url, texture)

        threading.Thread(target=work, daemon=True).start()

    def _avatar_loaded(self, url: str, texture: Gdk.Texture) -> None:
        _avatar_cache[url] = texture
        if self.pr and self.pr.author_avatar == url:
            self.avatar.set_custom_image(texture)

    # -- review actions ---------------------------------------------------

    def _open_in_browser(self) -> None:
        if self.pr:
            Gtk.UriLauncher.new(self.pr.url).launch(self.get_root(), None, None)

    def _review(self, event: str, initial_text: str = "") -> None:
        if not self.pr:
            return
        dialog = ReviewDialog(event, self.pr.key, self._submit_review, initial_text=initial_text)
        dialog.present(self.get_root())

    def _submit_review(self, event: str, body: str) -> None:
        pr = self.pr
        if not pr:
            return
        self.set_sensitive(False)

        def work():
            try:
                self.client.submit_review(pr.repo, pr.number, event, body)
                on_main(self._review_done, pr, event, "")
            except GitHubError as exc:
                on_main(self._review_done, pr, event, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _review_done(self, pr: PullRequest, event: str, error: str) -> None:
        self.set_sensitive(True)
        if error:
            self.emit("toast", f"Review failed: {error}")
            return
        verb = {"APPROVE": "Approved", "REQUEST_CHANGES": "Requested changes on", "COMMENT": "Commented on"}[event]
        self.emit("toast", f"{verb} {pr.key}")
        self.emit("reviewed", pr.key, event)

    # -- Claude -----------------------------------------------------------

    def _show_ai_state(self, pr: PullRequest) -> None:
        """Attach the panel to this PR's live run, or its last saved run, or hide it."""
        run = self.ai.active(pr)
        if run is not None:
            self.ai_panel.attach(run)
            self._reveal_panel()
            return
        saved = load_last_run(pr)
        if saved:
            self.ai_panel.show_saved(*saved)
            self._reveal_panel()
            return
        self.ai_panel.detach()
        self.ai_panel.set_visible(False)

    def _reveal_panel(self) -> None:
        if not self.ai_panel.get_visible():
            self.ai_panel.set_visible(True)
            height = self.vpaned.get_height()
            if height > 0:
                self.vpaned.set_position(int(height * 0.5))

    def _on_skill_picked(self, skill: Skill | None) -> None:
        if not self.pr:
            return
        if skill is None:
            dialog = ReviewDialog("PROMPT", self.pr.key, self._custom_prompt)
            dialog.present(self.get_root())
            return
        self._start_run(skill)

    def _custom_prompt(self, _event: str, body: str) -> None:
        self._start_run(Skill("Custom prompt", "", "custom", body.strip()), prompt=body.strip())

    def _start_run(self, skill: Skill, prompt: str | None = None) -> None:
        pr = self.pr
        if not pr:
            return
        current = self.ai.active(pr)
        if current is not None and current.state == "running":
            self.emit("toast", f"Claude is already running on {pr.key}; stop it first")
            return
        log.debug("starting %r on %s", skill.name, pr.key)
        run = self.ai.run(pr, skill, prompt)
        run.connect("finished", self._on_run_finished)
        self.ai_panel.attach(run)
        self._reveal_panel()

    def _on_run_finished(self, run, ok: bool, summary: str) -> None:
        log.debug("run finished on %s ok=%s: %s", run.pr.key, ok, summary)
        if self.pr is None or self.pr.key != run.pr.key:
            self.emit("toast", f"Claude {'finished' if ok else 'failed'} on {run.pr.key}: {summary}")

    def _post_ai_comment(self, _panel, text: str) -> None:
        if text.strip():
            self._review("COMMENT", initial_text=text.strip())

    def run_skill_named(self, name: str) -> None:
        if not self.pr:
            return
        for skill in self.ai.skills_for(self.pr):
            if skill.name.lower() == name.lower():
                self._start_run(skill)
                return
        self.emit("toast", f"No Claude action named {name!r}")

    def populate_picker(self) -> None:
        if self.pr:
            self.picker.populate(self.ai.skills_for(self.pr))
