"""Detail pane: PR metadata, changed files and diff, conversation, review actions, Claude chat."""

from __future__ import annotations

import logging
import os
import threading

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from . import local
from .ai import CLAUDE_MISSING, AIService, Skill
from .aipanel import AIPanel
from .conversation import ConversationView, thread_card
from .diffview import DiffView
from .github import ChangedFile, Conversation, GitHubClient, GitHubError, PullRequest, ReviewThread
from .poller import on_main
from .reviewdialog import ReviewDialog
from .skillpicker import SkillPicker
from .util import AvatarLoader, ci_badge, esc, humanize, review_badge, text_color_for
from .viewed import ViewedStore

STATUS_LETTER = {
    "added": ("A", "badge-ok"),
    "removed": ("D", "badge-err"),
    "renamed": ("R", "badge-info"),
    "copied": ("C", "badge-info"),
    "modified": ("M", "badge-warn"),
    "changed": ("M", "badge-warn"),
    "unchanged": ("·", "badge-muted"),
}

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

        self.comments = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3, valign=Gtk.Align.CENTER,
                                visible=False)
        self.comments.append(Gtk.Image.new_from_icon_name("user-available-symbolic"))
        self.comment_count = Gtk.Label()
        self.comments.append(self.comment_count)
        self.comments.add_css_class("caption")
        self.comments.add_css_class("dim-label")
        box.append(self.comments)

        stats = Gtk.Label(label=f"+{f.additions} −{f.deletions}")
        stats.add_css_class("caption")
        stats.add_css_class("dim-label")
        stats.add_css_class("numeric")
        stats.set_valign(Gtk.Align.CENTER)
        box.append(stats)

        self.check = Gtk.Image(icon_name="object-select-symbolic", valign=Gtk.Align.CENTER)
        self.check.add_css_class("viewed-check")
        self.check.set_opacity(0)  # reserves its space so the stats don't jump when it appears
        box.append(self.check)
        self.set_child(box)
        self.set_tooltip_text(f.filename)
        self.viewed = False

    def set_viewed(self, viewed: bool) -> None:
        self.viewed = viewed
        self.check.set_opacity(1 if viewed else 0)
        self.check.set_tooltip_text("Viewed: you scrolled to the end of this file" if viewed else None)

    def set_comment_count(self, n: int) -> None:
        self.comment_count.set_label(str(n))
        self.comments.set_visible(n > 0)
        self.comments.set_tooltip_text(f"{n} review comment{'s' if n != 1 else ''}")


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
        self.narrow = False
        self.avatars = AvatarLoader(client)
        self.viewed = ViewedStore()
        self.conversation: Conversation | None = None
        self._load_token = 0  # bumped per file load
        self._shown = 0  # bumped per show(), guards the conversation load
        toolbar = Adw.ToolbarView(vexpand=True)
        self.append(toolbar)

        # Header bar with actions.
        header = Adw.HeaderBar()
        self.title = Adw.WindowTitle()
        header.set_title_widget(self.title)
        self.sidebar_btn = Gtk.ToggleButton()  # wired to the sidebar by the window
        header.pack_start(self.sidebar_btn)

        self.browser_btn = Gtk.Button(icon_name="external-link-symbolic", tooltip_text="Open on GitHub")
        self.browser_btn.connect("clicked", lambda *_: self._open_in_browser())
        header.pack_end(self.browser_btn)

        self.actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.approve_btn = Gtk.Button(label="Approve")
        self.approve_btn.add_css_class("suggested-action")
        self.approve_btn.connect("clicked", lambda *_: self._review("APPROVE"))
        self.changes_btn = Gtk.Button(label="Request changes")
        self.changes_btn.connect("clicked", lambda *_: self._review("REQUEST_CHANGES"))
        comment_btn = Gtk.Button(label="Comment")
        comment_btn.connect("clicked", lambda *_: self._review("COMMENT"))
        self.actions_box.append(comment_btn)
        self.actions_box.append(self.changes_btn)
        self.actions_box.append(self.approve_btn)
        header.pack_end(self.actions_box)

        # Compact replacement for the three buttons on narrow windows.
        self.review_menu = Gio.Menu()
        self.review_menu.append("Approve", "detail.review::APPROVE")
        self.review_menu.append("Request changes", "detail.review::REQUEST_CHANGES")
        self.review_menu.append("Comment", "detail.review::COMMENT")
        self.comment_menu = Gio.Menu()  # your own pull request: GitHub only takes comments
        self.comment_menu.append("Comment", "detail.review::COMMENT")
        self.review_menu_btn = Gtk.MenuButton(menu_model=self.review_menu, always_show_arrow=True, visible=False)
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
            self.claude_btn.set_tooltip_text("Run a Claude Code skill, or chat with Claude about this change")
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

        # Files | Conversation.
        self.views = Adw.ViewStack(vexpand=True)
        switcher = Adw.InlineViewSwitcher(stack=self.views, halign=Gtk.Align.START)
        switcher.add_css_class("flat")
        meta.append(switcher)

        # Files + diff.
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=300, vexpand=True)
        self.paned.set_shrink_start_child(False)
        self.paned.set_shrink_end_child(False)

        files_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        files_top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        files_top.set_margin_top(4)
        files_top.set_margin_start(12)
        files_top.set_margin_end(4)
        self.files_header = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
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

        self.diff = DiffView(thread_widget=lambda t: thread_card(self.avatars, t))
        self.diff.connect("read-to-end", self._on_read_to_end)
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
        self.views.add_titled(self.stack, "files", "Files")

        self.conversation_view = ConversationView(self.avatars)
        self.conversation_view.connect("jump", self._on_jump)
        self.conversation_page = self.views.add_titled(self.conversation_view, "conversation", "Conversation")

        # Claude panel below the diff.
        self.ai_panel = AIPanel()
        self.ai_panel.set_visible(False)
        self.ai_panel.connect("close", lambda *_: self.ai_panel.set_visible(False))
        self.ai_panel.connect("post-comment", self._post_ai_comment)
        self.ai_panel.connect("send", self._on_chat_send)
        self.ai_panel.connect("new-chat", lambda *_: self.pr and self.ai_panel.new_chat(self.pr))
        self.ai_panel.connect("open-terminal", self._on_open_terminal)
        self.vpaned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        self.vpaned.set_start_child(self.views)
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
        """Show a pull request or local branch. Showing the same one again reloads it and
        keeps the selected file."""
        keep = None
        if self.pr is not None and self.pr.key == pr.key:
            row = self.files_list.get_selected_row()
            keep = self.files[row.index].filename if isinstance(row, FileRow) and row.index < len(self.files) else None
        self.pr = pr
        self.title.set_title(pr.title)
        if pr.is_local:
            self.title.set_subtitle(f"{pr.repo} · {pr.head_ref}")
            where = pr.path.replace(os.path.expanduser("~"), "~", 1)
            self.branch_label.set_markup(
                f"<tt>{esc(pr.head_ref)}</tt> compared with <tt>{esc(pr.base_ref)}</tt>"
                f"  <span alpha='60%'>· {esc(where)}</span>")
        else:
            self.title.set_subtitle(f"{pr.repo} #{pr.number}")
            self.branch_label.set_markup(
                f"<b>{esc(pr.author)}</b> wants to merge <tt>{esc(pr.head_ref)}</tt> into <tt>{esc(pr.base_ref)}</tt>"
                f"  <span alpha='60%'>· opened {esc(humanize(pr.created_at))}</span>")
        if pr.is_local and self.client.login:  # the list may have loaded before the first search named you
            pr.author, pr.author_avatar = self.client.login, pr.author_avatar or self.client.avatar_url
        self.avatar.set_text(pr.author)
        self.avatars.apply(self.avatar, pr.author_avatar)

        for child in list(self.badges):
            self.badges.remove(child)
        if pr.is_local:
            if pr.dirty:
                self.badges.append(_badge("Uncommitted changes", "badge-warn"))
            if pr.ahead:
                self.badges.append(_badge(f"{pr.ahead} commit{'s' if pr.ahead != 1 else ''} ahead", "badge-info"))
        else:
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

        self.browser_btn.set_sensitive(bool(pr.url))
        self.browser_btn.set_tooltip_text("Open on GitHub" if pr.url else "Not pushed yet")
        self.approve_btn.set_sensitive(not pr.is_draft)
        self.approve_btn.set_tooltip_text("Draft pull requests cannot be approved" if pr.is_draft else None)
        self._update_review_buttons()

        self._shown += 1
        self.conversation = None
        self.conversation_page.set_title("Commits" if pr.is_local else "Conversation")
        self.conversation_view.show(pr, None)
        if not pr.is_local:
            self._load_conversation(pr, self._shown)
        self._load_files(pr, keep)
        self._show_ai_state(pr)

    def set_narrow(self, narrow: bool) -> None:
        self.narrow = narrow
        self._update_review_buttons()

    def _update_review_buttons(self) -> None:
        """Approve and Request changes for other people's pull requests, Comment for your own,
        nothing for a local branch. Narrow windows fold them into one menu."""
        pr = self.pr
        is_local = pr is not None and pr.is_local
        own = pr is not None and not is_local and pr.author.lower() == (self.client.login or "").lower()
        self.approve_btn.set_visible(not own)
        self.changes_btn.set_visible(not own)
        self.actions_box.set_visible(not is_local and not self.narrow)
        self.review_menu_btn.set_visible(not is_local and self.narrow)
        self.review_menu_btn.set_menu_model(self.comment_menu if own else self.review_menu)

    # -- files ------------------------------------------------------------

    def _load_files(self, pr: PullRequest, keep: str | None = None) -> None:
        self._load_token += 1
        token = self._load_token
        self.stack.set_visible_child_name("loading")
        self.files = []
        self.files_list.remove_all()
        self.diff.clear()

        def work():
            try:
                files = local.list_files(pr) if pr.is_local else self.client.list_files(pr.repo, pr.number)
                on_main(self._files_loaded, token, files, "", keep)
            except (GitHubError, local.LocalError) as exc:
                on_main(self._files_loaded, token, None, str(exc), keep)
            except Exception as exc:  # noqa: BLE001 - show it rather than spin forever
                on_main(self._files_loaded, token, None, f"Unexpected error: {exc!r}", keep)

        threading.Thread(target=work, daemon=True).start()

    def _files_loaded(self, token: int, files: list[ChangedFile] | None, error: str, keep: str | None) -> None:
        if token != self._load_token:
            return
        if files is None:
            self.error_page.set_description(error)
            self.stack.set_visible_child_name("error")
            return
        self.files = files
        for i, f in enumerate(files):
            row = FileRow(i, f)
            row.set_viewed(self.viewed.is_viewed(self.pr.key, f))
            self.files_list.append(row)
        self._update_comment_counts()
        self._update_files_header()
        self.stack.set_visible_child_name("diff")
        index = next((i for i, f in enumerate(files) if f.filename == keep), 0)
        target = self.files_list.get_row_at_index(index)
        if target is not None:
            self.files_list.select_row(target)

    def _update_files_header(self) -> None:
        n = len(self.files)
        viewed = sum(1 for row in self.files_list if isinstance(row, FileRow) and row.viewed)
        text = f"{n} changed file{'s' if n != 1 else ''}"
        self.files_header.set_label(f"{text} · {viewed} viewed" if viewed else text)

    def _threads_for(self, filename: str) -> list[ReviewThread]:
        if self.conversation is None:
            return []
        return [t for t in self.conversation.threads if t.path == filename]

    def _on_file_selected(self, _list: Gtk.ListBox, row: FileRow | None) -> None:
        if row is not None and row.index < len(self.files):
            f = self.files[row.index]
            self.diff.show_file(f, self._threads_for(f.filename))

    def _on_read_to_end(self, _diff) -> None:
        row = self.files_list.get_selected_row()
        if self.pr is None or not isinstance(row, FileRow) or row.index >= len(self.files) or row.viewed:
            return
        self.viewed.mark(self.pr.key, self.files[row.index])
        row.set_viewed(True)
        self._update_files_header()

    def _step_file(self, delta: int) -> None:
        row = self.files_list.get_selected_row()
        index = (row.index if isinstance(row, FileRow) else -1) + delta
        target = self.files_list.get_row_at_index(index)
        if target is not None:
            self.files_list.select_row(target)
            target.grab_focus()

    # -- conversation -----------------------------------------------------

    def _load_conversation(self, pr: PullRequest, shown: int) -> None:
        def work():
            try:
                on_main(self._conversation_loaded, shown, self.client.conversation(pr.repo, pr.number), "")
            except GitHubError as exc:
                on_main(self._conversation_loaded, shown, None, str(exc))
            except Exception as exc:  # noqa: BLE001
                on_main(self._conversation_loaded, shown, None, f"Unexpected error: {exc!r}")

        threading.Thread(target=work, daemon=True).start()

    def _conversation_loaded(self, shown: int, conversation: Conversation | None, error: str) -> None:
        if shown != self._shown:
            return
        self.conversation = conversation
        self.conversation_view.show(self.pr, conversation, error)
        n = ConversationView.count(conversation)
        self.conversation_page.set_title(f"Conversation ({n})" if n else "Conversation")
        self._update_comment_counts()
        # Redraw the open file if it has threads to show inline, keeping the scroll position.
        row = self.files_list.get_selected_row()
        if isinstance(row, FileRow) and row.index < len(self.files):
            f = self.files[row.index]
            threads = self._threads_for(f.filename)
            if threads:
                position = self.diff.get_vadjustment().get_value()
                self.diff.show_file(f, threads)
                GLib.idle_add(self._restore_scroll, row, position, priority=GLib.PRIORITY_LOW)

    def _restore_scroll(self, row: FileRow, position: float) -> bool:
        if row.is_selected():
            self.diff.get_vadjustment().set_value(position)
        return False

    def _update_comment_counts(self) -> None:
        for row in self.files_list:
            if isinstance(row, FileRow) and row.index < len(self.files):
                threads = self._threads_for(self.files[row.index].filename)
                row.set_comment_count(sum(len(t.comments) for t in threads))

    def _on_jump(self, _view, path: str, side: str, line: int) -> None:
        index = next((i for i, f in enumerate(self.files) if f.filename == path), None)
        if index is None:
            self.emit("toast", f"{path} is not among the changed files")
            return
        self.views.set_visible_child_name("files")
        target = self.files_list.get_row_at_index(index)
        if not target.is_selected():
            self.files_list.select_row(target)
        if not self.diff.scroll_to_line(side, line):
            self.emit("toast", f"Line {line} is not in the diff of {path}")

    # -- review actions ---------------------------------------------------

    def _open_in_browser(self) -> None:
        if self.pr and self.pr.url:
            Gtk.UriLauncher.new(self.pr.url).launch(self.get_root(), None, None)

    def _review(self, event: str, initial_text: str = "") -> None:
        if not self.pr or self.pr.is_local:
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
        """Attach the panel to this PR's conversation (live or saved), or hide it."""
        run = self.ai.active(pr) or self.ai.restore(pr)
        if run is not None:
            self.ai_panel.attach(run)
            self._reveal_panel()
            return
        self.ai_panel.new_chat(pr, focus=False)
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
        if skill is None:  # "Chat with Claude…"
            current = self.ai.active(self.pr)
            if current is None or current.state != "running":
                self.ai_panel.new_chat(self.pr)
            self._reveal_panel()
            self.ai_panel.entry.grab_focus()
            return
        self._start_run(skill)

    def _on_chat_send(self, _panel, text: str, allow_edits: bool) -> None:
        pr = self.pr
        run = self.ai_panel.run
        if pr is None:
            return
        if run is not None and run.pr.key == pr.key and run.can_follow_up:
            run.send(text, allow_edits)
        else:
            self._start_run(Skill("Chat", "", "custom", text), prompt=text, allow_edits=allow_edits)

    def _start_run(self, skill: Skill, prompt: str | None = None, allow_edits: bool = False) -> None:
        pr = self.pr
        if not pr:
            return
        current = self.ai.active(pr)
        if current is not None and current.state == "running":
            self.emit("toast", f"Claude is already running on {pr.key}; stop it first")
            return
        log.debug("starting %r on %s", skill.name, pr.key)
        run = self.ai.run(pr, skill, prompt, allow_edits)
        run.connect("finished", self._on_run_finished)
        self.ai_panel.attach(run)
        self._reveal_panel()

    def _on_run_finished(self, run, ok: bool, summary: str) -> None:
        log.debug("run finished on %s ok=%s: %s", run.pr.key, ok, summary)
        if self.pr is None or self.pr.key != run.pr.key:
            self.emit("toast", f"Claude {'finished' if ok else 'failed'} on {run.pr.key}: {summary}")

    def _on_open_terminal(self, _panel) -> None:
        run = self.ai_panel.run
        error = run.open_in_terminal() if run is not None else "Nothing to continue yet: start a chat first"
        if error:
            self.emit("toast", error)

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
