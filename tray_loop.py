"""System-tray icon for HyLight on Windows (the counterpart of native_loop.py).

A windowed Windows app has no Dock and no window, so without this the only
way to reach a running HyLight would be to remember the settings URL. The
tray icon offers:

  - Left-click / "Open HyLight Settings" -> on_open() (opens the browser).
  - "Quit HyLight" -> on_quit(), which stops the web server; main() then
    runs its normal cleanup (LED off, release the lock) and exits.

Unlike the Cocoa loop, pystray's Windows backend runs happily on a
background thread, so main() keeps serve_forever() on the main thread and
its existing finally-block cleanup -- start() returns right away and
stop() removes the icon once the server has stopped.

Only imported on Windows (needs pystray + Pillow).
"""

import pystray
from PIL import Image

from web_ui import ASSETS_DIR

ICON_PATH = ASSETS_DIR / "HyLight_icon_1024.png"


def _load_image():
    image = Image.open(ICON_PATH).convert("RGBA")
    # The source art has macOS-style padding; trim it so the sun fills
    # the small tray slot.
    bbox = image.getchannel("A").getbbox()
    if bbox:
        image = image.crop(bbox)
    return image.resize((64, 64), Image.LANCZOS)


def start(on_open, on_quit):
    """Shows the tray icon on its own thread and returns it (pass it to
    stop() when the app is shutting down)."""
    menu = pystray.Menu(
        pystray.MenuItem("Open HyLight Settings", lambda icon, item: on_open(), default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit HyLight", lambda icon, item: on_quit()),
    )
    icon = pystray.Icon("HyLight", _load_image(), "HyLight", menu)
    icon.run_detached()
    return icon


def stop(icon):
    try:
        icon.stop()
    except Exception:
        pass
