"""Parsing + validation of the settings form, shared by the full settings
page and the step-by-step setup tour (which each submit only a subset)."""

import re

from config import ON_CALL_DURATION_CHOICES_MINUTES, RAMP_SENSITIVITY_MG_DL

GROUPS = ("window", "thresholds", "ramp", "on_call")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def parse_settings_form(form, groups=GROUPS):
    """Returns (values, error) for the requested setting groups: values is
    a dict ready for Config.update(), error a message or None (in which
    case nothing should be saved)."""
    values = {}
    try:
        if "window" in groups:
            start, end = form["window_start"], form["window_end"]
            if not (_TIME.match(start) and _TIME.match(end)):
                return None, "Enter the start and end as times of day"
            if end <= start:
                return None, "The end time must be later than the start time"
            days = sorted({int(d) for d in form.getlist("active_days")})
            if not days or any(d not in range(7) for d in days):
                return None, "Select at least one active day"
            values.update(window_start=start, window_end=end, active_days=days)

        if "thresholds" in groups:
            yellow, red = int(form["yellow_threshold"]), int(form["red_threshold"])
            if not (40 <= yellow <= 400 and 40 <= red <= 400):
                return None, "Thresholds must be between 40 and 400 mg/dL"
            if red <= yellow:
                return None, "Red threshold must be higher than yellow"
            values.update(yellow_threshold=yellow, red_threshold=red)

        if "ramp" in groups:
            sensitivity = form["ramp_sensitivity"]
            if sensitivity not in RAMP_SENSITIVITY_MG_DL:
                return None, "Not a valid ramp sensitivity"
            values.update(ramp_sensitivity=sensitivity)

        if "on_call" in groups:
            minutes = int(form["on_call_minutes"])
            if minutes not in ON_CALL_DURATION_CHOICES_MINUTES:
                return None, "Not a valid on-demand length"
            values.update(on_call_minutes=minutes)
    except (KeyError, ValueError):
        return None, "Some values were missing or not valid numbers"
    return values, None
