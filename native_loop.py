"""Native macOS event loop for the packaged HyLight.app.

A plain Python process never answers macOS's application events, so
Dock > Quit timed out and double-clicking an already-running app did
nothing. Running a real NSApplication on the main thread fixes both:

  - Quit (Dock, the menu bar, Cmd+Q, logout) -> on_terminate() runs the
    app's normal cleanup (LED off, release the lock), then the app exits.
  - Re-opening the running app (double-click, Dock icon) -> on_reopen().

The Flask server runs on a background thread instead; when it stops
(Quit/Restart buttons on the settings page, SIGTERM), this loop is
stopped from that thread and run() returns to the caller.

Only imported by the packaged build -- running the script from source
keeps the simple blocking server loop in dexcom_led_button.main().
"""

import threading

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSEvent,
    NSEventTypeApplicationDefined,
    NSMenu,
    NSMenuItem,
    NSObject,
    NSTerminateNow,
)
from Foundation import NSPoint, NSTimer
from PyObjCTools import AppHelper

_hooks = {}


class _AppDelegate(NSObject):
    def applicationShouldTerminate_(self, sender):
        _hooks["on_terminate"]()
        return NSTerminateNow

    def applicationShouldHandleReopen_hasVisibleWindows_(self, sender, has_visible_windows):
        _hooks["on_reopen"]()
        return True

    def openSettings_(self, sender):
        _hooks["on_reopen"]()


def _build_menu(app, delegate):
    menubar = NSMenu.alloc().init()
    app_item = NSMenuItem.alloc().init()
    menubar.addItem_(app_item)
    app_menu = NSMenu.alloc().init()
    open_item = app_menu.addItemWithTitle_action_keyEquivalent_(
        "Open HyLight Settings", "openSettings:", ""
    )
    open_item.setTarget_(delegate)
    app_menu.addItem_(NSMenuItem.separatorItem())
    app_menu.addItemWithTitle_action_keyEquivalent_("Quit HyLight", "terminate:", "q")
    app_item.setSubmenu_(app_menu)
    app.setMainMenu_(menubar)


def _stop_loop():
    app = NSApplication.sharedApplication()
    app.stop_(None)
    # stop_ only takes effect once another event is processed; post a
    # dummy one so run() returns right away instead of on the next click.
    event = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
        NSEventTypeApplicationDefined, NSPoint(0, 0), 0, 0, 0, None, 0, 0, 0
    )
    app.postEvent_atStart_(event, True)


def run(serve_forever, on_terminate, on_reopen):
    """Runs serve_forever on a background thread and the Cocoa event loop
    on this (main) thread. Returns once serve_forever returns, or never
    if macOS quits the app first (on_terminate runs, then the app exits)."""
    _hooks["on_terminate"] = on_terminate
    _hooks["on_reopen"] = on_reopen

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    delegate = _AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    _build_menu(app, delegate)

    # Python-level signal handlers (SIGTERM) only run when the main
    # thread executes Python code, which it never does while parked in
    # the native run loop -- this no-op timer gives it a regular chance.
    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.5, True, lambda timer: None)

    def serve():
        try:
            serve_forever()
        finally:
            AppHelper.callAfter(_stop_loop)

    threading.Thread(target=serve, name="web-server", daemon=True).start()
    app.run()
