"""System tray icon: a StatusNotifierItem with a DBusMenu, spoken directly over GIO D-Bus.

Works with any StatusNotifierWatcher host (waybar's tray module, KDE, etc.). The
icon is drawn with cairo so a count badge can be baked in; no theme lookup needed.
"""

from __future__ import annotations

import logging
import math
from importlib import resources

import cairo
import gi
from gi.repository import Gio, GLib

try:
    gi.require_version("Rsvg", "2.0")
    from gi.repository import Rsvg
except (ValueError, ImportError):  # librsvg typelib missing: fall back to the drawn glyph
    Rsvg = None

from .config import APP_ID, APP_NAME
from .github import PullRequest

log = logging.getLogger("prewster.tray")

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
SIZES = (16, 22, 24, 32, 48)

SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
    <method name="Activate"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
    <method name="SecondaryActivate"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
    <method name="Scroll"><arg type="i" name="delta" direction="in"/><arg type="s" name="orientation" direction="in"/></method>
    <method name="ProvideXdgActivationToken"><arg type="s" name="token" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg type="s" name="status"/></signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated"><arg type="u" name="revision"/><arg type="i" name="parent"/></signal>
    <signal name="ItemActivationRequested"><arg type="i" name="id"/><arg type="u" name="timestamp"/></signal>
  </interface>
</node>
"""


# -- icon rendering ---------------------------------------------------------

def _rounded_rect(cr: cairo.Context, x: float, y: float, w: float, h: float, r: float) -> None:
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


_svg_handle = None


def _icon_handle():
    """The packaged rooster SVG, loaded once, or None if librsvg is unavailable."""
    global _svg_handle
    if _svg_handle is None and Rsvg is not None:
        try:
            with resources.as_file(resources.files("prewster").joinpath("icons", f"{APP_ID}.svg")) as path:
                _svg_handle = Rsvg.Handle.new_from_file(str(path))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not load tray SVG: %s", exc)
            _svg_handle = False
    return _svg_handle or None


def draw_badge(cr: cairo.Context, size: int, count: int) -> None:
    """Red count badge, drawn in 128-unit coordinates (caller scales)."""
    cx, cy, r = 98, 30, 26
    cr.new_sub_path()
    cr.arc(cx, cy, r + 5, 0, 2 * math.pi)
    cr.set_source_rgb(1, 1, 1)
    cr.fill()
    cr.new_sub_path()
    cr.arc(cx, cy, r, 0, 2 * math.pi)
    cr.set_source_rgb(0.878, 0.106, 0.141)
    cr.fill()
    if size >= 22:
        label = str(count) if count < 100 else "99+"
        cr.set_source_rgb(1, 1, 1)
        cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(38 if len(label) == 1 else 30)
        ext = cr.text_extents(label)
        cr.move_to(cx - ext.width / 2 - ext.x_bearing, cy + ext.height / 2)
        cr.show_text(label)


def draw_icon(cr: cairo.Context, size: int, count: int, muted: bool = False) -> None:
    """The app icon at `size` pixels with an optional count badge."""
    handle = _icon_handle()
    if handle is not None:
        rect = Rsvg.Rectangle()
        rect.x = rect.y = 0
        rect.width = rect.height = size
        if muted:
            cr.push_group()
            handle.render_document(cr, rect)
            cr.pop_group_to_source()
            cr.paint_with_alpha(0.45)
        else:
            handle.render_document(cr, rect)
        if count > 0:
            cr.save()
            cr.scale(size / 128.0, size / 128.0)
            draw_badge(cr, size, count)
            cr.restore()
        return
    _draw_glyph(cr, size, count, muted)


def _draw_glyph(cr: cairo.Context, size: int, count: int, muted: bool = False) -> None:
    """Fallback: pull-request glyph on a tile, 128-unit design scaled to `size`."""
    cr.scale(size / 128.0, size / 128.0)
    gradient = cairo.LinearGradient(0, 8, 0, 120)
    if muted:
        gradient.add_color_stop_rgb(0, 0.55, 0.57, 0.60)
        gradient.add_color_stop_rgb(1, 0.38, 0.40, 0.44)
    else:
        gradient.add_color_stop_rgb(0, 0.384, 0.627, 0.918)
        gradient.add_color_stop_rgb(1, 0.110, 0.443, 0.847)
    _rounded_rect(cr, 8, 8, 112, 112, 26)
    cr.set_source(gradient)
    cr.fill()

    cr.set_source_rgb(1, 1, 1)
    cr.set_line_width(10)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    for cx, cy in ((42, 34), (42, 94), (86, 94)):
        cr.new_sub_path()
        cr.arc(cx, cy, 11, 0, 2 * math.pi)
        cr.stroke()
    cr.move_to(42, 45)
    cr.line_to(42, 83)
    cr.stroke()
    cr.move_to(66, 34)
    cr.line_to(74, 34)
    cr.arc(74, 46, 12, -math.pi / 2, 0)
    cr.line_to(86, 83)
    cr.stroke()
    cr.move_to(75, 23)
    cr.line_to(64, 34)
    cr.line_to(75, 45)
    cr.stroke()

    if count > 0:
        draw_badge(cr, size, count)


def render_pixmaps(count: int, muted: bool = False) -> list[tuple[int, int, bytes]]:
    """ARGB32 network-order, non-premultiplied pixmaps for every size in SIZES."""
    out = []
    for size in SIZES:
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        cr = cairo.Context(surface)
        draw_icon(cr, size, count, muted)
        surface.flush()
        data = surface.get_data()
        stride = surface.get_stride()
        buf = bytearray(size * size * 4)
        i = 0
        for y in range(size):
            row = data[y * stride:y * stride + size * 4]
            for x in range(size):
                b, g, r, a = row[x * 4:x * 4 + 4]  # little-endian BGRA premultiplied
                if 0 < a < 255:
                    r = min(255, r * 255 // a)
                    g = min(255, g * 255 // a)
                    b = min(255, b * 255 // a)
                buf[i:i + 4] = (a, r, g, b)
                i += 4
        out.append((size, size, bytes(buf)))
    return out


# -- the item ---------------------------------------------------------------

class TrayIcon:
    MENU_OPEN, MENU_REFRESH, MENU_QUIT, MENU_SEP = 1001, 1002, 1003, 1000

    def __init__(self, app):
        self.app = app
        self.conn: Gio.DBusConnection = app.get_dbus_connection()
        self.prs: list[PullRequest] = []
        self.error = ""
        self.status = "Active"
        self._pixmaps = render_pixmaps(0)
        self._attention = self._pixmaps
        self._revision = 1
        self._registered = False

        sni = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self.conn.register_object(ITEM_PATH, sni, self._item_call, self._item_prop, None)
        self.conn.register_object(MENU_PATH, menu, self._menu_call, self._menu_prop, None)
        Gio.bus_watch_name(Gio.BusType.SESSION, WATCHER, Gio.BusNameWatcherFlags.NONE,
                           self._watcher_appeared, self._watcher_vanished)

    # -- registration -----------------------------------------------------

    def _watcher_appeared(self, _conn, _name, _owner) -> None:
        self.conn.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                       GLib.Variant("(s)", (self.conn.get_unique_name(),)), None,
                       Gio.DBusCallFlags.NONE, 5000, None, self._registered_cb)

    def _registered_cb(self, conn, result) -> None:
        try:
            conn.call_finish(result)
            self._registered = True
            log.debug("tray icon registered with %s", WATCHER)
        except GLib.Error as exc:
            log.warning("tray registration failed: %s", exc.message)

    def _watcher_vanished(self, _conn, _name) -> None:
        self._registered = False
        log.debug("tray watcher vanished")

    # -- state ------------------------------------------------------------

    def update(self, prs: list[PullRequest], error: str) -> None:
        self.prs = list(prs)
        self.error = error
        count = len(prs)
        self._pixmaps = render_pixmaps(count, muted=bool(error) and not prs)
        self._attention = self._pixmaps
        new_status = "NeedsAttention" if count else "Active"
        self._revision += 1
        for signal in ("NewIcon", "NewAttentionIcon", "NewToolTip", "NewTitle"):
            self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", signal, None)
        if new_status != self.status:
            self.status = new_status
            self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewStatus", GLib.Variant("(s)", (new_status,)))
        self._emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated", GLib.Variant("(ui)", (self._revision, 0)))

    def _emit(self, path: str, iface: str, name: str, args: GLib.Variant | None) -> None:
        try:
            self.conn.emit_signal(None, path, iface, name, args)
        except GLib.Error as exc:
            log.warning("tray signal %s failed: %s", name, exc.message)

    def _title(self) -> str:
        n = len(self.prs)
        if self.error and not self.prs:
            return f"{APP_NAME}: {self.error}"
        if n == 0:
            return "No pull requests waiting for your review"
        return f"{n} pull request{'s' if n != 1 else ''} waiting for your review"

    def _tooltip_body(self) -> str:
        lines = [f"{GLib.markup_escape_text(p.key)}  {GLib.markup_escape_text(p.title)}" for p in self.prs[:12]]
        if len(self.prs) > 12:
            lines.append(f"…and {len(self.prs) - 12} more")
        return "\n".join(lines)

    # -- StatusNotifierItem -----------------------------------------------

    def _item_prop(self, _conn, _sender, _path, _iface, name: str) -> GLib.Variant:
        values = {
            "Category": ("s", "ApplicationStatus"),
            "Id": ("s", APP_ID),
            "Title": ("s", APP_NAME),
            "Status": ("s", self.status),
            "WindowId": ("i", 0),
            "IconThemePath": ("s", ""),
            "IconName": ("s", ""),
            "IconPixmap": ("a(iiay)", self._pixmaps),
            "OverlayIconName": ("s", ""),
            "OverlayIconPixmap": ("a(iiay)", []),
            "AttentionIconName": ("s", ""),
            "AttentionIconPixmap": ("a(iiay)", self._attention),
            "AttentionMovieName": ("s", ""),
            "ToolTip": ("(sa(iiay)ss)", ("", [], self._title(), self._tooltip_body())),
            "ItemIsMenu": ("b", False),
            "Menu": ("o", MENU_PATH),
        }
        sig, value = values[name]
        return GLib.Variant(sig, value)

    def _item_call(self, _conn, _sender, _path, _iface, method: str, _params, invocation) -> None:
        if method == "Activate":
            self.app.activate_action("show", None)
        elif method == "SecondaryActivate":
            self.app.activate_action("refresh", None)
        elif method == "ContextMenu":
            self.app.activate_action("show", None)
        invocation.return_value(None)

    # -- DBusMenu ---------------------------------------------------------

    def _menu_items(self) -> list[tuple[int, dict]]:
        def props(label: str, enabled: bool = True) -> dict:
            return {"label": GLib.Variant("s", label), "enabled": GLib.Variant("b", enabled)}

        items: list[tuple[int, dict]] = []
        if self.prs:
            for i, pr in enumerate(self.prs[:15]):
                title = pr.title if len(pr.title) <= 60 else pr.title[:57] + "…"
                items.append((i + 1, props(f"{pr.key}  {title}")))
        else:
            items.append((1, props(self._title(), enabled=False)))
        items.append((self.MENU_SEP, {"type": GLib.Variant("s", "separator")}))
        items.append((self.MENU_OPEN, props(f"Open {APP_NAME}")))
        items.append((self.MENU_REFRESH, props("Refresh now")))
        items.append((self.MENU_QUIT, props("Quit")))
        return items

    def _menu_prop(self, _conn, _sender, _path, _iface, name: str) -> GLib.Variant:
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }[name]

    def _menu_call(self, _conn, _sender, _path, _iface, method: str, params: GLib.Variant, invocation) -> None:
        if method == "GetLayout":
            children = [GLib.Variant("(ia{sv}av)", (item_id, props, [])) for item_id, props in self._menu_items()]
            root = (0, {"children-display": GLib.Variant("s", "submenu")}, children)
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self._revision, root)))
        elif method == "GetGroupProperties":
            ids = set(params.unpack()[0])
            rows = [(item_id, props) for item_id, props in self._menu_items() if not ids or item_id in ids]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (rows,)))
        elif method == "GetProperty":
            item_id, name = params.unpack()
            for iid, props in self._menu_items():
                if iid == item_id and name in props:
                    invocation.return_value(GLib.Variant("(v)", (props[name],)))
                    return
            invocation.return_dbus_error("com.canonical.dbusmenu.Error", "unknown property")
        elif method == "Event":
            item_id, event_id, _data, _ts = params.unpack()
            if event_id == "clicked":
                self._activate_item(item_id)
            invocation.return_value(None)
        elif method == "EventGroup":
            for item_id, event_id, _data, _ts in params.unpack()[0]:
                if event_id == "clicked":
                    self._activate_item(item_id)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _activate_item(self, item_id: int) -> None:
        if item_id == self.MENU_OPEN:
            self.app.activate_action("show", None)
        elif item_id == self.MENU_REFRESH:
            self.app.activate_action("refresh", None)
        elif item_id == self.MENU_QUIT:
            self.app.activate_action("quit", None)
        elif 1 <= item_id <= len(self.prs):
            self.app.activate_action("show-pr", GLib.Variant("s", self.prs[item_id - 1].key))
