"""PR Review: a native GTK4/libadwaita app that watches GitHub review requests."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GtkSource", "5")

__version__ = "0.1.0"
