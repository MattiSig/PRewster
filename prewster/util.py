"""Small helpers shared by the widgets."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from gi.repository import Adw, Gdk, GLib

from .poller import on_main


def humanize(dt: datetime) -> str:
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 7 * 86400:
        return f"{int(seconds // 86400)}d ago"
    return dt.astimezone().strftime("%b %d")


def esc(text: str) -> str:
    return GLib.markup_escape_text(text or "")


def ci_badge(state: str | None) -> tuple[str, str]:
    """Return (label, css class) for a statusCheckRollup state."""
    return {
        "SUCCESS": ("Checks passed", "badge-ok"),
        "FAILURE": ("Checks failed", "badge-err"),
        "ERROR": ("Checks errored", "badge-err"),
        "PENDING": ("Checks running", "badge-warn"),
        "EXPECTED": ("Checks expected", "badge-warn"),
    }.get(state or "", ("No checks", "badge-muted"))


def review_badge(decision: str | None) -> tuple[str, str] | None:
    return {
        "APPROVED": ("Approved", "badge-ok"),
        "CHANGES_REQUESTED": ("Changes requested", "badge-err"),
    }.get(decision or "")


def text_color_for(hex_color: str) -> str:
    """Black or white text, whichever reads better on the given background."""
    try:
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        return "#000000"
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return "#000000" if luminance > 150 else "#ffffff"


class AvatarLoader:
    """Downloads each avatar once and sets it on the Adw.Avatar widgets that asked for it."""

    def __init__(self, client):
        self.client = client
        self._cache: dict[str, Gdk.Texture] = {}
        self._waiting: dict[str, list[Adw.Avatar]] = {}

    def apply(self, avatar: Adw.Avatar, url: str) -> None:
        avatar.avatar_url = url
        avatar.set_custom_image(self._cache.get(url))
        if not url or url in self._cache:
            return
        waiting = self._waiting.setdefault(url, [])
        waiting.append(avatar)
        if len(waiting) == 1:
            threading.Thread(target=self._fetch, args=(url,), daemon=True).start()

    def _fetch(self, url: str) -> None:
        try:
            data = self.client.fetch_bytes(url + ("&" if "?" in url else "?") + "s=64")
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
        except Exception:  # noqa: BLE001 - avatars are decorative
            texture = None
        on_main(self._loaded, url, texture)

    def _loaded(self, url: str, texture: Gdk.Texture | None) -> None:
        if texture is not None:
            self._cache[url] = texture
        for avatar in self._waiting.pop(url, []):
            if texture is not None and getattr(avatar, "avatar_url", None) == url:
                avatar.set_custom_image(texture)
