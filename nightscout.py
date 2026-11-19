"""Glucose readings from Nightscout, or straight from xDrip+ on the same Wi-Fi.

xDrip+'s built-in web server answers the same request as a Nightscout site,
/api/v1/entries/sgv.json, in the same format, so this one small client covers:

  - A Nightscout site. Works from anywhere (home, school, office). xDrip+,
    xDrip4iOS, Juggluco, AAPS, Loop, Trio and others upload to it, whatever
    the sensor (Dexcom, Libre, ...).
  - xDrip+ itself, on an Android phone on the same network: xDrip+ Settings >
    Inter-app settings > xDrip Web Service, plus Open Web Service so other
    devices may connect. Port 17580, plain http. No cloud involved.

`Nightscout` offers the two calls HyLight makes on pydexcom's `Dexcom`, with
the same meaning, so the glucose loop doesn't care which one it holds:

  get_current_glucose_reading()     the newest reading from the last 10
                                    minutes, or None (pydexcom 0.5.1's rule)
  get_glucose_readings(minutes, n)  newest first, on a 5-minute rhythm

Facts this relies on (checked against Nightscout's and xDrip+'s source, Nov 2026):
  - `sgv` is always mg/dL in both, whatever units the person sees. Values
    under 39 are sensor error codes, not glucose, so they're skipped (which
    also means a mistaken mmol/L upload can never pass for a real reading).
    39 is Dexcom's LOW and 401 its HIGH; both are kept.
  - `date` is the reading time in epoch milliseconds. `direction` uses the
    same trend names as Dexcom Share ("SingleUp", "FortyFiveUp", ...).
  - Nightscout takes an access token as `?token=` (a "readable" role token
    is all HyLight needs) or the API secret as an `api-secret` header holding
    its SHA-1 hex digest. The API secret can also change data, so a token is
    the safer thing to keep on, say, a classroom computer.
  - xDrip+ checks its web service secret the same way (SHA-1 `api-secret`
    header), but only when one is set: if xDrip+ has no secret, a request that
    sends the header anyway is refused (403).
  - Both honour `count` and answer newest first.
  - Libre 2/3 through xDrip+ or Juggluco can upload a reading every minute.
    HyLight's ramp rules were built on Dexcom's 5-minute rhythm (a rise must
    hold for 2 consecutive samples, against a reading ~15 minutes older), so
    history is handed back on that rhythm -- see get_glucose_readings.

Errors come out as NightscoutError, with a message meant for the person that
never includes the token or secret. (requests' own errors would: they quote
the full URL, ?token= and all, and the log file is something testers send.)
"""

import hashlib
import ipaddress
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

import requests

CURRENT_MAX_AGE = timedelta(minutes=10)       # pydexcom's "current" reading
CLOCK_SKEW = timedelta(minutes=5)             # a phone clock may run this far ahead
SAMPLE_GAP = timedelta(minutes=4, seconds=30)  # Dexcom's 5-minute rhythm, with slack
MIN_SGV, MAX_SGV = 39, 1000                   # under 39 = sensor error codes
TIMEOUT_SECONDS = 15                          # same as the app's socket default
XDRIP_PORTS = {17580, 17581}                  # xDrip+'s http and https ports
ENTRIES_PATH = "/api/v1/entries/sgv.json"     # xDrip+ answers this path too

# Nightscout access tokens are "<subject name>-<16 hex>", e.g. "school-a595d5f0aeff64c0".
_TOKEN_SHAPE = re.compile(r"^[a-z0-9][a-z0-9_.-]*-[0-9a-f]{16}$", re.IGNORECASE)
# API paths people paste from other apps; the site root is what's wanted.
_API_TAIL = re.compile(r"/(api/v1(/entries(/sgv)?)?(\.json)?|sgv\.json|pebble)?/*$", re.IGNORECASE)

# Dexcom Share's names, in Dexcom's numeric trend order (as pydexcom).
TREND_NAMES = ["None", "DoubleUp", "SingleUp", "FortyFiveUp", "Flat",
               "FortyFiveDown", "SingleDown", "DoubleDown", "NotComputable", "RateOutOfRange"]
_TREND_ALIASES = {
    "NONE": "None",
    "NOT COMPUTABLE": "NotComputable", "NOT_COMPUTABLE": "NotComputable",
    "RATE OUT OF RANGE": "RateOutOfRange", "OUT OF RANGE": "RateOutOfRange",
    "OUT_OF_RANGE": "RateOutOfRange",
}


class NightscoutError(Exception):
    """A problem to show the person as is. Never contains the token or secret."""


class Reading:
    """The parts of pydexcom's GlucoseReading that HyLight uses."""

    __slots__ = ("value", "trend_direction", "datetime")

    def __init__(self, value, trend_direction, when):
        self.value = value                    # mg/dL
        self.trend_direction = trend_direction
        self.datetime = when                  # timezone-aware, like pydexcom's

    def __repr__(self):
        return f"Reading({self.value}, {self.trend_direction}, {self.datetime.isoformat(timespec='seconds')})"


def _guess_scheme(text):
    """No scheme typed: xDrip+'s port or a local address means http, else https."""
    hostport = text.split("/", 1)[0].rpartition("@")[2]
    host, port = hostport, ""
    if hostport.count(":") == 1:
        host, _, port = hostport.partition(":")
    if port.isdigit() and int(port) in XDRIP_PORTS:
        return "http"
    if host.lower() == "localhost" or host.lower().endswith(".local"):
        return "http"
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return "https"
    return "http" if (ip.is_private or ip.is_loopback or ip.is_link_local) else "https"


def parse_address(address, secret=""):
    """Turns what the person typed into (base_url, credential, as_token).

    Takes a site root ("my-site.example.com", "https://my-site.example.com/"),
    an API URL pasted from another app (".../api/v1/", ".../sgv.json"), a
    "?token=..." link from Nightscout's Admin Tools, or xDrip+'s Nightscout
    upload format "https://SECRET@my-site.example.com/api/v1/".

    The credential is the first of: what was typed in the secret field, a
    ?token= in the address, a SECRET@ in the address. as_token says to send it
    as Nightscout's ?token= (it came as ?token=, or looks like an access token)
    rather than as a hashed api-secret header (an API secret, or xDrip+'s
    web service secret)."""
    text = (address or "").strip()
    if not text:
        raise NightscoutError("Enter the address of your Nightscout site, or of the phone running xDrip+.")
    if "://" not in text:
        text = _guess_scheme(text) + "://" + text
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise NightscoutError("That doesn't look like a web address.") from None
    host = parts.hostname
    if parts.scheme not in ("http", "https") or not host:
        raise NightscoutError("That doesn't look like a web address.")
    netloc = (f"[{host}]" if ":" in host else host) + (f":{port}" if port else "")
    base_url = urlunsplit((parts.scheme, netloc, _API_TAIL.sub("", parts.path), "", ""))

    typed = (secret or "").strip()
    url_token = (parse_qs(parts.query).get("token") or [""])[0].strip()
    userinfo = unquote(parts.netloc.rpartition("@")[0]).strip()
    if typed:
        return base_url, typed, bool(_TOKEN_SHAPE.match(typed))
    if url_token:
        return base_url, url_token, True
    return base_url, userinfo, bool(_TOKEN_SHAPE.match(userinfo))


def _reading_time(item):
    try:
        stamp = float(item["date"])
        if stamp < 1e11:  # seconds, not milliseconds
            stamp *= 1000
        return datetime.fromtimestamp(stamp / 1000, tz=timezone.utc)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        pass
    for key in ("dateString", "sysTime"):
        text = item.get(key)
        if isinstance(text, str) and text.strip():
            try:
                when = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
            except ValueError:
                continue
            return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return None


def _trend_name(item):
    direction = item.get("direction")
    if isinstance(direction, str) and direction.strip():
        name = direction.strip()
        return _TREND_ALIASES.get(name.upper(), name)
    trend = item.get("trend")
    if isinstance(trend, int) and not isinstance(trend, bool) and 0 <= trend < len(TREND_NAMES):
        return TREND_NAMES[trend]
    return "None"


def parse_entries(items):
    """Valid glucose readings from an sgv.json answer: newest first, one per
    timestamp (a site fed by two uploaders can hold the same reading twice)."""
    readings = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("type", "sgv") != "sgv":
            continue
        try:
            value = float(item["sgv"])
        except (KeyError, TypeError, ValueError):
            continue
        when = _reading_time(item)
        if when is None or not MIN_SGV <= value <= MAX_SGV:
            continue
        readings.append(Reading(int(round(value)), _trend_name(item), when))
    readings.sort(key=lambda r: r.datetime, reverse=True)
    unique = []
    for reading in readings:
        if not unique or unique[-1].datetime != reading.datetime:
            unique.append(reading)
    return unique


class Nightscout:
    """A Nightscout site, or xDrip+'s web service, standing in for pydexcom's
    Dexcom. With verify=True (a new connection), the constructor checks the
    address and credential with one request and raises NightscoutError if
    that fails, as Dexcom() does for a bad login."""

    def __init__(self, address, secret="", verify=True):
        self.base_url, credential, as_token = parse_address(address, secret)
        parts = urlsplit(self.base_url)
        self.is_xdrip = parts.port in XDRIP_PORTS
        self.source_name = "xDrip+" if self.is_xdrip else "Nightscout"
        self.label = parts.netloc + parts.path  # for the page and the log: no credential
        self._url = self.base_url + ENTRIES_PATH
        self._params = {"token": credential} if credential and as_token else {}
        self._headers = {"Accept": "application/json"}
        if credential and not as_token:
            self._headers["api-secret"] = hashlib.sha1(credential.encode("utf-8")).hexdigest()
        self._session = requests.Session()
        self._anchor = None  # see get_glucose_readings
        self.now = lambda: datetime.now(timezone.utc)  # replaceable in tests
        if verify:
            self._fetch(1)

    def _fetch(self, count):
        try:
            resp = self._session.get(self._url, params=dict(self._params, count=count),
                                     headers=self._headers, timeout=TIMEOUT_SECONDS)
        except requests.exceptions.SSLError:
            raise NightscoutError(f"Couldn't make a secure connection to {self.label}.") from None
        except requests.exceptions.Timeout:
            raise NightscoutError(f"{self.label} didn't answer in time.") from None
        except requests.exceptions.RequestException:
            hint = (" Is the phone on the same Wi-Fi, with xDrip+'s web service and Open Web Service on?"
                    if self.is_xdrip else " Check the address and the internet connection.")
            raise NightscoutError(f"Couldn't reach {self.label}.{hint}") from None

        if resp.status_code in (401, 403):
            if self.is_xdrip:
                blank = " (leave it blank if xDrip+ has none)" if "api-secret" in self._headers else ""
                raise NightscoutError(f"xDrip+ refused access: the secret must match its web service secret{blank}.")
            raise NightscoutError(f"{self.label} refused access: check the access token or API secret.")
        if resp.status_code == 404:
            raise NightscoutError(f"{self.label} has no glucose data at that address. "
                                  "Check it's your Nightscout site, or the phone running xDrip+.")
        if resp.status_code != 200:
            raise NightscoutError(f"{self.label} answered with an error (HTTP {resp.status_code}).")
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, list):
            raise NightscoutError(f"{self.label} didn't answer like Nightscout or xDrip+. Check the address.")
        return parse_entries(data)

    def _within(self, readings, window):
        now = self.now()
        return [r for r in readings if -CLOCK_SKEW <= now - r.datetime <= window]

    def get_current_glucose_reading(self):
        """Newest reading from the last 10 minutes, or None. A few are asked
        for in case the newest entries are error codes."""
        recent = self._within(self._fetch(4), CURRENT_MAX_AGE)
        return recent[0] if recent else None

    def get_glucose_readings(self, minutes=30, max_count=6):
        """Readings from the last `minutes`, newest first, spaced like Dexcom's.

        A 1-minute CGM is thinned to readings at least SAMPLE_GAP apart, and
        the newest one returned (the "anchor") only moves on once a reading
        SAMPLE_GAP newer arrives. The glucose loop counts a new sample each
        time that newest timestamp changes, so this keeps "2 consecutive
        samples" meaning ~5 minutes, and "the newest reading at least 14
        minutes older" meaning ~15 minutes, whatever the sensor. A 5-minute
        CGM passes through unchanged."""
        recent = self._within(self._fetch(minutes + 6), timedelta(minutes=minutes))
        if not recent:
            return []
        newest = recent[0].datetime
        if self._anchor is None or newest < self._anchor or newest - self._anchor >= SAMPLE_GAP:
            self._anchor = newest
        start = next((i for i, r in enumerate(recent) if r.datetime <= self._anchor), 0)
        series = [recent[start]]
        for reading in recent[start + 1:]:
            if series[-1].datetime - reading.datetime >= SAMPLE_GAP:
                series.append(reading)
        return series[:max_count]
