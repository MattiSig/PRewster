"""The notification sound: the rooster's crow, or a sound file of your own.

Played with an external player so there is no audio stack to load in-process, and
skipped while the notification daemon is in do-not-disturb mode.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from contextlib import nullcontext
from importlib import resources
from pathlib import Path

from gi.repository import Gio

log = logging.getLogger("prewster.sound")

BUILTIN = "rooster"
# Tried in order; the sound file is appended to the command.
PLAYERS = (["pw-play"], ["paplay"], ["canberra-gtk-play", "-f"])


def _output(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def do_not_disturb() -> bool:
    """True when notifications are silenced: mako's do-not-disturb mode, dunst or swaync
    paused, or GNOME notification banners turned off."""
    if shutil.which("makoctl") and "do-not-disturb" in _output(["makoctl", "mode"]).split():
        return True
    if shutil.which("dunstctl") and _output(["dunstctl", "is-paused"]).strip() == "true":
        return True
    if shutil.which("swaync-client") and _output(["swaync-client", "--get-dnd", "--skip-wait"]).strip() == "true":
        return True
    source = Gio.SettingsSchemaSource.get_default()
    if source and source.lookup("org.gnome.desktop.notifications", True):
        if not Gio.Settings.new("org.gnome.desktop.notifications").get_boolean("show-banners"):
            return True
    return False


def _play(setting: str, respect_dnd: bool) -> bool:
    if respect_dnd and do_not_disturb():
        log.debug("do-not-disturb is on, not playing a sound")
        return False
    if setting == BUILTIN:
        source = resources.as_file(resources.files("prewster").joinpath("sounds", f"{BUILTIN}.oga"))
    else:
        source = nullcontext(Path(setting).expanduser())
    with source as path:
        if not path.is_file():
            log.warning("notification sound not found: %s", path)
            return False
        players = [p for p in PLAYERS if shutil.which(p[0])]
        for player in players:
            try:
                result = subprocess.run([*player, str(path)], stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=30)
            except subprocess.TimeoutExpired:
                log.warning("%s timed out playing %s", player[0], path)
                return False
            except OSError as exc:
                log.debug("%s failed to start: %s", player[0], exc)
                continue
            if result.returncode == 0:
                return True
            log.debug("%s exited with %d", player[0], result.returncode)
    if players:
        log.warning("could not play %s with %s", path, ", ".join(p[0] for p in players))
    else:
        log.warning("no sound player found: install pipewire, libpulse or libcanberra")
    return False


def play(setting: str, respect_dnd: bool = True, wait: bool = False) -> bool:
    """Play the configured sound: "rooster", a file path, or "" for none.

    Runs in a background thread unless wait is set; returns whether it was played
    (always False when not waiting).
    """
    setting = setting.strip()
    if not setting:
        return False
    if wait:
        return _play(setting, respect_dnd)
    threading.Thread(target=_play, args=(setting, respect_dnd), daemon=True).start()
    return False
