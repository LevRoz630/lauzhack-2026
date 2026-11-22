#!/usr/bin/python3
"""Put an image file on the clipboard and hold it there, so Ctrl+V pastes it.

Run with the system Python (it has GTK): /usr/bin/python3 hub/clip_image.py <file.png>
Exits when something else is copied, or after ten minutes.
"""
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
clipboard.set_image(GdkPixbuf.Pixbuf.new_from_file(sys.argv[1]))
clipboard.connect("owner-change", lambda _c, event: Gtk.main_quit() if event.owner is None or event.reason != 0 else None)
GLib.timeout_add_seconds(600, Gtk.main_quit)
Gtk.main()
