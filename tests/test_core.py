"""Regression tests for the platform-independent core of HyLight.

Run from the project root:   python3 -m unittest discover -s tests -v

Everything here runs against fakes -- a temporary config folder, an in-memory
keyring, a fake button and a fake Dexcom -- so it can never touch the real
Keychain / Credential Manager, ~/.dexcom_led_button, the Dexcom account or the
USB button. Keep it that way: an early smoke test against the real Keychain
deleted the owner's saved Dexcom login.
"""

import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# --- isolation: must happen BEFORE dexcom_led_button is imported, because it
# --- reads CONFIG_DIR at import time (log file, lock file) -------------------
import keyring

_keychain = {}
keyring.set_password = lambda service, key, value: _keychain.__setitem__((service, key), value)
keyring.get_password = lambda service, key: _keychain.get((service, key))
keyring.delete_password = lambda service, key: _keychain.pop((service, key), None)

import config as config_module

_TMP = Path(tempfile.mkdtemp(prefix="hylight-tests-"))
config_module.CONFIG_DIR = _TMP
config_module.CONFIG_PATH = _TMP / "config.json"

import dexcom_led_button as app  # noqa: E402
import web_ui  # noqa: E402


class FakeReading:
    def __init__(self, value):
        self.value = value
        self.trend_direction = "Flat"


class FakeDexcom:
    next_value = None

    def __init__(self, username=None, password=None, region=None):
        self.username = username

    def get_current_glucose_reading(self):
        return None if self.next_value is None else FakeReading(self.next_value)

    def get_glucose_readings(self, minutes=20, max_count=4):
        return []


class FakeButton:
    """Stands in for LedButton: records colors, never touches hardware."""

    def __init__(self):
        self.connected = True
        self.colors = []

    def set_color(self, name):
        self.colors.append(name)

    def read_button_state(self):
        time.sleep(0.01)
        return None

    def consume_needs_refresh(self):
        return False

    def close(self):
        pass


class FakeServer:
    def shutdown(self):
        pass


def fresh_config():
    return config_module.Config()


class ColorTableTests(unittest.TestCase):
    def test_every_channel_is_fully_on_or_off(self):
        # The LED channels are plain on/off switches, not brightness.
        for name, (_enable, r, g, b) in app.COLORS.items():
            self.assertTrue(all(v in (0, 255) for v in (r, g, b)), name)

    def test_every_color_is_a_distinct_combination(self):
        combos = [rgb[1:] for rgb in app.COLORS.values()]
        self.assertEqual(len(combos), len(set(combos)))

    def test_scheme(self):
        c = {k: v[1:] for k, v in app.COLORS.items()}
        self.assertEqual(c["white"], (255, 255, 255))
        self.assertEqual(c["yellow"], (255, 255, 0))
        self.assertEqual(c["red"], (255, 0, 0))
        self.assertEqual(c["purple"], (255, 0, 255))
        self.assertEqual(c["green"], (0, 255, 0))
        self.assertEqual(c["blue"], (0, 0, 255))
        self.assertNotIn("orange", c)


class StatusColorTests(unittest.TestCase):
    def setUp(self):
        self.cfg = fresh_config()  # yellow above 150, red above 200

    def test_status_colors(self):
        cases = [
            (55, False, "purple"), (69, False, "purple"), (70, False, "off"),
            (120, False, "off"), (120, True, "white"), (150, False, "off"),
            (151, False, "yellow"), (151, True, "yellow"), (200, False, "yellow"),
            (201, False, "red"), (None, True, "off"),
        ]
        for value, ramp, expected in cases:
            self.assertEqual(app.compute_status_color(value, ramp, self.cfg), expected, (value, ramp))

    def test_preview_is_green_only_when_nothing_else_applies(self):
        self.assertEqual(app.compute_preview_color(120, False, self.cfg), "green")
        self.assertEqual(app.compute_preview_color(120, True, self.cfg), "white")
        self.assertEqual(app.compute_preview_color(60, False, self.cfg), "purple")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        config_module.CONFIG_PATH.unlink(missing_ok=True)

    def test_defaults(self):
        self.assertEqual(fresh_config().get_yellow_threshold(), 150)

    def test_old_orange_threshold_is_migrated(self):
        config_module.CONFIG_PATH.write_text(json.dumps({"orange_threshold": 140, "red_threshold": 200}))
        cfg = config_module.Config.load()
        self.assertEqual(cfg.get_yellow_threshold(), 140)
        self.assertNotIn("orange_threshold", cfg.as_dict())
        cfg.update(yellow_threshold=150)
        self.assertNotIn("orange_threshold", json.loads(config_module.CONFIG_PATH.read_text()))

    def test_old_ramp_amount_maps_to_nearest_sensitivity(self):
        for old_mg, expected in [(5, "high"), (10, "high"), (11, "high"), (15, "medium"),
                                 (17, "medium"), (18, "low"), (30, "low")]:
            config_module.CONFIG_PATH.write_text(json.dumps({"ramp_magnitude_mg_dl": old_mg, "mute_duration_minutes": 45}))
            cfg = config_module.Config.load()
            self.assertEqual(cfg.get_ramp_sensitivity(), expected, old_mg)
            self.assertNotIn("ramp_magnitude_mg_dl", cfg.as_dict())
            self.assertNotIn("mute_duration_minutes", cfg.as_dict())

    def test_sensitivity_levels(self):
        cfg = fresh_config()
        self.assertEqual(cfg.get_ramp_sensitivity(), "medium")
        for level, mg in [("high", 8), ("medium", 14), ("low", 20), ("off", None)]:
            cfg.update(ramp_sensitivity=level)
            self.assertEqual(cfg.get_ramp_magnitude(), mg)

    def test_old_reminder_setting_is_dropped(self):
        config_module.CONFIG_PATH.write_text(json.dumps({"reminder_delay_minutes": 20}))
        cfg = config_module.Config.load()
        self.assertNotIn("reminder_delay_minutes", cfg.as_dict())
        self.assertEqual(cfg.get_on_call_minutes(), 60)


class GlucoseLoopTests(unittest.TestCase):
    def run_once(self, value, ramp):
        app.state.__init__()
        dexcom, button, cfg = FakeDexcom(), FakeButton(), fresh_config()
        dexcom.next_value = value
        wake, stop = threading.Event(), threading.Event()
        with mock.patch.object(app, "in_window", lambda now, c: True), \
                mock.patch.object(app, "detect_ramp", lambda d, c: ramp):
            thread = threading.Thread(target=app.glucose_loop, args=(dexcom, button, cfg, wake, stop), daemon=True)
            thread.start()
            deadline = time.time() + 3
            while not button.colors and time.time() < deadline:
                time.sleep(0.005)
            stop.set()
            wake.set()
            thread.join(2)
        return button.colors[0], app.state.get_currently_lit()

    def test_led_follows_glucose(self):
        for value, ramp, expected in [
            (55, False, "purple"), (120, False, "off"), (120, True, "white"),
            (160, False, "yellow"), (160, True, "yellow"), (210, False, "red"),
        ]:
            shown, tracked = self.run_once(value, ramp)
            self.assertEqual((shown, tracked), (expected, expected), (value, ramp))

    def test_long_press_mutes_the_ramp_light(self):
        app.state.__init__()
        app.state.set_currently_lit("white")
        button = FakeButton()
        now = datetime.now()
        app.handle_long_press(now, button, fresh_config())
        self.assertEqual(button.colors, ["off"])
        self.assertTrue(app.state.is_muted(now + timedelta(minutes=29)))
        self.assertFalse(app.state.is_muted(now + timedelta(minutes=31)))


class RampSensitivityTests(unittest.TestCase):
    """detect_ramp: the rise needed depends on sensitivity; Off checks nothing."""

    class HistoryDexcom:
        def __init__(self, rise, trend="Flat"):
            self.rise, self.trend, self.calls, self.base = rise, trend, 0, datetime(2026, 1, 1, 12, 0)

        def get_glucose_readings(self, minutes=20, max_count=4):
            self.calls += 1  # each call is a new Dexcom sample, 5 minutes later
            t = self.base + timedelta(minutes=5 * self.calls)
            mk = lambda v, dt: type("R", (), {"value": v, "datetime": dt, "trend_direction": self.trend})()
            span = getattr(self, "span", 15)  # minutes back the oldest reading goes
            ages = [a for a in (0, 5, 10, 15) if a <= span]
            return [mk(120 + self.rise * (1 - a / span), t - timedelta(minutes=a)) for a in ages]

    def confirmed(self, sensitivity, rise, trend="Flat"):
        app.state.__init__()
        cfg = fresh_config()
        cfg.update(ramp_sensitivity=sensitivity)
        dexcom = self.HistoryDexcom(rise, trend)
        results = [app.detect_ramp(dexcom, cfg) for _ in range(2)]  # needs 2 consecutive samples
        return results[-1], dexcom.calls

    def test_rise_needed_per_level(self):
        for level, mg in [("high", 8), ("medium", 14), ("low", 20)]:
            self.assertTrue(self.confirmed(level, mg)[0], (level, mg))
            self.assertFalse(self.confirmed(level, mg - 1)[0], (level, mg - 1))

    def test_trend_arrow_still_counts_when_on(self):
        self.assertTrue(self.confirmed("low", 0, trend="FortyFiveUp")[0])

    def test_rise_is_measured_over_15_minutes_not_10(self):
        app.state.__init__()
        cfg = fresh_config()
        cfg.update(ramp_sensitivity="medium")  # 14 mg/dL
        dexcom = self.HistoryDexcom(20)
        dexcom.span = 10  # only 10 minutes of history: no 15-minute comparison possible
        self.assertFalse(any(app.detect_ramp(dexcom, cfg) for _ in range(3)))

    def test_off_disables_ramp_detection_entirely(self):
        ramping, calls = self.confirmed("off", 40, trend="DoubleUp")
        self.assertFalse(ramping)
        self.assertEqual(calls, 0, "Off shouldn't even ask Dexcom for history")


class OnCallWindowTests(unittest.TestCase):
    """Double press = a temporary active window (default 1 hour)."""

    def setUp(self):
        app.state.__init__()
        self.cfg = fresh_config()  # schedule 11:30-20:00 every day, on-call 60 min
        self.night = datetime.now().replace(hour=22, minute=0, second=0, microsecond=0)

    def run_loop(self, value, until_colors, timeout=3.0):
        dexcom, button = FakeDexcom(), FakeButton()
        dexcom.next_value = value
        wake, stop = threading.Event(), threading.Event()
        with mock.patch.object(app, "detect_ramp", lambda d, c: False):
            thread = threading.Thread(target=app.glucose_loop, args=(dexcom, button, self.cfg, wake, stop), daemon=True)
            thread.start()
            deadline = time.time() + timeout
            while len(button.colors) < until_colors and time.time() < deadline:
                time.sleep(0.005)
            stop.set()
            wake.set()
            thread.join(2)
        return button.colors

    def test_double_press_starts_window_flashes_and_wakes_loop(self):
        button, wake = FakeButton(), threading.Event()
        app.handle_double_press(self.night, button, self.cfg, wake)
        self.assertEqual(button.colors, ["blue", "off", "blue", "off", "off"])
        self.assertTrue(wake.is_set())
        self.assertTrue(app.state.is_on_call(self.night + timedelta(minutes=59)))
        self.assertFalse(app.state.is_on_call(self.night + timedelta(minutes=61)))

    def test_on_call_makes_off_schedule_time_active(self):
        self.assertFalse(app.in_scheduled_window(self.night, self.cfg))
        self.assertFalse(app.in_window(self.night, self.cfg))
        app.state.start_on_call(self.night, self.cfg.get_on_call_duration())
        self.assertTrue(app.in_window(self.night + timedelta(minutes=30), self.cfg))
        self.assertFalse(app.in_window(self.night + timedelta(minutes=61), self.cfg))

    def test_double_press_again_restarts_the_full_length(self):
        app.handle_double_press(self.night, FakeButton(), self.cfg, threading.Event())
        app.handle_double_press(self.night + timedelta(minutes=30), FakeButton(), self.cfg, threading.Event())
        self.assertEqual(app.state.get_on_call_until(), self.night + timedelta(minutes=90))

    def test_end_flashes_twice_when_going_back_off_schedule(self):
        app.state.on_call_until = datetime.now() - timedelta(seconds=1)
        with mock.patch.object(app, "in_scheduled_window", lambda now, c: False):
            colors = self.run_loop(160, until_colors=6)
        self.assertEqual(colors[:4], ["blue", "off", "blue", "off"])
        self.assertEqual(colors[-1], "off")  # outside the window again: high reading no longer lights
        self.assertIsNone(app.state.get_on_call_until())

    def test_no_end_flash_when_regular_window_has_taken_over(self):
        app.state.on_call_until = datetime.now() - timedelta(seconds=1)
        with mock.patch.object(app, "in_scheduled_window", lambda now, c: True):
            colors = self.run_loop(160, until_colors=1)
        self.assertEqual(colors, ["yellow"])
        self.assertIsNone(app.state.get_on_call_until())

    def test_loop_lights_during_window_and_flashes_on_time_at_the_end(self):
        app.state.on_call_until = datetime.now() + timedelta(seconds=0.4)
        started = time.time()
        with mock.patch.object(app, "in_scheduled_window", lambda now, c: False):
            colors = self.run_loop(160, until_colors=7)
        elapsed = time.time() - started
        self.assertEqual(colors[0], "yellow")  # active during on-call, outside the schedule
        self.assertEqual(colors[1:5], ["blue", "off", "blue", "off"])
        self.assertEqual(colors[-1], "off")
        self.assertLess(elapsed, 2.5, "end flash should not wait for the 60 s poll")

    def test_poll_wait_targets_the_on_call_end(self):
        now = datetime.now()
        self.assertEqual(app._poll_wait_seconds(now), app.POLL_INTERVAL_SECONDS)
        app.state.on_call_until = now + timedelta(seconds=10)
        self.assertAlmostEqual(app._poll_wait_seconds(now), 10.05, places=2)
        app.state.on_call_until = now + timedelta(hours=2)
        self.assertEqual(app._poll_wait_seconds(now), app.POLL_INTERVAL_SECONDS)
        app.state.on_call_until = now - timedelta(seconds=5)
        self.assertAlmostEqual(app._poll_wait_seconds(now), 0.05, places=3)

    def test_two_real_taps_through_button_loop_start_the_window(self):
        class TappingButton(FakeButton):
            """Reports two quick taps (50 ms each, 150 ms apart), then nothing."""
            def __init__(self):
                super().__init__()
                self.t0 = time.monotonic()

            def read_button_state(self):
                time.sleep(0.002)
                t = time.monotonic() - self.t0
                return "pressed" if (t < 0.05 or 0.20 <= t < 0.25) else None

        button, wake = TappingButton(), threading.Event()
        threading.Thread(target=app.button_loop, args=(button, self.cfg, wake), daemon=True).start()
        deadline = time.time() + 3
        while not app.state.get_on_call_until() and time.time() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(app.state.get_on_call_until())
        self.assertTrue(wake.wait(2))
        self.assertIn("blue", button.colors)


class SettingsFormTests(unittest.TestCase):
    """Validation shared by the settings page and the setup tour."""

    def parse(self, groups, **fields):
        from werkzeug.datastructures import MultiDict
        from settings_form import parse_settings_form
        days = fields.pop("active_days", ["0", "1"])
        form = MultiDict(list(fields.items()) + [("active_days", d) for d in days])
        return parse_settings_form(form, groups)

    def test_window(self):
        self.assertEqual(self.parse(("window",), window_start="21:00", window_end="22:30")[0],
                         {"window_start": "21:00", "window_end": "22:30", "active_days": [0, 1]})
        self.assertIn("later than the start", self.parse(("window",), window_start="18:00", window_end="11:30")[1])
        self.assertIn("times of day", self.parse(("window",), window_start="25:00", window_end="26:00")[1])
        self.assertIn("at least one", self.parse(("window",), window_start="11:30", window_end="18:00", active_days=[])[1])
        self.assertIn("at least one", self.parse(("window",), window_start="11:30", window_end="18:00", active_days=["9"])[1])

    def test_thresholds(self):
        self.assertEqual(self.parse(("thresholds",), yellow_threshold="140", red_threshold="200")[0],
                         {"yellow_threshold": 140, "red_threshold": 200})
        self.assertIn("higher than yellow", self.parse(("thresholds",), yellow_threshold="200", red_threshold="200")[1])
        self.assertIn("between 40 and 400", self.parse(("thresholds",), yellow_threshold="20", red_threshold="200")[1])
        self.assertIn("not valid numbers", self.parse(("thresholds",), yellow_threshold="abc", red_threshold="200")[1])

    def test_only_requested_groups_are_read(self):
        self.assertEqual(self.parse(("ramp",), ramp_sensitivity="low"), ({"ramp_sensitivity": "low"}, None))
        self.assertEqual(self.parse(("on_call",), on_call_minutes="90"), ({"on_call_minutes": 90}, None))
        self.assertEqual(self.parse((), whatever="x"), ({}, None))


class OnboardingTests(unittest.TestCase):
    """The first-run setup tour."""

    def setUp(self):
        _keychain.clear()
        config_module.CONFIG_PATH.unlink(missing_ok=True)
        app.state.__init__()
        patcher = mock.patch.object(app, "Dexcom", FakeDexcom)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = fresh_config()  # fresh install: tour not done
        self.controller = app.MonitorController(FakeButton(), self.cfg)
        self.controller.web_server = FakeServer()
        self.addCleanup(self.controller.disconnect)
        self.client = web_ui.create_app(self.cfg, self.controller).test_client()

    def test_fresh_install_starts_the_tour(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/setup/1"))

    def test_existing_users_config_skips_the_tour(self):
        config_module.CONFIG_PATH.write_text(json.dumps({"yellow_threshold": 140}))
        self.assertTrue(config_module.Config.load().get_onboarding_done())

    def test_every_screen_renders_with_its_scene(self):
        import re
        cover = self.client.get("/setup/1").get_data(as_text=True)
        self.assertIn("Glucose awareness, right when you need it", cover)
        self.assertIn('class="cover-logo"', cover)
        self.assertIn('name="password"', cover)
        self.assertIn("Connect later", cover)
        self.assertNotIn('id="scene"', cover)
        for step, scene, title in [(2, "lights", "What the light tells you"), (3, "window", "When should HyLight watch"),
                                   (4, "levels", "Set your high levels"), (5, "ramp", "Catch the rise early"),
                                   (6, "ondemand", "Double-press")]:
            page = self.client.get(f"/setup/{step}").get_data(as_text=True)
            self.assertIn(title, page)
            data = json.loads(re.search(r'<script type="application/json" id="scene-data">(.*?)</script>', page).group(1))
            self.assertEqual((data["step"], data["scene"]), (step, scene))
            self.assertEqual(data["ramp_mg"], {"high": 8, "medium": 14, "low": 20, "off": None})
            self.assertIn('id="scene"', page)
            self.assertIn("Skip setup", page)
        self.assertIn('name="window_start"', self.client.get("/setup/3").get_data(as_text=True))
        self.assertIn('name="ramp_sensitivity" value="off"', self.client.get("/setup/5").get_data(as_text=True))
        on_demand = self.client.get("/setup/6").get_data(as_text=True)
        self.assertIn("On-Demand Window", on_demand)
        self.assertNotIn("kids", on_demand)
        self.assertNotIn("restart the timer", on_demand)

    def test_walk_through_connects_first_and_saves_each_step(self):
        r = self.client.post("/setup/1", data={"username": "wearer", "password": "x", "region": "ous"})
        self.assertTrue(r.headers["Location"].endswith("/setup/2"))
        self.assertTrue(self.controller.is_running())
        self.assertEqual(self.cfg.as_dict()["dexcom_region"], "ous")
        self.assertIn("Connected to Dexcom as <b>wearer</b>", self.client.get("/setup/1").get_data(as_text=True))
        self.assertTrue(self.client.post("/setup/2").headers["Location"].endswith("/setup/3"))
        r = self.client.post("/setup/3", data={"window_start": "12:00", "window_end": "17:00", "active_days": ["0", "2", "4"]})
        self.assertTrue(r.headers["Location"].endswith("/setup/4"))
        self.assertEqual((self.cfg.as_dict()["window_start"], self.cfg.as_dict()["active_days"]), ("12:00", [0, 2, 4]))
        self.client.post("/setup/4", data={"yellow_threshold": "140", "red_threshold": "210"})
        self.assertEqual(self.cfg.get_yellow_threshold(), 140)
        self.client.post("/setup/5", data={"ramp_sensitivity": "high"})
        self.assertEqual(self.cfg.get_ramp_sensitivity(), "high")
        self.assertFalse(self.cfg.get_onboarding_done())
        r = self.client.post("/setup/6", data={"on_call_minutes": "90"}, follow_redirects=True)
        self.assertEqual(self.cfg.get_on_call_minutes(), 90)
        self.assertTrue(self.cfg.get_onboarding_done())
        self.assertIn(b"Tour complete", r.data)

    def test_bad_login_stays_on_the_cover(self):
        with mock.patch.object(app, "Dexcom", side_effect=Exception("401")):
            r = self.client.post("/setup/1", data={"username": "follower", "password": "x", "region": "jp"})
        page = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Could not log in with those credentials", page)
        self.assertIn('value="follower"', page)
        self.assertIn('<option value="jp" selected>', page)
        self.assertFalse(self.controller.is_running())
        missing = self.client.post("/setup/1", data={"username": "", "password": ""}).get_data(as_text=True)
        self.assertIn("Username and password are required.", missing)

    def test_connect_later_ends_on_the_login_page(self):
        self.assertIn("What the light tells you", self.client.get("/setup/2").get_data(as_text=True))
        self.assertIn("Next, you'll connect your Dexcom account", self.client.get("/setup/6").get_data(as_text=True))
        r = self.client.post("/setup/6", data={"on_call_minutes": "60"}, follow_redirects=True)
        self.assertIn(b"Last step: connect your Dexcom", r.data)
        self.assertIn(b'name="password"', r.data)

    def test_invalid_step_keeps_what_was_typed_and_saves_nothing(self):
        r = self.client.post("/setup/3", data={"window_start": "19:00", "window_end": "09:00", "active_days": ["1"]})
        page = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("The end time must be later than the start time.", page)
        self.assertIn('value="19:00"', page)
        self.assertEqual(self.cfg.as_dict()["window_start"], "11:30")
        r = self.client.post("/setup/4", data={"yellow_threshold": "200", "red_threshold": "180"})
        self.assertIn(b"Red threshold must be higher than yellow.", r.data)
        self.assertEqual(self.cfg.get_red_threshold(), 200)

    def test_skip_and_unknown_step(self):
        self.assertTrue(self.client.get("/setup/9").headers["Location"].endswith("/setup/1"))
        self.client.post("/setup/exit")
        self.assertTrue(self.cfg.get_onboarding_done())
        self.assertIn(b'name="password"', self.client.get("/").data)

    def test_tour_again_from_settings(self):
        self.cfg.update(onboarding_done=True)
        self.client.post("/login", data={"username": "w", "password": "x", "region": "us"})
        settings = self.client.get("/").get_data(as_text=True)
        self.assertIn('href="/setup/1"', settings)
        cover = self.client.get("/setup/1").get_data(as_text=True)
        self.assertIn("Start the tour", cover)
        self.assertIn("Exit the tour", cover)
        self.assertNotIn('name="password"', cover)
        self.assertTrue(self.client.post("/setup/1").headers["Location"].endswith("/setup/2"))
        done = self.client.post("/setup/6", data={"on_call_minutes": "45"}, follow_redirects=True)
        self.assertIn(b"Tour complete", done.data)
        self.assertIn(b"Disconnect Dexcom account", done.data)


class WebFlowTests(unittest.TestCase):
    def setUp(self):
        _keychain.clear()
        config_module.CONFIG_PATH.unlink(missing_ok=True)
        app.state.__init__()
        patcher = mock.patch.object(app, "Dexcom", FakeDexcom)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = fresh_config()
        self.cfg.update(onboarding_done=True)
        self.button = FakeButton()
        self.controller = app.MonitorController(self.button, self.cfg)
        self.controller.web_server = FakeServer()
        self.addCleanup(self.controller.disconnect)
        self.client = web_ui.create_app(self.cfg, self.controller).test_client()

    def login(self, name="wearer"):
        return self.client.post("/login", data={"username": name, "password": "x", "region": "us"}, follow_redirects=True)

    def settings_form(self, **overrides):
        form = {
            "window_start": "11:30", "window_end": "20:00", "active_days": ["0", "1", "2", "3", "4"],
            "yellow_threshold": "150", "red_threshold": "200", "ramp_sensitivity": "medium",
            "on_call_minutes": "60",
        }
        form.update(overrides)
        return form

    def test_login_form_until_connected(self):
        self.assertIn(b'name="password"', self.client.get("/").data)

    def test_login_then_disconnect_then_login_again(self):
        self.assertIn(b"Disconnect Dexcom account", self.login().data)
        self.assertTrue(self.controller.is_running())
        page = self.client.post("/disconnect", follow_redirects=True).data
        self.assertFalse(self.controller.is_running())
        self.assertIn(b'name="password"', page)
        self.assertNotIn(b"Disconnect Dexcom account", page)
        self.login("someone_else")
        self.assertEqual(self.controller.dexcom_username, "someone_else")

    def test_settings_page_wording(self):
        self.login()
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Yellow above", page)
        self.assertIn("green/white/yellow/red/purple", page)
        self.assertNotIn("orange", page.lower())
        self.assertNotIn("reminder", page.lower())
        self.assertIn('class="btn-primary"', page)

    def test_ramp_sensitivity_setting_and_no_mute_setting(self):
        self.login()
        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("Mute Duration", page)
        self.assertNotIn("mute_duration", page)
        self.assertIn("Mutes whatever's lit for 30 minutes", page)
        self.assertRegex(page, r'value="medium" selected>\s*Medium &mdash; 14 mg/dL rise over 15 min')
        for level in ("high", "low", "off"):
            self.assertIn(f'value="{level}"', page)
        bad = self.client.post("/settings", data=self.settings_form(ramp_sensitivity="extreme"), follow_redirects=True)
        self.assertIn(b"Not a valid ramp sensitivity", bad.data)
        self.client.post("/settings", data=self.settings_form(ramp_sensitivity="off"))
        self.assertIsNone(self.cfg.get_ramp_magnitude())
        self.assertRegex(self.client.get("/").get_data(as_text=True), r'value="off" selected>\s*Off')

    def test_on_call_setting_and_status(self):
        self.login()
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("On-Demand Window", page)
        self.assertNotIn("On-Call", page)
        self.assertRegex(page, r'value="60" selected>\s*1 hour')
        bad = self.client.post("/settings", data=self.settings_form(on_call_minutes="25"), follow_redirects=True)
        self.assertIn(b"Not a valid on-demand length", bad.data)
        self.client.post("/settings", data=self.settings_form(on_call_minutes="90"))
        self.assertEqual(self.cfg.get_on_call_minutes(), 90)
        self.assertIsNone(self.client.get("/status").json["on_call_until"])
        app.state.start_on_call(datetime.now(), self.cfg.get_on_call_duration())
        self.assertIsNotNone(self.client.get("/status").json["on_call_until"])
        self.assertTrue(self.client.get("/status").json["in_active_window"] or app.in_scheduled_window(datetime.now(), self.cfg))

    def test_settings_validation_and_save(self):
        self.login()
        bad = self.client.post("/settings", data=self.settings_form(yellow_threshold="180", red_threshold="180"), follow_redirects=True)
        self.assertIn(b"Red threshold must be higher than yellow", bad.data)
        ok = self.client.post("/settings", data=self.settings_form(yellow_threshold="180", red_threshold="230"), follow_redirects=True)
        self.assertIn(b'id="saved-banner"', ok.data)
        self.assertEqual((self.cfg.get_yellow_threshold(), self.cfg.get_red_threshold()), (180, 230))

    def test_quit_offers_restart_and_restart_sets_flag(self):
        self.login()
        page = self.client.post("/quit").data
        self.assertIn(b"Restart HyLight instead", page)
        self.assertIsNotNone(self.controller._pending_quit_timer)
        self.controller._pending_quit_timer.cancel()
        self.assertIn(b"restarting", self.client.post("/restart").data.lower())
        self.assertTrue(self.controller.consume_restart_requested())
        self.assertFalse(self.controller.consume_restart_requested())

    def test_quit_page_hides_restart_before_the_server_goes(self):
        self.login()
        page = self.client.post("/quit").data.decode()
        self.controller._pending_quit_timer.cancel()
        self.assertIn("HyLight has quit", page)
        self.assertIn("}, %d);" % ((web_ui.QUIT_GRACE_SECONDS - 1) * 1000), page)


class LedButtonCloseTests(unittest.TestCase):
    """Seen on Windows: after shutdown closed the device, the button thread's
    next read failed, the self-healing reopened it ("LED button connected")
    and flagged a repaint of the LED shutdown had just turned off."""

    def test_close_is_final(self):
        with mock.patch.object(app.hid, "enumerate", return_value=[]):
            button = app.LedButton()
        device = mock.Mock()
        device.read.side_effect = OSError("read error")
        button.device, button.connected = device, True
        button.close()
        device.close.assert_called_once()
        button._last_reopen_attempt = 0.0
        with mock.patch.object(button, "_open") as reopen:
            self.assertIsNone(button.read_button_state())
            button.set_color("red")
            button._reopen()
        reopen.assert_not_called()
        self.assertFalse(button.connected)
        self.assertFalse(button.consume_needs_refresh())


class LedWriteTests(unittest.TestCase):
    """Windows rejects the feature report (the descriptor declares none), so
    there the same bytes go out with write(). Probed on real hardware."""

    def _button(self):
        with mock.patch.object(app.hid, "enumerate", return_value=[]):
            button = app.LedButton()
        button.device = mock.Mock()
        button.connected = True
        return button

    def test_windows_uses_output_report(self):
        button = self._button()
        with mock.patch.object(app.sys, "platform", "win32"):
            button.set_color("green")
        button.device.write.assert_called_once_with([0x00, *app.COLORS["green"]])
        button.device.send_feature_report.assert_not_called()

    def test_mac_uses_feature_report(self):
        button = self._button()
        with mock.patch.object(app.sys, "platform", "darwin"):
            button.set_color("red")
        button.device.send_feature_report.assert_called_once_with([0x00, *app.COLORS["red"]])
        button.device.write.assert_not_called()


class PidCheckTests(unittest.TestCase):
    def test_windows_never_calls_os_kill(self):
        # os.kill(pid, 0) on Windows terminates the process it is "checking".
        with mock.patch.object(app.sys, "platform", "win32"), \
                mock.patch.object(app, "_pid_is_alive_windows", return_value=True) as win, \
                mock.patch.object(app.os, "kill", side_effect=AssertionError("os.kill on Windows")):
            self.assertTrue(app._pid_is_alive(1234))
        win.assert_called_once_with(1234)

    @unittest.skipIf(sys.platform == "win32", "POSIX path")
    def test_posix_live_and_dead_pids(self):
        import subprocess
        self.assertTrue(app._pid_is_alive(app.os.getpid()))
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertFalse(app._pid_is_alive(child.pid))

    @unittest.skipUnless(sys.platform == "win32", "Windows path")
    def test_windows_live_and_dead_pids(self):
        import subprocess
        self.assertTrue(app._pid_is_alive(app.os.getpid()))
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertFalse(app._pid_is_alive(child.pid))


class _FakePystray:
    """Stands in for pystray, which needs a real desktop to import."""

    class MenuItem:
        def __init__(self, text, action, default=False):
            self.text, self.action, self.default = text, action, default

    class Menu:
        SEPARATOR = object()

        def __init__(self, *items):
            self.items = items

    class Icon:
        def __init__(self, name, image, title, menu):
            self.name, self.image, self.title, self.menu = name, image, title, menu
            self.detached = self.stopped = False
            self._message_handlers = {}  # as in pystray's Windows backend

        def run_detached(self):
            self.detached = True

        def stop(self):
            self.stopped = True


class TrayTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(sys.modules, {"pystray": _FakePystray})
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.modules.pop("tray_loop", None)
        self.addCleanup(sys.modules.pop, "tray_loop", None)
        import tray_loop
        self.tray_loop = tray_loop

    def _items(self, icon):
        return [i for i in icon.menu.items if isinstance(i, _FakePystray.MenuItem)]

    def test_icon_image_is_trimmed_and_tray_sized(self):
        image = self.tray_loop._load_image()
        self.assertEqual(image.size, (64, 64))
        self.assertEqual(image.mode, "RGBA")

    def test_menu_wiring(self):
        opened, quit_ = [], []
        icon = self.tray_loop.start(lambda: opened.append(1), lambda: quit_.append(1), lambda: None)
        self.assertTrue(icon.detached)
        open_item, quit_item = self._items(icon)
        self.assertEqual(open_item.text, "Open HyLight Settings")
        self.assertTrue(open_item.default)  # left-click opens settings
        self.assertEqual(quit_item.text, "Quit HyLight")
        open_item.action(icon, open_item)
        self.assertEqual((opened, quit_), ([1], []))
        quit_item.action(icon, quit_item)
        self.assertEqual(quit_, [1])
        self.tray_loop.stop(icon)
        self.assertTrue(icon.stopped)

    def test_tray_quit_stops_the_real_web_server(self):
        from werkzeug.serving import make_server
        server = make_server("127.0.0.1", 0, web_ui.create_app(app.Config.load(), app.MonitorController(FakeButton(), app.Config.load())))
        controller = mock.Mock()
        controller.request_shutdown.side_effect = lambda: threading.Thread(target=server.shutdown, daemon=True).start()
        icon = app._start_tray("http://127.0.0.1:1", controller, FakeButton(), server)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        time.sleep(0.2)
        self._items(icon)[1].action(icon, None)
        serving.join(3)
        self.assertFalse(serving.is_alive(), "Quit from the tray didn't stop the server")
        server.server_close()

    def test_tray_failure_doesnt_stop_the_app(self):
        with mock.patch.dict(sys.modules, {"pystray": None}):
            sys.modules.pop("tray_loop", None)
            self.assertIsNone(app._start_tray("http://x", mock.Mock(), FakeButton(), FakeServer()))

    def test_windows_shutdown_is_allowed_and_turns_the_led_off(self):
        quit_, ended = [], []
        icon = self.tray_loop.start(lambda: None, lambda: quit_.append(1), lambda: ended.append(1))
        handle = icon._message_handlers
        # pystray answers 0 to unknown messages; 0 here would veto shutdown.
        self.assertEqual(handle[self.tray_loop.WM_QUERYENDSESSION](0, 0), 1)
        handle[self.tray_loop.WM_ENDSESSION](0, 0)  # shutdown was cancelled
        self.assertEqual(ended, [])
        handle[self.tray_loop.WM_ENDSESSION](1, 0)
        self.assertEqual(ended, [1])
        handle[self.tray_loop.WM_CLOSE](0, 0)  # plain `taskkill`
        self.assertEqual(quit_, [1])

    def test_session_end_cleans_up_before_returning(self):
        button, server = FakeButton(), mock.Mock()
        app.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        app.LOCK_PATH.write_text(str(app.os.getpid()))
        with mock.patch.object(app, "_resources_released", False):
            icon = app._start_tray("http://x", mock.Mock(), button, server)
            icon._message_handlers[self.tray_loop.WM_ENDSESSION](1, 0)
            self.assertEqual(button.colors, ["off"])
            self.assertFalse(app.LOCK_PATH.exists())
            server.shutdown.assert_called_once()


class WindowsRelaunchTests(unittest.TestCase):
    """os.execv on Windows spawns a child and exits instead of replacing the
    process, so Windows restarts by starting a fresh copy explicitly."""

    def _relaunch(self, frozen):
        flags = dict(DETACHED_PROCESS=0x8, CREATE_NEW_PROCESS_GROUP=0x200, CREATE_NEW_CONSOLE=0x10)
        with mock.patch.object(app.sys, "platform", "win32"), \
                mock.patch.object(app.sys, "frozen", frozen, create=True), \
                mock.patch.multiple(app.subprocess, create=True, **flags), \
                mock.patch.object(app.subprocess, "Popen") as popen, \
                mock.patch.object(app.os, "execv", side_effect=AssertionError("execv on Windows")):
            app._relaunch_app()
        popen.assert_called_once()
        return popen.call_args

    def test_packaged_exe_starts_detached_copy(self):
        args, kwargs = self._relaunch(frozen=True)
        self.assertEqual(args[0], [sys.executable])
        self.assertEqual(kwargs["creationflags"], 0x8 | 0x200)
        self.assertIs(kwargs["stdout"], app.subprocess.DEVNULL)

    def test_source_run_starts_copy_in_new_console(self):
        args, kwargs = self._relaunch(frozen=False)
        self.assertEqual(args[0], [sys.executable] + sys.argv)
        self.assertEqual(kwargs["creationflags"], 0x10)


if __name__ == "__main__":
    unittest.main()
