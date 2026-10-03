#!/usr/bin/env python3
"""
Dexcom-powered LED button for post-lunch glucose monitoring.

Behavior
--------
Active window (days of week + start/end time), and the YELLOW/RED
thresholds below, are all user-configurable via the settings web page
(see "Settings UI" further down) and stored in config.py. Defaults:

Active window: 11:30 AM - 8:00 PM (local time), every day.

  PURPLE  -> current glucose < 70 mg/dL (LOW_THRESHOLD, hardcoded --
             not exposed in settings)
  WHITE   -> glucose appears to be ramping up (see RAMP DETECTION below)
  YELLOW  -> current glucose > 150 mg/dL (high)
  RED     -> current glucose > 200 mg/dL (very high)

Each LED channel is simply on or off (see "LED hardware notes"), so these
are the distinct colors the hardware can make: red, yellow (R+G),
purple (R+B), white (R+G+B), plus green and blue.

Priority when more than one condition is true: PURPLE > RED > YELLOW >
WHITE (PURPLE vs. the others is moot in practice -- a reading can't be
both low and high at once). Outside the window, or when nothing is
active and no reminder is due, the LED is off.

Button gestures -- short press, long press, and double press are all
distinct and never ambiguous with each other (see button_loop):

  SHORT PRESS (a quick tap) -- anytime, this previews your current
  status for a few seconds (PREVIEW_DISPLAY_SECONDS) and then reverts
  to whatever was actually showing before. Unlike the automatic logic,
  the preview always shows something meaningful: PURPLE, RED, YELLOW,
  WHITE, or GREEN (glucose is fine -- see below). It's a pure preview
  -- it doesn't mute anything or change what the automatic logic is
  doing, and it never appears automatically, only as a direct response
  to a short press. (It shows after release rather than while held
  because this LED can't reliably update while the button is physically
  held down -- the device floods the USB connection with "pressed"
  reports the whole time it's held, and color commands sent during that
  flood come out garbled on this hardware.)

  GREEN is only ever shown by a short-press preview, never by the
  automatic in-window logic -- there's deliberately no "everything's
  fine" color while the window is automatically running, only an
  on-demand one when you ask via a press.

  LONG PRESS (hold for LONG_PRESS_THRESHOLD or more, then release) --
  mutes whatever's currently lit for the configured mute duration
  (default 30 minutes, adjustable on the settings page). Muting mutes
  ALL of purple/red/yellow/white together, not just whichever one was showing,
  since e.g. muting red but leaving yellow/white active meant the
  light would just come back on a minute later at a lower tier even
  though the reading was still just as high. A long press while
  already muted (light is off because of the mute, not because
  nothing's active) cancels the mute early and immediately shows the
  true current status (never green -- this restores the real automatic
  status, not a preview). A long press while the blue reminder is lit
  dismisses it. Otherwise (nothing lit, nothing muted) a long press
  does nothing.

  DOUBLE PRESS (two short taps close together, works any time of day,
  independent of the window above) -- after the configured reminder
  delay (default 20 minutes, adjustable on the settings page), LED
  turns blue as a reminder to check your glucose. Double-pressing again
  resets/restarts the countdown. A double press also blinks the LED
  blue twice immediately, as confirmation that the delay was
  (re)started.

If a glucose alert (purple/red/yellow/white) needs to display at the same
moment the blue reminder is due, the glucose alert wins -- the
reminder just waits and will show once the alert is muted or clears.

Unplugging/replugging the button (e.g. undocking a laptop to travel
and redocking later, while the button stays on the desk) is a routine
event, not an error: the app keeps running the whole time the button
is disconnected (Dexcom monitoring is unaffected), shows the button's
own hardware boot-default color while it's gone, and automatically
reconnects and repaints the correct color within about half a second
of being plugged back in -- no restart needed. Same if the app itself
is started while the button happens to not be plugged in yet.

Ramp (white) detection -- how this was chosen
------------------------------------------------
Looking at several weeks of your Tandem/Dexcom reports, post-lunch
rises are usually real and sustained, not noise: on a typical spike
day you go from roughly 80-130 mg/dL around noon up to 200-290 mg/dL
over the next 1-2 hours (e.g. Aug 31: 80 -> 297 by ~2:30pm, Sep 3:
129 -> 292 by early afternoon). That gives real margin to catch the
rise early without chasing every small wiggle. So this script leans
on two independent signals and requires both to agree, confirmed
across two real (not just polled) Dexcom samples ~5-10 minutes apart:

  1. Dexcom's own trend arrow is SingleUp, FortyFiveUp, or DoubleUp.
  2. A rise of >= the configured rise threshold (default 15 mg/dL) over
     roughly the last 15 minutes, as a backup for whenever the trend
     arrow is briefly unavailable.

Either signal counts, but it must hold for RAMP_CONFIRM_CYCLES (2)
consecutive real samples before the white light fires, so a single
noisy or backfilled reading can't flip the light on its own -- this is
hardcoded for now, not exposed in settings. If it's still too twitchy
or too slow after a few days of real use, the rise threshold is
adjustable on the settings page under "Ramp Detection".

LED hardware notes
-------------------
This button's HID report descriptor declares only Input (32 buttons,
4 bytes) and Output (32-bit "generic indicator", 4 bytes) reports --
no Feature report at all, despite what any vendor doc says. Through
testing, the reliable way to drive the LED turned out to be:

    device.send_feature_report([0x00, enable, r, g, b])

where byte layout is [report_id=0, enable_flag, R, G, B]. On Windows
that call fails ("Incorrect function"): Windows checks feature reports
against the descriptor, which declares none. There the same five bytes
go out as an Output report instead -- device.write([0x00, enable, r, g,
b]) -- confirmed by eye on Windows 10 (green, then off; enable=0 still
ignored; the faint red glow at "off" is still there). "Off" is
enable=1 with R=G=B=0 -- enable=0 is silently ignored by the firmware
(confirmed: it doesn't change anything, including turning the light
off), which is why every entry in COLORS below uses enable=1.

Each of R, G and B is a plain on/off switch, not a brightness: any
nonzero value lights that channel at full power. Confirmed by eye -- an
old "yellow" of (255, 255, 0) and an "orange" of (5, 64, 0) looked
identical, because both just mean "red and green on". So the only
colors this LED can make are the seven on/off combinations, and COLORS
below uses 0xFF for every channel that is on.

Two confirmed hardware limitations, NOT fixable in software -- see
led_off_diag.py, which was used to systematically try every reachable
alternative (5 different feature-report encodings including repeats,
plus the separate declared Output-report control path (device.write)
mentioned above) and none of them changed either behavior:

  1. A faint reddish glow remains visible at "off" even in the best
     encoding found. This is a genuine floor in the LED driver itself,
     not something any digital command over either report type can
     zero out.
  2. Physically holding the button down shows a bright purple/magenta,
     regardless of what color this program last set (it reproduces
     even with no program running at all). This is a built-in
     "button pressed" indicator wired independently of the RGB driver
     we can command.
  A real fix for either would need vendor documentation this device
  doesn't have, or different button hardware in a future revision.

This device also exposes multiple HID interfaces under the same
vendor/product ID (a keyboard-ish interface for button presses grouped
under interface_number 1, and a separate raw interface_number 0 used
for LED control). Opening by vendor_id/product_id alone can grab the
wrong one, so this script explicitly finds and opens the
interface_number == 0 path.

Setup
-----
1. pip install pydexcom hidapi keyring flask   (use --break-system-packages
   if your pip complains about an externally-managed environment)
2. In your Dexcom app: Settings -> Share -> turn Share on and add at
   least one follower.
3. macOS: grant your terminal app (or the app bundle, once packaged)
   Input Monitoring access under
   System Settings -> Privacy & Security -> Input Monitoring.
4. python3 dexcom_led_button.py
   This opens a settings page in your browser at
   http://127.0.0.1:8765 (not port 5000 -- macOS's own AirPlay
   Receiver listens there by default on most Macs, so a real server
   can never actually bind it). The first time it runs, that page asks
   for your Dexcom Share username/password; once verified they're
   saved to the macOS Keychain (encrypted, tied to your login) so you
   won't be asked again on future runs, and monitoring starts
   automatically. You can also skip that step entirely by exporting
   DEXCOM_USERNAME / DEXCOM_PASSWORD / DEXCOM_REGION as environment
   variables before running, which always takes priority.
   Revisit http://127.0.0.1:8765 any time to change the active window,
   days of week, or color thresholds -- changes save immediately and
   apply on the next glucose poll.

Settings UI
-----------
web_ui.py contains a small local-only (127.0.0.1) Flask app that:
  - Shows a Dexcom Share login form until credentials are connected,
    then starts monitoring automatically on a successful login.
  - Shows a settings form (active days, window start/end, yellow/red
    thresholds, and the ramp-detection knobs) once connected.
All settings are persisted via config.py to
~/.dexcom_led_button/config.json. Dexcom credentials are never written
there -- they only ever live in the OS keychain via `keyring`.
"""

import os
import signal
import subprocess
import sys
import time
import socket
import getpass
import threading
import webbrowser
import logging
from datetime import datetime
from pathlib import Path

import hid
from pydexcom import Dexcom

from config import Config, CONFIG_DIR

KEYCHAIN_SERVICE = "dexcom_led_button"

# pydexcom's requests calls carry no timeout, so a Dexcom Share server
# that stalls (no response, no error -- confirmed happening in practice)
# blocks glucose_loop's thread forever: no BG= log, no exception, state
# frozen at whatever it was on the last successful poll. A global socket
# timeout turns that silent hang into a normal, loggable, retried-next-
# cycle exception instead. Comfortably under POLL_INTERVAL_SECONDS (60s)
# so a slow-but-alive request still has room to succeed.
socket.setdefaulttimeout(15)

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
#
# The active window (days/start/end), YELLOW/RED thresholds, and the
# ramp-detection knobs are all user-configurable now -- see
# config.py and web_ui.py. What's left here are the things a customer
# never needs to touch: hardware IDs, internal timings, and colors.

VENDOR_ID = 0xD209
PRODUCT_ID = 0x1200

WEB_UI_PORT = 8765                  # NOT 5000 -- macOS's own AirPlay
                                     # Receiver listens there by default
                                     # on most Macs, so a real Flask
                                     # server can never actually bind it

POLL_INTERVAL_SECONDS = 60          # how often we *ask* Dexcom; it only
                                     # actually has new data every ~5 min
RISING_TRENDS = {"SingleUp", "DoubleUp", "FortyFiveUp"}
RAMP_CONFIRM_CYCLES = 2             # consecutive real samples required to
                                     # confirm a ramp; hardcoded for now
                                     # (not exposed in settings)
LOW_THRESHOLD = 70                  # current glucose below this -> PURPLE;
                                     # a fixed clinical low, not exposed in
                                     # settings (like RAMP_CONFIRM_CYCLES)

DOUBLE_PRESS_WINDOW = 0.5           # max gap between two short taps' releases
LONG_PRESS_THRESHOLD = 0.6          # held at least this long => long press
BUTTON_POLL_SLEEP = 0.001
RELEASE_TIMEOUT = 0.05              # seconds of silence => button released
                                     # (this device has no distinct "released"
                                     # HID report; while held it re-sends the
                                     # same "pressed" report roughly every
                                     # poll, so a gap this long only happens
                                     # after a real release)
PREVIEW_DISPLAY_SECONDS = 3.0        # how long the short-press status
                                     # preview stays lit after release

# (enable_flag, r, g, b)
COLORS = {
    "off": (0x01, 0x00, 0x00, 0x00),
    "green": (0x01, 0x00, 0xFF, 0x00),
    "red": (0x01, 0xFF, 0x00, 0x00),
    "blue": (0x01, 0x00, 0x00, 0xFF),
    "yellow": (0x01, 0xFF, 0xFF, 0x00),   # R + G
    "purple": (0x01, 0xFF, 0x00, 0xFF),   # R + B
    "white": (0x01, 0xFF, 0xFF, 0xFF),    # R + G + B
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("led-button")

# The packaged app has no visible terminal, so also keep a rolling log
# file a tester can send back when something looks wrong. Never let a
# logging problem (read-only home, full disk) stop the app itself.
try:
    from logging.handlers import RotatingFileHandler

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _file_handler = RotatingFileHandler(
        CONFIG_DIR / "hylight.log", maxBytes=1_000_000, backupCount=2
    )
    _file_handler.setFormatter(
        logging.Formatter("%(asctime)s.%(msecs)03d  %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logging.getLogger().addHandler(_file_handler)
except OSError:
    pass

# Werkzeug logs every single HTTP request at INFO level by default --
# with the status badge polling /status every few seconds, that drowns
# out the actual diagnostic signal (the BG=... line from glucose_loop)
# within moments. Only still logs its own WARNING/ERROR (startup
# issues, crashes), not routine request noise.
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# --------------------------------------------------------------------------
# HID device wrapper
# --------------------------------------------------------------------------


class LedButton:
    """Self-healing HID wrapper.

    The physical button lives on a desk and gets disconnected/reconnected
    independently of this program (e.g. undocking a laptop for travel,
    then redocking later) -- this class is built around that being a
    routine event, not an error condition. It never raises out of
    __init__, set_color(), or read_button_state() for a missing/removed
    device; instead it tracks self.connected and keeps retrying a
    throttled reopen in the background, so callers (glucose_loop,
    button_loop) just keep running and self-recover automatically once
    the device reappears -- no restart required."""

    REOPEN_RETRY_SECONDS = 0.5

    def __init__(self, vendor_id=VENDOR_ID, product_id=PRODUCT_ID):
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.lock = threading.Lock()
        self.device = None
        self.connected = False
        self._last_reopen_attempt = 0.0
        self._needs_refresh = False
        self._reopen()
        if not self.connected:
            log.warning(
                "LED button not found at startup -- is it plugged in? "
                "Will keep watching for it and connect automatically "
                "once it's plugged in."
            )

    def _find_led_interface_path(self):
        devices = hid.enumerate(self.vendor_id, self.product_id)
        target = next((d for d in devices if d.get("interface_number") == 0), None)
        if target is None:
            raise RuntimeError(
                "Could not find interface_number==0 for this device. "
                "Is the button plugged in?"
            )
        return target["path"]

    def _open(self):
        path = self._find_led_interface_path()
        d = hid.device()
        d.open_path(path)
        d.set_nonblocking(1)
        self.device = d

    def _ensure_open(self):
        if not self.connected:
            self._reopen()
        return self.connected

    def set_color(self, name):
        if not self._ensure_open():
            return
        enable, r, g, b = COLORS[name]
        try:
            with self.lock:
                report = [0x00, enable, r, g, b]
                if sys.platform == "win32":
                    self.device.write(report)
                else:
                    self.device.send_feature_report(report)
        except Exception:
            self._handle_io_failure("LED write")

    def read_button_state(self):
        """Returns "pressed" if this read caught a press report, else None
        (including whenever the button is currently disconnected).

        This device has no distinct "released" report -- while the button
        is held it keeps re-sending the same "pressed" report on roughly
        every poll, and once released it simply stops sending anything.
        Callers infer release from that silence (see RELEASE_TIMEOUT)."""
        if not self._ensure_open():
            return None
        try:
            with self.lock:
                data = self.device.read(64)
        except Exception:
            self._handle_io_failure("Button read")
            return None
        if data == [0x01, 0x00, 0x00, 0x00]:
            return "pressed"
        return None

    def _handle_io_failure(self, what):
        if self.connected:
            log.warning("%s failed -- button disconnected (unplugged?)", what)
        self.connected = False
        self._reopen()

    def _reopen(self):
        """Throttled reopen attempt; never raises. Used both for the
        initial connect and for reconnecting after a disconnect - in
        both cases self.device may already be None or stale."""
        with self.lock:
            now = time.monotonic()
            if now - self._last_reopen_attempt < self.REOPEN_RETRY_SECONDS:
                return
            self._last_reopen_attempt = now

            if self.device is not None:
                try:
                    self.device.close()
                except Exception:
                    pass
                self.device = None

            try:
                self._open()
            except Exception:
                return

        was_connected = self.connected
        self.connected = True
        if not was_connected:
            log.info("LED button connected")
            # Flagged here, not detected by callers comparing `connected`
            # across loop iterations -- a disconnect-then-reopen can
            # complete entirely inside one read/write call (e.g. right
            # after a sleep/wake cycle leaves a stale handle), so the
            # transition would otherwise never be observed as a step
            # from False to True.
            self._needs_refresh = True

    def consume_needs_refresh(self):
        """Returns True once per (re)connect, so a caller can push the
        true current color immediately instead of waiting for the next
        scheduled update. See the comment in _reopen() for why this is
        a flag rather than a before/after comparison of `connected`."""
        if self._needs_refresh:
            self._needs_refresh = False
            return True
        return False

    def close(self):
        try:
            if self.device is not None:
                self.device.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Shared state
# --------------------------------------------------------------------------


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.mute_until = None        # None = not muted; else muted until this datetime
        self.reminder_due_at = None   # when the blue light should turn on
        self.reminder_lit = False     # is blue currently on?
        self.currently_lit = "off"    # what color is actually displayed right now
        self.latest_value = None      # most recent glucose value seen (any time of day)
        self.latest_ramp_confirmed = False
        self._ramp_streak = 0
        self._last_ramp_dt = None     # datetime of last Dexcom sample we scored

    # -- glucose alert muting -----------------------------------------
    # Muting always covers all levels together (purple/red/yellow/white),
    # not just whichever one was lit -- muting red but leaving
    # yellow/white un-muted meant the light would come right back on a
    # minute later at a lower tier even though the underlying reading
    # was still just as high (or low).
    def mute_all(self, now, duration):
        with self.lock:
            self.mute_until = now + duration
        log.info("Muted all glucose alerts until %s", self.mute_until.strftime("%H:%M:%S"))

    def is_muted(self, now):
        with self.lock:
            until = self.mute_until
        return until is not None and now < until

    def clear_mute(self):
        with self.lock:
            self.mute_until = None
        log.info("Mute cleared")

    def get_mute_until(self):
        with self.lock:
            return self.mute_until

    def set_currently_lit(self, color):
        with self.lock:
            self.currently_lit = color

    def get_currently_lit(self):
        with self.lock:
            return self.currently_lit

    # -- latest reading, cached for the outside-window hold preview -----
    def set_latest_reading(self, value, ramp_confirmed):
        with self.lock:
            self.latest_value = value
            self.latest_ramp_confirmed = ramp_confirmed

    def get_latest_reading(self):
        with self.lock:
            return self.latest_value, self.latest_ramp_confirmed

    # -- reminder -------------------------------------------------------
    def arm_reminder(self, now, delay):
        with self.lock:
            self.reminder_due_at = now + delay
            self.reminder_lit = False
        log.info("Post-meal reminder armed for %s", self.reminder_due_at.strftime("%H:%M:%S"))

    def dismiss_reminder(self):
        with self.lock:
            self.reminder_due_at = None
            self.reminder_lit = False
        log.info("Reminder dismissed")

    def check_reminder_due(self, now):
        with self.lock:
            if (
                self.reminder_due_at is not None
                and not self.reminder_lit
                and now >= self.reminder_due_at
            ):
                self.reminder_lit = True
        return self.reminder_lit

    def reminder_is_lit(self):
        with self.lock:
            return self.reminder_lit

    # -- ramp streak, tied to real Dexcom samples, not poll count --------
    def bump_ramp_streak_if_new(self, reading_dt, rising):
        with self.lock:
            if reading_dt == self._last_ramp_dt:
                return self._ramp_streak  # no new sample since last check
            self._last_ramp_dt = reading_dt
            if rising:
                self._ramp_streak += 1
            else:
                self._ramp_streak = 0
            return self._ramp_streak


state = State()


# --------------------------------------------------------------------------
# Glucose polling
# --------------------------------------------------------------------------


def in_window(now, cfg):
    return (
        now.weekday() in cfg.get_active_days()
        and cfg.get_window_start() <= now.time() <= cfg.get_window_end()
    )


def detect_ramp(dexcom, cfg):
    """Returns True once a rise has been confirmed across two real samples."""
    try:
        readings = dexcom.get_glucose_readings(minutes=20, max_count=4)
    except Exception:
        log.exception("Failed to fetch glucose history for ramp check")
        return False

    if not readings:
        return False

    latest = readings[0]  # newest first
    trend_rising = getattr(latest, "trend_direction", None) in RISING_TRENDS

    magnitude_rising = False
    if len(readings) >= 3:
        magnitude_rising = (latest.value - readings[2].value) >= cfg.get_ramp_magnitude()

    rising_now = trend_rising or magnitude_rising
    streak = state.bump_ramp_streak_if_new(latest.datetime, rising_now)
    return streak >= RAMP_CONFIRM_CYCLES


def compute_status_color(value, ramp_confirmed, cfg):
    """Same PURPLE/RED/YELLOW/WHITE priority as the automatic logic, but
    with no mute applied -- used to restore the true automatic status,
    e.g. when a long press cancels a mute early. Returns "off" (not
    "green") when nothing applies, matching what the automatic in-window
    logic would show on its own."""
    if value is None:
        return "off"
    if value < LOW_THRESHOLD:
        return "purple"
    if value > cfg.get_red_threshold():
        return "red"
    if value > cfg.get_yellow_threshold():
        return "yellow"
    if ramp_confirmed:
        return "white"
    return "off"


def compute_preview_color(value, ramp_confirmed, cfg):
    """Same as compute_status_color, but for the short-press,
    outside-window preview, which should always show something
    meaningful rather than "off" -- green means everything's currently
    fine. Never shown automatically, only ever as a direct response to
    a short press."""
    color = compute_status_color(value, ramp_confirmed, cfg)
    return "green" if color == "off" else color


def _sleep_or_wake(wake_event, seconds):
    """Sleeps up to `seconds`, but returns immediately if wake_event is
    set first (used so saving settings doesn't have to wait out the
    rest of the current poll interval to take visible effect)."""
    if wake_event.wait(seconds):
        wake_event.clear()


def glucose_loop(dexcom, button, cfg, wake_event, stop_event):
    while not stop_event.is_set():
        now = datetime.now()
        try:
            reading = dexcom.get_current_glucose_reading()
        except Exception:
            log.exception("Failed to fetch current glucose reading")
            _sleep_or_wake(wake_event, POLL_INTERVAL_SECONDS)
            continue

        value = reading.value if reading else None

        # Reminder timer runs independent of the active window.
        state.check_reminder_due(now)

        active_color = None
        ramp_confirmed = False
        red_threshold = cfg.get_red_threshold()
        yellow_threshold = cfg.get_yellow_threshold()
        if value is not None and in_window(now, cfg):
            ramp_confirmed = detect_ramp(dexcom, cfg)
            state.set_latest_reading(value, ramp_confirmed)

            if not state.is_muted(now):
                if value < LOW_THRESHOLD:
                    active_color = "purple"
                elif value > red_threshold:
                    active_color = "red"
                elif value > yellow_threshold:
                    active_color = "yellow"
                elif ramp_confirmed:
                    active_color = "white"

            log.info(
                "BG=%s trend=%s ramp_confirmed=%s -> %s",
                value,
                getattr(reading, "trend_direction", "?"),
                ramp_confirmed,
                active_color,
            )
        elif value is not None:
            # Ramp detection is window-specific, but the raw value is still
            # cached so the outside-window preview can show red/yellow
            # based on the true current reading.
            state.set_latest_reading(value, False)
            log.info("BG=%s (outside active window)", value)
        else:
            # Dexcom's Share API can return a 200 with no reading at all
            # (empty publisher data) rather than raising -- e.g. Share
            # turned off, or no follower attached to the account. That
            # used to be silently indistinguishable from the process
            # being dead, since nothing logged in this branch before.
            log.info("No current reading returned by Dexcom Share API")

        if active_color:
            display_color = active_color
        elif state.reminder_is_lit():
            display_color = "blue"
        else:
            display_color = "off"

        button.set_color(display_color)
        state.set_currently_lit(display_color)

        _sleep_or_wake(wake_event, POLL_INTERVAL_SECONDS)


# --------------------------------------------------------------------------
# Button handling (short / long / double press)
# --------------------------------------------------------------------------


def handle_long_press(now, button, cfg):
    """A long press mutes whatever's currently lit (all levels together,
    see mute_all) for the configured mute duration (default 30 minutes),
    or dismisses the blue reminder. A long press while already muted
    cancels the mute early and immediately restores the true current
    status."""
    lit = state.get_currently_lit()
    if lit in ("white", "yellow", "red", "purple"):
        state.mute_all(now, cfg.get_mute_duration())
        button.set_color("off")
        state.set_currently_lit("off")
    elif lit == "blue":
        state.dismiss_reminder()
        button.set_color("off")
        state.set_currently_lit("off")
    elif state.is_muted(now):
        # Nothing's currently lit because an alert is muted, not because
        # there's nothing to show -- a press here cancels the mute and
        # immediately brings the light back rather than waiting out the
        # rest of the mute duration.
        state.clear_mute()
        if in_window(now, cfg):
            value, ramp_confirmed = state.get_latest_reading()
            color = compute_status_color(value, ramp_confirmed, cfg)
            button.set_color(color)
            state.set_currently_lit(color)
    # otherwise ("off", nothing muted), a long press does nothing


def handle_double_press(now, button, cfg):
    state.arm_reminder(now, cfg.get_reminder_delay())
    flash_double_press_confirmation(button)
    # The LED otherwise doesn't change - it lights up blue once the
    # configured delay elapses, handled by glucose_loop's normal poll cycle.


def flash_double_press_confirmation(button, blinks=2, on_seconds=0.15, off_seconds=0.15):
    """Blink blue briefly to confirm the reminder delay was (re)started,
    then restore whatever color is actually supposed to be showing."""
    restore_color = state.get_currently_lit()
    for _ in range(blinks):
        button.set_color("blue")
        time.sleep(on_seconds)
        button.set_color("off")
        time.sleep(off_seconds)
    button.set_color(restore_color)


def button_loop(button, cfg):
    is_down = False
    press_down_time = 0.0      # monotonic time of the current press's down edge
    last_pressed_seen = 0.0    # monotonic time of the most recent "pressed" report
    pending_short_tap = None   # monotonic release time of an unconfirmed short tap
    preview_off_at = None      # monotonic deadline to end the outside-window preview

    while True:
        report = button.read_button_state()
        now_monotonic = time.monotonic()
        now = datetime.now()

        if button.consume_needs_refresh():
            # Just (re)connected after being unplugged -- push the true
            # current color right away rather than waiting for the next
            # glucose poll (up to POLL_INTERVAL_SECONDS away).
            button.set_color(state.get_currently_lit())

        if report == "pressed":
            last_pressed_seen = now_monotonic
            if not is_down:
                # Down edge.
                is_down = True
                press_down_time = now_monotonic

        elif is_down and now_monotonic - last_pressed_seen > RELEASE_TIMEOUT:
            # Up edge, inferred from the "pressed" report stream going quiet.
            is_down = False
            held_duration = last_pressed_seen - press_down_time

            if held_duration >= LONG_PRESS_THRESHOLD:
                log.info("Long press detected")
                handle_long_press(now, button, cfg)
            else:
                # Short tap always previews the current status (a pure
                # preview -- it doesn't mute anything or touch
                # state.currently_lit), including GREEN to confirm an
                # in-range reading during the active window, where the
                # automatic logic never lights anything on its own.
                # This LED can't reliably update while the button is
                # actively flooding the bus with "pressed" reports, so
                # the preview is shown after release instead of during
                # the hold: fixed duration, then back to whatever's
                # actually supposed to be showing.
                value, ramp_confirmed = state.get_latest_reading()
                preview_color = compute_preview_color(value, ramp_confirmed, cfg)
                button.set_color(preview_color)
                preview_off_at = now_monotonic + PREVIEW_DISPLAY_SECONDS

                # Checked against other short taps only (a long press
                # can never be mistaken for one half of a double press,
                # since it's filtered out above before reaching here).
                if pending_short_tap is not None and (
                    now_monotonic - pending_short_tap <= DOUBLE_PRESS_WINDOW
                ):
                    pending_short_tap = None
                    log.info("Double press detected")
                    handle_double_press(now, button, cfg)
                else:
                    pending_short_tap = now_monotonic

        if (
            preview_off_at is not None
            and not is_down
            and now_monotonic >= preview_off_at
        ):
            # Revert to whatever the automatic logic actually has showing
            # right now -- off/blue outside the window (as before), but
            # also purple/red/yellow/white if the preview interrupted that
            # during the active window.
            button.set_color(state.get_currently_lit())
            preview_off_at = None

        time.sleep(BUTTON_POLL_SLEEP)


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def _prompt_for_credentials(prefill_username=None):
    """Ask for Dexcom Share credentials in the terminal (password
    input is hidden). A GUI dialog (tkinter) was tried here first, but
    the Tcl/Tk framework bundled with this Python install hard-crashes
    on some macOS point releases before Python can even catch the
    error, so a plain terminal prompt is the reliable option."""
    if prefill_username:
        prompt = f"Dexcom Share username [{prefill_username}]: "
    else:
        prompt = "Dexcom Share username: "
    username = input(prompt).strip() or prefill_username
    password = getpass.getpass("Dexcom Share password: ")
    return username, password


def _get_keyring():
    try:
        import keyring
        return keyring
    except ImportError:
        log.info("`keyring` not installed - credentials won't be saved to "
                  "Keychain. Run: pip3 install keyring")
        return None


def get_credentials(interactive=True):
    """Credential lookup order: environment variables -> macOS Keychain
    -> terminal prompt. Returns (username, password, region, from_prompt)
    - from_prompt is True only when the user just typed it, so the
    caller can decide whether to save it (only after a successful
    login, so a typo never gets permanently stuck in the Keychain).

    With interactive=False, the terminal prompt is skipped entirely and
    (None, None, region, False) is returned if nothing is found -- used
    by the normal app startup, which asks for credentials via the
    settings web page instead (customers won't have a terminal open)."""
    username = os.environ.get("DEXCOM_USERNAME")
    password = os.environ.get("DEXCOM_PASSWORD")
    region = os.environ.get("DEXCOM_REGION", "us")

    if username and password:
        return username, password, region, False

    keyring = _get_keyring()

    if keyring is not None:
        username = username or keyring.get_password(KEYCHAIN_SERVICE, "username")
        password = password or keyring.get_password(KEYCHAIN_SERVICE, "password")

    if username and password:
        return username, password, region, False

    if not interactive:
        return None, None, region, False

    username, password = _prompt_for_credentials(prefill_username=username)
    return username, password, region, True


def save_credentials_to_keychain(username, password):
    keyring = _get_keyring()
    if keyring is not None:
        keyring.set_password(KEYCHAIN_SERVICE, "username", username)
        keyring.set_password(KEYCHAIN_SERVICE, "password", password)
        log.info("Saved credentials to macOS Keychain for next time.")


def clear_saved_credentials():
    keyring = _get_keyring()
    if keyring is None:
        return
    for key in ("username", "password"):
        try:
            keyring.delete_password(KEYCHAIN_SERVICE, key)
        except Exception:
            pass


# --------------------------------------------------------------------------
# Monitoring lifecycle (used by the settings web UI)
# --------------------------------------------------------------------------


class MonitorController:
    """Owns starting the glucose/button threads once Dexcom credentials
    are available. Safe to call start() more than once - only the first
    call (while not already running) does anything."""

    def __init__(self, button, cfg):
        self.button = button
        self.cfg = cfg
        self._lock = threading.Lock()
        self._running = False
        self.dexcom_username = None
        self._settings_changed = threading.Event()
        self._stop_glucose_loop = threading.Event()
        self.web_server = None  # set once main() constructs it
        self._restart_requested = False
        self._pending_quit_timer = None

    def is_running(self):
        with self._lock:
            return self._running

    def get_status(self):
        """For the settings page's connection indicator. button_connected
        reflects the physical USB/HID link and is meaningful even before
        a Dexcom login (LedButton exists from process startup); the
        rest only means something once monitoring has actually started.
        The diagnostic fields (latest_value etc.) exist so what the app
        actually believes is checkable without needing terminal access
        to the process -- the gap that made a recent "why won't it light
        up" bug hard to diagnose remotely."""
        monitoring = self.is_running()
        now = datetime.now()
        latest_value, latest_ramp_confirmed = state.get_latest_reading()
        mute_until = state.get_mute_until()
        return {
            "button_connected": self.button.connected,
            "monitoring": monitoring,
            "currently_lit": state.get_currently_lit() if monitoring else None,
            "server_time": now.isoformat(timespec="seconds"),
            "in_active_window": in_window(now, self.cfg) if monitoring else None,
            "latest_value": latest_value,
            "latest_ramp_confirmed": latest_ramp_confirmed,
            "yellow_threshold": self.cfg.get_yellow_threshold(),
            "red_threshold": self.cfg.get_red_threshold(),
            "muted": state.is_muted(now),
            "muted_until": mute_until.isoformat(timespec="seconds") if mute_until else None,
        }

    def notify_settings_changed(self):
        """Wakes glucose_loop immediately instead of leaving it to wait
        out the rest of the current poll interval (up to
        POLL_INTERVAL_SECONDS) before a saved settings change takes
        visible effect on the LED."""
        self._settings_changed.set()

    def request_shutdown(self, delay_seconds=0):
        """Stops the web server's serve_forever() loop from within a
        request handler (safe to call from the request thread -- see
        werkzeug.serving.BaseWSGIServer.shutdown()). Once serve_forever()
        returns, main()'s own finally blocks take care of turning the LED
        off and releasing the single-instance lock, so there's no cleanup
        duplicated here.

        A nonzero delay_seconds keeps the server up a bit longer after
        Quit is confirmed, so the quit page it just served stays
        responsive long enough for its "Restart HyLight instead" button
        to still work -- that button calls request_restart(), which
        cancels this pending timer."""
        if self.web_server is None:
            return
        if delay_seconds:
            self._pending_quit_timer = threading.Timer(delay_seconds, self.web_server.shutdown)
            self._pending_quit_timer.daemon = True
            self._pending_quit_timer.start()
        else:
            threading.Thread(target=self.web_server.shutdown, daemon=True).start()

    def request_restart(self):
        """Like request_shutdown(), but main() re-execs the process
        afterward instead of exiting for good -- see
        consume_restart_requested(). Cancels a pending delayed quit from
        request_shutdown(delay_seconds=...), since restarting supersedes
        it."""
        if self._pending_quit_timer is not None:
            self._pending_quit_timer.cancel()
        self._restart_requested = True
        self.request_shutdown()

    def consume_restart_requested(self):
        was = self._restart_requested
        self._restart_requested = False
        return was

    def start(self, username, password, region):
        """Verifies the credentials against Dexcom, saves them to the
        Keychain, and starts monitoring. Raises whatever pydexcom raises
        if the login is invalid, and does NOT save or start in that case."""
        with self._lock:
            if self._running:
                return
            dexcom = Dexcom(username=username, password=password, region=region)

            save_credentials_to_keychain(username, password)
            self.cfg.update(dexcom_region=region)
            self.dexcom_username = username

            self.button.set_color("off")
            self._stop_glucose_loop.clear()
            threading.Thread(
                target=glucose_loop,
                args=(dexcom, self.button, self.cfg, self._settings_changed, self._stop_glucose_loop),
                daemon=True,
            ).start()
            threading.Thread(
                target=button_loop, args=(self.button, self.cfg), daemon=True
            ).start()
            self._running = True
            log.info("Monitoring started for %s", username)

    def disconnect(self):
        """Stops the glucose-polling thread and forgets the logged-in
        session, so / falls back to the login screen. Credentials
        themselves are cleared from the Keychain by the /disconnect
        route, separately -- this only concerns the in-memory session.
        button_loop is left running (it only reads cached state/cfg, not
        the dexcom object) so the button stays responsive."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self.dexcom_username = None
            self._stop_glucose_loop.set()
            self._settings_changed.set()  # wake it immediately instead of waiting out the poll interval
        state.set_latest_reading(None, False)
        state.set_currently_lit("off")
        self.button.set_color("off")
        log.info("Disconnected from Dexcom")


# --------------------------------------------------------------------------
# Single-instance lock
# --------------------------------------------------------------------------
#
# The LED button can only be held open by one process at a time, and a
# raw hid.pyx OSError traceback when a second copy tries to start is a
# confusing way for a customer to learn that. A small PID lock file
# turns that into a clear, actionable message instead. A stale lock left
# behind by a process that didn't exit cleanly (crash, force-quit,
# power loss) is detected via a liveness check and reused automatically.

LOCK_PATH = CONFIG_DIR / "app.lock"


def _pid_is_alive(pid):
    # Never os.kill(pid, 0) on Windows: there it terminates the process.
    if sys.platform == "win32":
        return _pid_is_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    return True


def _pid_is_alive_windows(pid):
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_ACCESS_DENIED = 5
    STILL_ACTIVE = 259
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Access denied means it exists but belongs to someone else.
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        # A handle can outlive the process; only STILL_ACTIVE means running.
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def _acquire_single_instance_lock():
    """Returns (True, None) if this process now holds the lock, or
    (False, existing_pid) if another live instance already holds it."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            existing_pid = int(LOCK_PATH.read_text().strip())
        except (ValueError, OSError):
            existing_pid = None
        if existing_pid and existing_pid != os.getpid() and _pid_is_alive(existing_pid):
            return False, existing_pid
    LOCK_PATH.write_text(str(os.getpid()))
    return True, None


def _release_single_instance_lock():
    try:
        if LOCK_PATH.exists() and int(LOCK_PATH.read_text().strip()) == os.getpid():
            LOCK_PATH.unlink()
    except (OSError, ValueError):
        pass


# --------------------------------------------------------------------------
# Shutdown and relaunch
# --------------------------------------------------------------------------

_resources_lock = threading.Lock()
_resources_released = False


def _shutdown_resources(button=None, server=None):
    """Turns the LED off, frees the USB device and the web port, and
    releases the single-instance lock. Idempotent, because with the
    native macOS event loop there are two ways to reach it: main()'s
    finally block (Quit/Restart buttons, SIGTERM) and the loop's own
    terminate handler (Dock > Quit, Cmd+Q, logout), which exits the
    process without ever unwinding back into main()."""
    global _resources_released
    with _resources_lock:
        if _resources_released:
            return
        _resources_released = True
    if button is not None:
        button.set_color("off")
        button.close()
    if server is not None:
        server.server_close()
    _release_single_instance_lock()


def _stop_web_server(server, timeout=3.0):
    """Stops serve_forever() from a thread that isn't serving requests
    (shutdown() blocks until the loop exits, and would hang forever if
    the loop never started -- hence the bounded wait)."""
    stopper = threading.Thread(target=server.shutdown, daemon=True)
    stopper.start()
    stopper.join(timeout)


def _relaunch_app():
    """Starts a fresh copy of the app once this process has released
    everything (used by the settings page's Restart button)."""
    bundle = None
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        candidate = Path(sys.executable).resolve().parents[2]
        if candidate.suffix == ".app":
            bundle = candidate

    if bundle is None:
        # Running from source: re-exec in place.
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # Packaged app: exec-ing in place isn't safe once Cocoa is running, so
    # go through LaunchServices like a double-click does. `open` doesn't
    # inherit our environment, so forward the few variables that change
    # where this app keeps its data or how it behaves. The pause lets this
    # process finish exiting first.
    env_args = []
    for name in ("HOME", "PYTHON_KEYRING_BACKEND", "BROWSER"):
        if name in os.environ:
            env_args += ["--env", f"{name}={os.environ[name]}"]
    subprocess.Popen(
        ["/bin/sh", "-c", 'sleep 1; exec "$@"', "sh", "/usr/bin/open", "-n", *env_args, str(bundle)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _start_tray(url, controller):
    """Windows: shows the tray icon (Open settings / Quit). Returns the
    icon, or None if the tray couldn't be started -- the app still runs,
    reachable through its settings page."""
    try:
        import tray_loop

        def _on_open():
            log.info("Tray: opening the settings page")
            webbrowser.open(url)

        def _on_quit():
            log.info("Quit requested from the tray icon")
            controller.request_shutdown()

        return tray_loop.start(_on_open, _on_quit)
    except Exception:
        log.warning("Couldn't show the tray icon -- the settings page still works.", exc_info=True)
        return None


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main():
    acquired, existing_pid = _acquire_single_instance_lock()
    if not acquired:
        # Relaunching (e.g. double-clicking the app icon again, or
        # re-running the script) is the natural "something looks wrong,
        # let me pull the app back up" gesture -- especially for the
        # packaged app, where stdout isn't visible anywhere, a bare error
        # message here would just silently do nothing from the user's
        # point of view. Reopen the already-running instance's settings
        # page instead of just refusing.
        url = f"http://127.0.0.1:{WEB_UI_PORT}"
        webbrowser.open(url)
        sys.exit(
            f"dexcom_led_button is already running (process {existing_pid}) -- "
            f"reopened its settings page at {url} instead of starting a "
            "second copy (only one copy can use the LED button at a time)."
        )

    controller = None
    button = None
    server = None
    tray_icon = None
    try:
        cfg = Config.load()

        # LedButton is self-healing -- it never raises here even if the
        # button isn't plugged in yet; it logs a warning and keeps
        # retrying in the background (see LedButton's docstring).
        button = LedButton()
        controller = MonitorController(button, cfg)

        username, password, region, _ = get_credentials(interactive=False)
        if username and password:
            try:
                controller.start(username, password, region)
            except Exception:
                log.warning(
                    "Login failed using saved/environment credentials. "
                    "Clearing any saved Keychain entry -- reconnect via "
                    "the settings page."
                )
                clear_saved_credentials()

        from web_ui import create_app
        from werkzeug.serving import make_server

        app = create_app(cfg, controller)
        try:
            server = make_server("127.0.0.1", WEB_UI_PORT, app)
        except OSError:
            sys.exit(
                f"Could not start the settings web server on port {WEB_UI_PORT} "
                "-- something else on this Mac is already using it. If another "
                "copy of this app is somehow still running, quit it first."
            )
        # Exposed so the settings page's Quit button can stop the
        # server gracefully (see MonitorController.request_shutdown)
        # -- once serve_forever() returns below, the finally block
        # turns the LED off and releases the lock.
        controller.web_server = server

        # `kill`/`killall` (SIGTERM) would otherwise end the process
        # instantly, skipping the cleanup below and leaving the LED
        # lit on whatever color it last showed.
        def _handle_sigterm(signum, frame):
            log.info("Received SIGTERM -- shutting down")
            controller.request_shutdown()

        signal.signal(signal.SIGTERM, _handle_sigterm)

        url = f"http://127.0.0.1:{WEB_UI_PORT}"
        webbrowser.open(url)
        log.info("Settings UI at %s -- Ctrl+C to stop.", url)

        native_loop = None
        if sys.platform == "darwin" and getattr(sys, "frozen", False):
            try:
                import native_loop
            except ImportError:
                log.warning(
                    "Cocoa bindings unavailable -- Dock > Quit and "
                    "double-click-to-reopen won't work in this build."
                )

        if native_loop is not None:
            def _on_native_terminate():
                log.info("Quit requested by macOS (Dock, menu bar or logout)")
                _stop_web_server(server)
                _shutdown_resources(button, server)

            def _on_native_reopen():
                log.info("App reopened -- opening the settings page")
                threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

            native_loop.run(server.serve_forever, _on_native_terminate, _on_native_reopen)
        else:
            if sys.platform == "win32":
                tray_icon = _start_tray(url, controller)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if tray_icon is not None:
            # Its thread isn't a daemon: the process can't exit until it stops.
            import tray_loop
            tray_loop.stop(tray_icon)
        restart = controller is not None and controller.consume_restart_requested()
        _shutdown_resources(button, server)
        if restart:
            _relaunch_app()


if __name__ == "__main__":
    main()
