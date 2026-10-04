"""System-tray icon for HyLight on Windows (the counterpart of native_loop.py).

A windowed Windows app has no Dock and no window, so without this the only
way to reach a running HyLight would be to remember the settings URL. The
tray icon offers:

  - Left-click / "Open HyLight Settings" -> on_open() (opens the browser).
  - "Quit HyLight" -> on_quit(), which stops the web server; main() then
    runs its normal cleanup (LED off, release the lock) and exits.
  - `taskkill` (without /f) -> on_quit() too: it sends WM_CLOSE to the
    process's windows, and the tray's hidden window is the only one.
  - Windows shutdown / logoff -> on_session_end(), which must finish the
    cleanup itself: Windows ends the process as soon as it returns.

pystray's hidden window answers 0 to every message it doesn't handle.
For WM_QUERYENDSESSION, 0 means "don't shut down", which would put
HyLight on Windows' "this app is preventing shutdown" screen -- so it
gets an explicit "yes" here.

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

WM_CLOSE = 0x0010
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016


def _load_image():
    image = Image.open(ICON_PATH).convert("RGBA")
    # The source art has macOS-style padding; trim it so the sun fills
    # the small tray slot.
    bbox = image.getchannel("A").getbbox()
    if bbox:
        image = image.crop(bbox)
    return image.resize((64, 64), Image.LANCZOS)


def _add_window_message_handlers(icon, on_quit, on_session_end):
    def on_query_end_session(wparam, lparam):
        return 1  # yes, Windows may shut down / log off

    def on_end_session(wparam, lparam):
        if wparam:  # the session really is ending (not cancelled)
            on_session_end()
        return 0

    def on_close(wparam, lparam):
        on_quit()
        return 0

    # A private pystray attribute (Windows backend only); without it the
    # tray still works, minus these extras.
    handlers = getattr(icon, "_message_handlers", None)
    if isinstance(handlers, dict):
        handlers[WM_QUERYENDSESSION] = on_query_end_session
        handlers[WM_ENDSESSION] = on_end_session
        handlers[WM_CLOSE] = on_close


def start(on_open, on_quit, on_session_end):
    """Shows the tray icon on its own thread and returns it (pass it to
    stop() when the app is shutting down)."""
    menu = pystray.Menu(
        pystray.MenuItem("Open HyLight Settings", lambda icon, item: on_open(), default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit HyLight", lambda icon, item: on_quit()),
    )
    icon = pystray.Icon("HyLight", _load_image(), "HyLight", menu)
    _add_window_message_handlers(icon, on_quit, on_session_end)
    icon.run_detached()
    return icon


def stop(icon):
    try:
        icon.stop()
    except Exception:
        pass
