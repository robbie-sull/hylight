"""Local-only (127.0.0.1) settings web app for the LED button.

Two states:
  - Not connected yet: "/" shows a Dexcom Share login form. Submitting
    it validates the credentials against Dexcom, saves them to the
    Keychain, and starts monitoring.
  - Connected: "/" shows the settings form (active window, days,
    thresholds, mute duration). Saving persists to config.py's Config
    and takes effect on the next glucose poll / next press.

Kept dependency-free beyond Flask itself (no template files, no
external CSS/JS) since this only ever needs to work on localhost.

Branding follows the Danko Panko's HyLight mark (v2): a light blue
page background (#E3EFF8, matching the app icon), near-black warm
brown for text (#2D2320), and the sun mark's red (#EB4438) as the
single accent for both primary and destructive actions.
"""

import logging
import sys
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template_string, request, url_for

from config import DAY_NAMES, MUTE_DURATION_CHOICES_MINUTES, REMINDER_DELAY_CHOICES_MINUTES

# The real logo artwork (wordmark + sun), background-removed and
# base64-inlined so the rendered page stays a single dependency-free
# file (see assets/ for the source images and logo_header.png for the
# exact cropped/quantized PNG actually used here). A frozen PyInstaller
# build extracts bundled data files under sys._MEIPASS, not next to
# this file's own __file__ -- see the `datas` entry in HyLight.spec
# that puts this same relative path there.
if getattr(sys, "frozen", False):
    ASSETS_DIR = Path(sys._MEIPASS) / "assets"
else:
    ASSETS_DIR = Path(__file__).parent / "assets"
with open(ASSETS_DIR / "logo_header_b64.txt") as _f:
    LOGO_HEADER_B64 = _f.read().strip()

# Small inline glucose-trace icons shown next to a couple of section
# headers -- trusted, developer-authored markup (rendered with |safe).
ICON_THRESHOLDS = """
<svg width="44" height="30" viewBox="0 0 52 36" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <line x1="2" y1="14" x2="50" y2="14" stroke="#EB4438" stroke-width="2" stroke-dasharray="4 3"/>
  <polyline points="4,28 16,24 26,10 38,6 48,9" stroke="#2D2320" stroke-width="2" fill="none"
            stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="4" cy="28" r="3" fill="#2FAE66"/>
  <circle cx="16" cy="24" r="3" fill="#2FAE66"/>
  <circle cx="26" cy="10" r="3" fill="#EB4438"/>
  <circle cx="38" cy="6" r="3" fill="#EB4438"/>
  <circle cx="48" cy="9" r="3" fill="#EB4438"/>
</svg>
"""

# A mostly-flat trace that only just starts climbing at the very end --
# this is what the product is meant to catch (the beginning of a rise),
# not an already-dramatic climb.
ICON_RAMP = """
<svg width="44" height="30" viewBox="0 0 52 36" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <polyline points="4,26 16,25 26,24 36,15 46,7" stroke="#2D2320" stroke-width="2" fill="none"
            stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="4" cy="26" r="3" fill="#2FAE66"/>
  <circle cx="16" cy="25" r="3" fill="#2FAE66"/>
  <circle cx="26" cy="24" r="3" fill="#2FAE66"/>
  <circle cx="36" cy="15" r="3" fill="#fff" stroke="#2D2320" stroke-width="1.5"/>
  <circle cx="46" cy="7" r="3" fill="#fff" stroke="#2D2320" stroke-width="1.5"/>
  <path d="M46 7 L41 8.5 M46 7 L44.5 12" stroke="#2D2320" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""

BRAND_HEAD = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HyLight Settings</title>
<style>
  :root {
    --ink: #2D2320;
    --ink-dark: #1A1412;
    --red: #EB4438;
    --red-dark: #C22F24;
    --blue: #2D6CDF;
    --blue-dark: #1F4FB0;
    --bg: #E3EFF8;
    --card: #FFFFFF;
    --border: #CBDCEA;
    --text: #2D2320;
    --muted: #7C8B98;
  }
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  body {
    font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    max-width: 620px;
    margin: 0 auto;
    padding: 32px 20px 60px;
  }
  .brand { margin-bottom: 28px; }
  .brand-logo { display: block; height: 80px; width: auto; }
  h2 { font-size: 1.05rem; font-weight: 700; margin-top: 2rem; color: var(--ink); }
  fieldset {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 18px 22px;
    margin-bottom: 20px;
    min-width: 0;
    scroll-margin-top: 20px;
  }
  legend {
    max-width: 100%;
    padding: 0 8px;
    color: var(--ink);
    font-weight: 700;
    font-size: 0.95rem;
    white-space: normal;
    box-sizing: border-box;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  legend svg { flex: none; }
  label { display: block; margin: 10px 0 4px; font-size: 0.9rem; color: var(--text); }
  input[type=text], input[type=password], input[type=number], input[type=time],
  select {
    padding: 8px 10px;
    font-size: 0.95rem;
    border: 1px solid var(--border);
    border-radius: 9px;
    width: 100%;
    box-sizing: border-box;
    background: #fff;
    color: var(--text);
  }
  input:focus, select:focus { outline: 2px solid var(--red); outline-offset: 1px; }
  .days { display: flex; gap: 12px; flex-wrap: wrap; }
  .days label { display: flex; align-items: center; gap: 4px; font-size: 0.9rem; margin: 0; }
  .days input { width: auto; }
  .row { display: flex; gap: 16px; flex-wrap: wrap; }
  .row > div { flex: 1 1 140px; min-width: 0; }
  button {
    margin-top: 20px;
    padding: 11px 20px;
    font-size: 0.95rem;
    font-weight: 600;
    border: none;
    border-radius: 10px;
    background: var(--red);
    color: white;
    cursor: pointer;
  }
  button:hover { background: var(--red-dark); }
  button.btn-primary { background: var(--blue); }
  button.btn-primary:hover { background: var(--blue-dark); }
  .error {
    background: #FBE7E4; color: var(--red-dark); padding: 10px 14px;
    border-radius: 10px; margin-bottom: 16px; border: 1px solid #F0C4BC;
  }
  .success {
    background: #E3F1E8; color: #1E6B3C; padding: 10px 14px;
    border-radius: 10px; margin-bottom: 16px; border: 1px solid #BFE3CC;
  }
  .hint { color: var(--muted); font-size: 0.82rem; margin-top: 4px; }
  .status { color: var(--muted); font-size: 0.9rem; margin-top: -6px; }

  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 18px 22px;
    margin-bottom: 20px;
  }
  .card-title { margin: 0 0 14px; color: var(--ink); font-weight: 700; font-size: 0.95rem; }
  .press-guide { display: flex; flex-direction: column; gap: 6px; }
  a.press-demo {
    text-decoration: none;
    color: inherit;
    cursor: pointer;
  }
  .press-demo {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 10px 14px;
    margin: 0 -14px;
    border-radius: 10px;
    border: 1px solid transparent;
    transition: background-color 0.15s ease, border-color 0.15s ease;
  }
  .press-demo:hover { background: #EDF1F8; border-color: var(--border); }
  .press-demo-btn {
    position: relative;
    flex: none;
    width: 46px;
    height: 46px;
    border-radius: 50%;
    background: #E4E8EE;
    border: 2px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .press-demo-led {
    width: 20px;
    height: 20px;
    border-radius: 50%;
    background: #CBD2DC;
  }
  .press-demo-btn::after {
    content: "";
    position: absolute;
    inset: -6px;
    border-radius: 50%;
    border: 2px solid var(--red);
    opacity: 0;
  }
  .press-demo-label strong { display: block; color: var(--ink); font-size: 0.9rem; }
  .press-demo-label p { margin: 2px 0 0; font-size: 0.82rem; color: var(--muted); }

  /* Animations only run while hovering that row -- restarting fresh each
     time (rather than pausing/resuming mid-cycle) so it's never confusing
     which gesture is being demonstrated, and only one plays at once. */
  @keyframes ledShortFlash {
    0%, 15% { background: #CBD2DC; box-shadow: none; }
    28%, 58% { background: #2FAE66; box-shadow: 0 0 10px #2FAE66; }
    75%, 100% { background: #CBD2DC; box-shadow: none; }
  }
  .press-demo:hover .led-short { animation: ledShortFlash 1.4s ease-in-out infinite; }

  @keyframes ledLongHold {
    0%, 12% { background: #CBD2DC; box-shadow: none; }
    28%, 78% { background: var(--red); box-shadow: 0 0 10px var(--red); }
    92%, 100% { background: #CBD2DC; box-shadow: none; }
  }
  .press-demo:hover .led-long { animation: ledLongHold 2.2s ease-in-out infinite; }
  @keyframes ringLongHold {
    0%, 12% { transform: scale(0.7); opacity: 0; }
    22% { opacity: 0.7; }
    78% { transform: scale(1.25); opacity: 0.7; }
    92%, 100% { transform: scale(1.25); opacity: 0; }
  }
  .press-demo:hover .ring-long::after { animation: ringLongHold 2.2s ease-in-out infinite; }

  @keyframes ledDoubleFlash {
    0%, 8% { background: #CBD2DC; box-shadow: none; }
    18%, 30% { background: #3B82F6; box-shadow: 0 0 10px #3B82F6; }
    40%, 48% { background: #CBD2DC; box-shadow: none; }
    58%, 70% { background: #3B82F6; box-shadow: 0 0 10px #3B82F6; }
    82%, 100% { background: #CBD2DC; box-shadow: none; }
  }
  .press-demo:hover .led-double { animation: ledDoubleFlash 1.6s ease-in-out infinite; }

  .button-status {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: 0.85rem;
    color: var(--muted);
    margin-bottom: 20px;
    padding: 6px 14px;
    border-radius: 999px;
    background: #EEF1F6;
    border: 1px solid var(--border);
  }
  .status-dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: #9AA3B2;
    flex: none;
  }
  .status-dot.connected { background: #2FAE66; box-shadow: 0 0 6px #2FAE66; }
  .status-dot.disconnected { background: var(--red); }
</style>
</head>
<body>

<div class="brand">
  <img class="brand-logo" src="data:image/png;base64,__LOGO_HEADER_B64__" alt="Danko Panko's HyLight">
</div>

<div class="button-status" id="button-status">
  <span class="status-dot" id="status-dot"></span>
  <span id="status-text">Checking button...</span>
</div>
"""
BRAND_HEAD = BRAND_HEAD.replace("__LOGO_HEADER_B64__", LOGO_HEADER_B64)

BRAND_FOOT = """
<script>
(function () {
  var dot = document.getElementById("status-dot");
  var text = document.getElementById("status-text");

  function fmtTime(iso) {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
    } catch (e) {
      return iso;
    }
  }

  function refresh() {
    fetch("/status")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.button_connected) {
          dot.className = "status-dot disconnected";
          text.textContent = "Button not detected — check it's plugged in";
          return;
        }
        dot.className = "status-dot connected";
        if (!data.monitoring) {
          text.textContent = "Button connected";
          return;
        }
        // Surfaced here because a mute silently suppressing every color
        // (by design -- see "Long press" in the Quick Guide) was hard to
        // notice otherwise: nothing elsewhere in the UI showed it was on.
        var parts = ["showing " + (data.currently_lit || "off")];
        if (data.latest_value !== null && data.latest_value !== undefined) {
          parts.push("BG " + data.latest_value);
        }
        if (data.muted) {
          parts.push("muted until " + fmtTime(data.muted_until));
        }
        text.textContent = "Button connected — " + parts.join(" · ");
      })
      .catch(function () {
        dot.className = "status-dot disconnected";
        text.textContent = "Could not reach the app";
      });
  }

  refresh();
  setInterval(refresh, 3000);

  var savedBanner = document.getElementById("saved-banner");
  if (savedBanner) {
    setTimeout(function () {
      savedBanner.style.transition = "opacity 0.5s";
      savedBanner.style.opacity = "0";
      setTimeout(function () { savedBanner.remove(); }, 500);
    }, 3000);
  }
})();
</script>
</body>
</html>
"""

LOGIN_PAGE = BRAND_HEAD + """
<h2 style="margin-top:0;">Connect your Dexcom Share account</h2>
{% if error %}<div class="error">{{ error }}</div>{% endif %}
<form method="post" action="{{ url_for('login') }}">
  <fieldset>
    <legend>Dexcom Share</legend>
    <label for="username">Username</label>
    <input type="text" id="username" name="username" required autofocus>

    <label for="password">Password</label>
    <input type="password" id="password" name="password" required>

    <label for="region">Region</label>
    <select id="region" name="region">
      <option value="us">United States</option>
      <option value="ous">Outside the US</option>
      <option value="jp">Japan</option>
    </select>
    <div class="hint">
      Sign in with the Dexcom account of the person wearing the sensor
      &mdash; not a follower's account. In the Dexcom app: Settings &rarr;
      Share &rarr; turn Share on and add a follower.
    </div>
  </fieldset>

  <button type="submit">Connect</button>
</form>

<h2>App</h2>
<form method="post" action="{{ url_for('quit_app') }}"
      onsubmit="return confirm('Quit HyLight?');">
  <button type="submit">Quit HyLight</button>
</form>
""" + BRAND_FOOT

SETTINGS_PAGE = BRAND_HEAD + """
<p class="status">Connected as {{ username }}</p>
{% if error %}<div class="error">{{ error }}</div>{% endif %}
{% if saved %}<div class="success" id="saved-banner">Settings saved.</div>{% endif %}

<div class="card">
  <p class="card-title">Quick Guide</p>
  <div class="press-guide">
    <a class="press-demo" href="#active-window">
      <div class="press-demo-btn"><div class="press-demo-led led-short"></div></div>
      <div class="press-demo-label">
        <strong>Short press</strong>
        <p>A quick tap. Previews your current status (green/white/yellow/red/purple)
           for 3 seconds, anytime.</p>
      </div>
    </a>
    <a class="press-demo" href="#mute-duration">
      <div class="press-demo-btn ring-long"><div class="press-demo-led led-long"></div></div>
      <div class="press-demo-label">
        <strong>Long press</strong>
        <p>Press and hold, then release. Mutes whatever's lit for the mute
           duration below -- press and hold again to cancel the mute early.</p>
      </div>
    </a>
    <a class="press-demo" href="#reminder-timer">
      <div class="press-demo-btn"><div class="press-demo-led led-double"></div></div>
      <div class="press-demo-label">
        <strong>Double press</strong>
        <p>Two quick taps. Starts a reminder (default 20 min, see below) to
           check your glucose, confirmed by a double blue flash.</p>
      </div>
    </a>
  </div>
</div>

<form method="post" action="{{ url_for('save_settings') }}">
  <fieldset id="active-window">
    <legend>Active Window</legend>
    <div class="row">
      <div>
        <label for="window_start">Start time</label>
        <input type="time" id="window_start" name="window_start" value="{{ cfg.window_start }}" required>
      </div>
      <div>
        <label for="window_end">End time</label>
        <input type="time" id="window_end" name="window_end" value="{{ cfg.window_end }}" required>
      </div>
    </div>

    <label>Active days</label>
    <div class="days">
      {% for i, name in enumerate_days %}
      <label>
        <input type="checkbox" name="active_days" value="{{ i }}"
               {% if i in cfg.active_days %}checked{% endif %}>
        {{ name }}
      </label>
      {% endfor %}
    </div>
  </fieldset>

  <fieldset id="warning-thresholds">
    <legend>{{ icon_thresholds | safe }} Warning Thresholds (mg/dL)</legend>
    <div class="row">
      <div>
        <label for="yellow_threshold">Yellow above</label>
        <input type="number" id="yellow_threshold" name="yellow_threshold"
               value="{{ cfg.yellow_threshold }}" min="40" max="400" required>
      </div>
      <div>
        <label for="red_threshold">Red above</label>
        <input type="number" id="red_threshold" name="red_threshold"
               value="{{ cfg.red_threshold }}" min="40" max="400" required>
      </div>
    </div>
    <div class="hint">
      Red must be higher than yellow. Below 70 always shows purple (a fixed
      low-glucose alert, not adjustable here).
    </div>
  </fieldset>

  <fieldset>
    <legend>{{ icon_ramp | safe }} Ramp Detection</legend>
    <label for="ramp_magnitude_mg_dl">Minimum mg/dL rise over 15min to flag as ramping</label>
    <input type="number" id="ramp_magnitude_mg_dl" name="ramp_magnitude_mg_dl"
           value="{{ cfg.ramp_magnitude_mg_dl }}" min="1" max="200" required>
    <div class="hint">
      Lower values trigger sooner but are more likely to false-alarm on noise.
    </div>
  </fieldset>

  <fieldset id="mute-duration">
    <legend>Mute Duration</legend>
    <label for="mute_duration_minutes">A long press mutes for</label>
    <select id="mute_duration_minutes" name="mute_duration_minutes">
      {% for minutes in mute_duration_choices %}
      <option value="{{ minutes }}" {% if minutes == cfg.mute_duration_minutes %}selected{% endif %}>
        {{ minutes }} minutes
      </option>
      {% endfor %}
    </select>
  </fieldset>

  <fieldset id="reminder-timer">
    <legend>Reminder Timer</legend>
    <label for="reminder_delay_minutes">A double press reminds you to check your glucose after</label>
    <select id="reminder_delay_minutes" name="reminder_delay_minutes">
      {% for minutes in reminder_delay_choices %}
      <option value="{{ minutes }}" {% if minutes == cfg.reminder_delay_minutes %}selected{% endif %}>
        {{ minutes }} minutes
      </option>
      {% endfor %}
    </select>
  </fieldset>

  <button type="submit" class="btn-primary">Save settings</button>
</form>

<h2>Dexcom account</h2>
<form method="post" action="{{ url_for('disconnect') }}"
      onsubmit="return confirm('Disconnect this Dexcom account? Monitoring keeps running until you quit the app; on the next launch you will need to reconnect.');">
  <button type="submit">Disconnect Dexcom account</button>
</form>

<h2>App</h2>
<form method="post" action="{{ url_for('quit_app') }}"
      onsubmit="return confirm('Quit HyLight? The LED will turn off and stop responding to presses until you reopen the app.');">
  <button type="submit">Quit HyLight</button>
</form>
""" + BRAND_FOOT

QUIT_PAGE = BRAND_HEAD + """
<div class="success">HyLight is shutting down. The LED will turn off shortly.
You can close this tab -- reopen the app whenever you want to use it again.</div>
<form method="post" action="{{ url_for('restart_app') }}">
  <button type="submit" class="btn-primary">Restart HyLight instead</button>
</form>
""" + BRAND_FOOT

RESTART_PAGE = BRAND_HEAD + """
<div class="success">HyLight is restarting. The LED will turn off, then the app
will reconnect automatically in a few seconds -- this page will reload itself
once it's back.</div>
<script>
// The old copy is still shutting down for the first moments, so wait
// before polling -- otherwise it would answer and we'd redirect to a
// server that is about to disappear.
setTimeout(function poll() {
  fetch("/status")
    .then(function () { window.location = "/"; })
    .catch(function () { setTimeout(poll, 700); });
}, 3000);
</script>
""" + BRAND_FOOT

# How long the quit confirmation page's server stays up after Quit is
# confirmed, giving its "Restart HyLight instead" button a window to
# still work before the process actually exits for good.
QUIT_GRACE_SECONDS = 6


def create_app(cfg, controller):
    app = Flask(__name__)

    def render_settings(error=None, saved=False):
        return render_template_string(
            SETTINGS_PAGE,
            cfg=cfg.as_dict(),
            username=controller.dexcom_username,
            enumerate_days=list(enumerate(DAY_NAMES)),
            mute_duration_choices=MUTE_DURATION_CHOICES_MINUTES,
            reminder_delay_choices=REMINDER_DELAY_CHOICES_MINUTES,
            icon_thresholds=ICON_THRESHOLDS,
            icon_ramp=ICON_RAMP,
            error=error,
            saved=saved,
        )

    @app.route("/status", methods=["GET"])
    def status():
        return jsonify(controller.get_status())

    @app.route("/", methods=["GET"])
    def index():
        if not controller.is_running():
            return render_template_string(LOGIN_PAGE, error=None)
        return render_settings(saved=request.args.get("saved") == "1")

    @app.route("/login", methods=["POST"])
    def login():
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        region = request.form.get("region", "us")

        if not username or not password:
            return render_template_string(LOGIN_PAGE, error="Username and password are required.")

        try:
            controller.start(username, password, region)
        except Exception as exc:
            # The page only shows a generic message; log the real reason
            # (bad password vs. network/SSL trouble) for diagnosing pilots.
            logging.getLogger("led-button").warning(
                "Login attempt failed: %s: %s", type(exc).__name__, exc
            )
            return render_template_string(
                LOGIN_PAGE,
                error="Could not log in with those credentials. Double-check your "
                      "Dexcom Share username/password and that Share is turned on.",
            )

        return redirect(url_for("index"))

    @app.route("/settings", methods=["POST"])
    def save_settings():
        if not controller.is_running():
            return redirect(url_for("index"))

        form = request.form
        active_days = [int(d) for d in form.getlist("active_days")]

        try:
            window_start = form["window_start"]
            window_end = form["window_end"]
            yellow_threshold = int(form["yellow_threshold"])
            red_threshold = int(form["red_threshold"])
            ramp_magnitude = int(form["ramp_magnitude_mg_dl"])
            mute_duration_minutes = int(form["mute_duration_minutes"])
            reminder_delay_minutes = int(form["reminder_delay_minutes"])
        except (KeyError, ValueError):
            return render_settings(error="Some values were missing or not valid numbers -- nothing was saved.")

        if red_threshold <= yellow_threshold:
            return render_settings(error="Red threshold must be higher than yellow -- nothing was saved.")

        if not active_days:
            return render_settings(error="Select at least one active day -- nothing was saved.")

        if mute_duration_minutes not in MUTE_DURATION_CHOICES_MINUTES:
            return render_settings(error="Not a valid mute duration -- nothing was saved.")

        if reminder_delay_minutes not in REMINDER_DELAY_CHOICES_MINUTES:
            return render_settings(error="Not a valid reminder delay -- nothing was saved.")

        cfg.update(
            window_start=window_start,
            window_end=window_end,
            active_days=active_days,
            yellow_threshold=yellow_threshold,
            red_threshold=red_threshold,
            ramp_magnitude_mg_dl=ramp_magnitude,
            mute_duration_minutes=mute_duration_minutes,
            reminder_delay_minutes=reminder_delay_minutes,
        )
        controller.notify_settings_changed()
        return redirect(url_for("index", saved="1"))

    @app.route("/disconnect", methods=["POST"])
    def disconnect():
        from dexcom_led_button import clear_saved_credentials

        controller.disconnect()
        clear_saved_credentials()
        return redirect(url_for("index"))

    @app.route("/quit", methods=["POST"])
    def quit_app():
        controller.request_shutdown(delay_seconds=QUIT_GRACE_SECONDS)
        return render_template_string(QUIT_PAGE)

    @app.route("/restart", methods=["POST"])
    def restart_app():
        controller.request_restart()
        return render_template_string(RESTART_PAGE)

    return app
