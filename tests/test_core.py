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
from datetime import datetime
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
        app.handle_long_press(datetime.now(), button, fresh_config())
        self.assertEqual(button.colors, ["off"])
        self.assertTrue(app.state.is_muted(datetime.now()))


class WebFlowTests(unittest.TestCase):
    def setUp(self):
        _keychain.clear()
        config_module.CONFIG_PATH.unlink(missing_ok=True)
        app.state.__init__()
        patcher = mock.patch.object(app, "Dexcom", FakeDexcom)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = fresh_config()
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
            "yellow_threshold": "150", "red_threshold": "200", "ramp_magnitude_mg_dl": "15",
            "mute_duration_minutes": "30", "reminder_delay_minutes": "20",
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
        self.assertIn('class="btn-primary"', page)

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


if __name__ == "__main__":
    unittest.main()
