# HyLight ("Danko Panko's HyLight") -- project context

A small desktop app that drives a physical USB LED button as a glucose indicator. It polls
the owner's Dexcom CGM through the Dexcom Share API, lights the button a color for the
current status, and serves a local settings page. The macOS version is built and in pilot
testing. **The current job is a Windows version.** The owner tests on real hardware and
reports what they see by eye; Claude cannot see the LED.

## File map

| File | Role |
|---|---|
| `dexcom_led_button.py` | Entry point + everything runtime: HID wrapper (`LedButton`), glucose loop, button gestures, `MonitorController`, single-instance lock, shutdown/relaunch. The module docstring is the detailed behavior spec -- read it. |
| `web_ui.py` | Local Flask settings UI (127.0.0.1:8765). One file, inline HTML/CSS/JS, logo embedded as base64 (`assets/logo_header_b64.txt`). Routes: `/`, `/login`, `/settings`, `/disconnect`, `/quit`, `/restart`, `/status`. |
| `config.py` | `Config`: JSON settings in `~/.dexcom_led_button/config.json`. |
| `native_loop.py` | **macOS only** (PyObjC/AppKit). Cocoa event loop so Dock>Quit and double-click-to-reopen work. |
| `tray_loop.py` | **Windows only** (pystray + Pillow). Tray icon: "Open HyLight Settings" (also left-click) and "Quit HyLight". Runs on its own thread; `main()` keeps `serve_forever()` and stops the icon in its `finally`. |
| `HyLight.spec` | **macOS** PyInstaller spec -> `HyLight.app`. |
| `tests/test_core.py` | Platform-independent tests, all against fakes. `python3 -m unittest discover -s tests -v` |
| `assets/` | `HyLight.icns` (mac), `HyLight_icon_1024.png` (source art for a Windows `.ico`; has macOS-style padding), header logo files. |
| `pilot_release/Read Me First.txt` | Tester guide that ships in the Mac zips. Keep it in sync with behavior. |

## Behavior (see module docstring for full detail)

- **Colors.** PURPLE = below 70 (fixed `LOW_THRESHOLD`). WHITE = ramping up. YELLOW = above the
  "yellow" threshold (default 150). RED = above the red threshold (default 200). GREEN = in range,
  **only ever shown by a short-press preview, never automatically**. BLUE = reminder timer done.
  Priority: purple > red > yellow > white. Automatic lights only happen inside the active window
  (default 11:30-20:00); a short press shows status for 3 s at any time.
- **Gestures.** Short tap: 3 s status preview. Long press (>=0.6 s): mute all alerts for the mute
  duration (default 30 min); long press again while muted cancels the mute; long press on blue
  dismisses the reminder. Double tap: starts a reminder timer (default 20 min), double-blue-flash
  confirmation, then solid blue.
- **Settings** (web page): active days/window, yellow & red thresholds, ramp rise size (mg/dL over
  ~15 min), mute duration, reminder delay. Saved immediately and the glucose loop is woken.
- Ramp detection needs the Dexcom trend arrow OR a rise >= the configured amount, confirmed over
  2 consecutive real samples (`RAMP_CONFIRM_CYCLES`).

## Hardware facts (hard-won -- do not re-derive)

USB HID, vendor `0xD209`, product `0x1200`. The device exposes several interfaces; **LED control and
button reads both use `interface_number == 0`**.

- **LED write:** `device.send_feature_report([0x00, enable, R, G, B])`, `enable` must be `1`
  (`enable=0` is silently ignored, even for "off"). "Off" = enable 1, RGB 0.
- **Each of R, G, B is a plain on/off switch, not a brightness.** Any nonzero value = full on. So
  only 7 colors exist: red, green, blue, yellow (R+G), purple (R+B), cyan (G+B), white (R+G+B).
  `COLORS` uses 0xFF for every channel that is on. (Cyan is the one unused color.)
- **Button read:** `device.read(64)` returns `[0x01,0,0,0]` while pressed. There is **no "released"
  report** -- it just stops sending; release is inferred from silence (`RELEASE_TIMEOUT`).
- The HID report descriptor declares **no Feature report** (only Input and a 4-byte Output). The
  Feature-report write works on macOS anyway; see the Windows risks below.
- Unfixable hardware behavior: a faint red glow remains at "off"; holding the button shows a
  built-in purple/magenta regardless of what we set (same color as the PURPLE alert); LED writes
  are unreliable while the button is held down, so previews are shown after release.
- Only one process can drive the button at a time. `LedButton` is self-healing: it never raises for
  an unplugged device, retries the open every 0.5 s, and repaints on reconnect.

## Dexcom facts

- Library: `pydexcom` 0.5.1 (Dexcom Share). Log in with the **wearer's own Dexcom account, not a
  follower's.** A follower login succeeds but returns zero readings, and
  `get_current_glucose_reading()` then returns `None` instead of raising -- so empty data is
  silent unless logged (it is: "No current reading returned by Dexcom Share API"). The wearer must
  also have Share turned on with at least one follower.
- pydexcom sets **no HTTP timeout**; the app calls `socket.setdefaulttimeout(15)`.
- Dexcom has a new reading about every 5 minutes; the app polls every 60 s.
- Credentials live only in the OS keyring (`keyring`, service `dexcom_led_button`), never in files.

## Runtime data

`~/.dexcom_led_button/` holds `config.json`, `app.lock` (PID, single-instance), `hylight.log`
(rotating). Port `8765` (not 5000: macOS AirPlay Receiver squats on it). Old config key
`orange_threshold` is migrated to `yellow_threshold` on load.

## Windows port -- what to do

**Goal:** feature parity with the Mac app (same web UI, same behavior), shipped as a double-click
Windows app (PyInstaller `--onedir`, no console, `.ico` made from `assets/HyLight_icon_1024.png`),
from the same codebase. Keep shared logic shared; isolate platform code.

**Do these in order, and stop to report after step 1:**

1. **Hardware probe first, before writing any app code.** With the button plugged in, run a small
   throwaway script (needs `pip install hidapi`): print every `hid.enumerate(0xD209, 0x1200)` entry
   (path, `interface_number`, `usage_page`, `usage`); open the interface-0 entry; try
   `send_feature_report([0, 1, 255, 0, 0])` and watch the LED; then try `write()` variants; confirm
   `read(64)` returns `[1,0,0,0]` on a press. Report exactly what works.
   - **Biggest risk (unverified hypothesis):** Windows validates feature reports against the HID
     descriptor, which declares none, so `HidD_SetFeature` may fail where macOS's lenient IOKit
     path succeeds. If so, the fallback is a raw USB control transfer: SET_REPORT,
     `bmRequestType=0x21`, `bRequest=0x09`, `wValue=0x0300`, `wIndex=0`, 4 data bytes
     `[enable, R, G, B]` (what macOS hidapi sends for report ID 0) -- via libusb/WinUSB, which
     likely needs a WinUSB driver on interface 0 (e.g. Zadig) and must not disturb the keyboard-ish
     interface 1. Decide with the owner before going down that road; it changes how the product is
     installed.
2. **Fix `_pid_is_alive` (single-instance lock).** It uses `os.kill(pid, 0)`. On Windows that call
   **terminates the target process** (signal 0 is passed to `TerminateProcess`), so it would kill
   the already-running copy. Use `psutil.pid_exists` or `ctypes` `OpenProcess`.
3. **Replace `native_loop.py` for Windows** with a system-tray icon (e.g. `pystray` + Pillow) with
   "Open HyLight Settings" and "Quit"; run the Flask server on a thread (see how
   `native_loop.run` is wired in `main()`). A second launch already works via the existing
   not-acquired-lock branch in `main()` (opens the browser, exits).
4. **Restart.** `_relaunch_app()` has a source path (`os.execv`) and a mac-bundle path (`open -n`).
   Add a Windows frozen path: `subprocess.Popen([sys.executable], creationflags=DETACHED_PROCESS |
   CREATE_NEW_PROCESS_GROUP)` after releasing the lock/port, then exit.
5. **Packaging.** New `HyLight_win.spec`; `console=False`; hiddenimports `keyring.backends.Windows`
   (Credential Manager) and the hid module; collect hidapi's DLL (`collect_all("hid")`). Unsigned
   exes trigger SmartScreen ("More info > Run anyway") and sometimes antivirus false positives;
   `--onedir` helps. Code signing is a later, paid step. Write a Windows tester guide.
6. **Smaller items.** SIGTERM isn't delivered by `taskkill`; make sure Quit via tray/web turns the
   LED off. A windowed exe has `sys.stderr is None` -- make sure `logging` doesn't depend on it
   (the file handler already writes to `CONFIG_DIR`). `signal`/`webbrowser`/`socket` code is fine.
   Test file paths with spaces and non-ASCII user names.

**Mac-only today** (don't import on Windows): `native_loop.py`, `HyLight.spec`, the `open -n`
relaunch, Keychain wording in log messages, `LSMinimumSystemVersion`/BUNDLE settings.

## How to work in this repo

- **Never test against the real keyring, real `~/.dexcom_led_button`, the real Dexcom account, or
  the real button** unless the owner says so. An early smoke test called `/disconnect` against the
  real Keychain and deleted the owner's saved login. Use `tests/test_core.py`'s pattern: patch
  `config.CONFIG_DIR` *before* importing `dexcom_led_button`, fake keyring, fake button/Dexcom.
- For a built app, test with a throwaway home dir (`USERPROFILE`/`HOME`) and
  `PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring`: launch it, hit `/status`, `/restart`,
  `/quit`, then confirm the lock file is released and no process is left behind.
- Run `python3 -m unittest discover -s tests -v` before and after changes to shared code.
- Verify before claiming: run it, look at the real output. If something can only be judged on
  the physical LED, say so and ask the owner to look.
- Don't commit or push without being asked. Keep answers short and plain.
- Dev setup: Python 3.9+, `pip install pydexcom hidapi keyring flask` (+ `pystray pillow` on Windows,
  + `pyinstaller` to build).
  Run from source: `python3 dexcom_led_button.py` (opens http://127.0.0.1:8765).

## Branding (for any new UI/assets)

Light-blue page `#E3EFF8`, white cards, red accent `#EB4438` (dark `#C22F24`), ink `#2D2320`,
primary action blue `#2D6CDF`. The logo is a red glowing sun; the header uses the wordmark image,
not text.
