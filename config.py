"""Persisted, user-editable settings for the LED button.

Everything here is safe to write to a plain JSON file on disk -- Dexcom
credentials are NOT stored here, they stay in the OS keychain via the
`keyring` package (see get_credentials/save_credentials_to_keychain in
dexcom_led_button.py).
"""

import json
import threading
from datetime import time as dtime, timedelta
from pathlib import Path

CONFIG_DIR = Path.home() / ".dexcom_led_button"
CONFIG_PATH = CONFIG_DIR / "config.json"

# Monday=0 ... Sunday=6, matching datetime.weekday().
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

MUTE_DURATION_CHOICES_MINUTES = [10, 15, 20, 30, 45, 60, 90, 120]
REMINDER_DELAY_CHOICES_MINUTES = [5, 10, 15, 20, 30, 45, 60]

DEFAULTS = {
    "window_start": "11:30",
    "window_end": "20:00",
    "active_days": ALL_DAYS,
    "yellow_threshold": 150,
    "red_threshold": 200,
    "ramp_magnitude_mg_dl": 15,
    "mute_duration_minutes": 30,
    "reminder_delay_minutes": 20,
    "dexcom_region": "us",
}


def _parse_time(value):
    hour, minute = value.split(":")
    return dtime(int(hour), int(minute))


class Config:
    """Thread-safe settings store, backed by a JSON file."""

    def __init__(self, data=None):
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS)
        if data:
            self._data.update(data)

    @classmethod
    def load(cls):
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH) as f:
                    data = json.load(f)
                # The high-glucose light changed from orange to yellow, and
                # its setting was renamed with it; keep an existing value.
                if "orange_threshold" in data:
                    data.setdefault("yellow_threshold", data["orange_threshold"])
                    del data["orange_threshold"]
                return cls(data)
            except Exception:
                pass
        return cls()

    def save(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with self._lock:
            data = dict(self._data)
        tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        tmp_path.replace(CONFIG_PATH)

    def as_dict(self):
        with self._lock:
            return dict(self._data)

    def update(self, **kwargs):
        with self._lock:
            self._data.update(kwargs)
        self.save()

    def get_window_start(self):
        with self._lock:
            return _parse_time(self._data["window_start"])

    def get_window_end(self):
        with self._lock:
            return _parse_time(self._data["window_end"])

    def get_active_days(self):
        with self._lock:
            return set(self._data["active_days"])

    def get_yellow_threshold(self):
        with self._lock:
            return self._data["yellow_threshold"]

    def get_red_threshold(self):
        with self._lock:
            return self._data["red_threshold"]

    def get_ramp_magnitude(self):
        with self._lock:
            return self._data["ramp_magnitude_mg_dl"]

    def get_mute_duration_minutes(self):
        with self._lock:
            return self._data["mute_duration_minutes"]

    def get_mute_duration(self):
        return timedelta(minutes=self.get_mute_duration_minutes())

    def get_reminder_delay_minutes(self):
        with self._lock:
            return self._data["reminder_delay_minutes"]

    def get_reminder_delay(self):
        return timedelta(minutes=self.get_reminder_delay_minutes())

    def get_dexcom_region(self):
        with self._lock:
            return self._data["dexcom_region"]
