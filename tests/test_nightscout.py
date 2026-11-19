"""Tests for the Nightscout / xDrip+ glucose source (nightscout.py) and how the
app uses it.

Network tests only ever talk to throwaway HTTP servers on 127.0.0.1 that
behave like Nightscout and like xDrip+'s web service (auth quirks included) --
never a real site, phone, Dexcom account or button. The isolation set up by
test_core (temporary config folder, in-memory keyring) is reused, and must be
in place before dexcom_led_button is imported, so it's imported first.

Run from the project root:   python3 -m unittest discover -s tests -v
"""

import hashlib
import http.server
import json
import threading
import time
import traceback
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import test_core  # noqa: F401  -- isolation first
from test_core import FakeButton, FakeServer, _keychain, app, config_module, web_ui  # noqa: E402

import nightscout  # noqa: E402
from nightscout import Nightscout, NightscoutError, parse_address, parse_entries  # noqa: E402

NOW = datetime(2026, 11, 18, 18, 0, tzinfo=timezone.utc)
TOKEN = "school-a595d5f0aeff64c0"            # the shape Nightscout's Admin Tools hands out
API_SECRET = "a-long-nightscout-api-secret"
SHA1_OF_ABC = "915858afa2278f25527f192038108346164b47f2"  # xDrip+'s documented example


def entry(minutes_ago, sgv, direction="Flat", now=NOW):
    when = now - timedelta(minutes=minutes_ago)
    return {"_id": f"e{minutes_ago}", "device": "xDrip-LibreReceiver", "type": "sgv",
            "date": int(when.timestamp() * 1000), "dateString": when.isoformat(),
            "sgv": sgv, "direction": direction}


class FakeCGMServer:
    """Answers /api/v1/entries/sgv.json (and xDrip+'s /sgv.json), newest first.

    kind="nightscout": with a token and/or API secret set, wants ?token=TOKEN or
        an api-secret header holding sha1(API secret); 401 otherwise.
    kind="xdrip": xDrip+'s rules (XdripWebService.java) -- with a secret set,
        the api-secret header must be its sha1; with none set, sending the
        header at all is refused. 403 either way. HTTP/1.0, like xDrip+."""

    def __init__(self, kind="nightscout", secret=None, token=None):
        self.kind, self.secret, self.token = kind, secret, token
        self.entries = []
        self.requests = []
        self.override = None  # (status, body, content type) to answer instead
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_GET(self):
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)
                outer.requests.append((parts.path, query, dict(self.headers)))
                status, body, ctype = outer.answer(parts.path, query, self.headers.get("api-secret"))
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.address = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def answer(self, path, query, api_secret):
        if self.override:
            return self.override
        if path not in ("/api/v1/entries/sgv.json", "/sgv.json"):
            return 404, "Path not found", "text/plain"
        hashed = hashlib.sha1(self.secret.encode()).hexdigest() if self.secret else None
        if self.kind == "xdrip":
            if (api_secret is not None and api_secret.lower() != hashed) or (hashed and api_secret is None):
                return 403, "Authentication failed - check api-secret", "text/plain"
        elif self.secret or self.token:
            token_ok = bool(self.token) and query.get("token", [""])[0] == self.token
            secret_ok = bool(hashed) and (api_secret or "").lower() == hashed
            if not (token_ok or secret_ok):
                return 401, '{"status":401,"message":"Unauthorized"}', "application/json"
        count = int(query.get("count", ["10"])[0])
        return 200, json.dumps(self.entries[:count]), "application/json"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def client_for(server, secret="", **kwargs):
    client = Nightscout(server.address, secret, verify=False, **kwargs)
    client.now = lambda: NOW
    return client


def closed_port_address():
    """An address nothing listens on (a server started and stopped)."""
    server = FakeCGMServer()
    server.close()
    return server.address


class AddressTests(unittest.TestCase):
    def test_site_roots_and_pasted_api_urls(self):
        for typed, expected in [
            ("my-site.example.com", "https://my-site.example.com"),
            ("https://my-site.example.com/", "https://my-site.example.com"),
            ("HTTPS://My-Site.Example.com/api/v1/", "https://my-site.example.com"),
            ("https://my-site.example.com/api/v1/entries/sgv.json?count=10", "https://my-site.example.com"),
            ("https://my-site.example.com/api/v1/entries.json", "https://my-site.example.com"),
            ("https://example.com/ns/api/v1/", "https://example.com/ns"),       # a site under a path
            ("http://192.168.1.20:17580/sgv.json", "http://192.168.1.20:17580"),
        ]:
            self.assertEqual(parse_address(typed)[0], expected, typed)

    def test_no_scheme_means_https_except_local_or_xdrip(self):
        self.assertEqual(parse_address("my-site.example.com")[0], "https://my-site.example.com")
        self.assertEqual(parse_address("192.168.1.20:17580")[0], "http://192.168.1.20:17580")
        self.assertEqual(parse_address("10.0.0.5")[0], "http://10.0.0.5")
        self.assertEqual(parse_address("localhost:1337")[0], "http://localhost:1337")
        self.assertEqual(parse_address("8.8.8.8")[0], "https://8.8.8.8")  # parsed only, never fetched

    def test_where_the_credential_comes_from(self):
        # Typed in the secret field: a token by its shape, anything else a secret.
        self.assertEqual(parse_address("https://s.example.com", TOKEN)[1:], (TOKEN, True))
        self.assertEqual(parse_address("https://s.example.com", API_SECRET)[1:], (API_SECRET, False))
        # A ?token= link from Nightscout's Admin Tools.
        self.assertEqual(parse_address(f"https://s.example.com/?token={TOKEN}")[1:], (TOKEN, True))
        # xDrip+'s Nightscout upload format, pasted as is (the secret URL-encoded).
        self.assertEqual(parse_address("https://my%40Secret@s.example.com/api/v1/"),
                         ("https://s.example.com", "my@Secret", False))
        # What's typed in the field wins over what's in the address.
        self.assertEqual(parse_address(f"https://s.example.com/?token={TOKEN}", API_SECRET)[1:], (API_SECRET, False))
        self.assertEqual(parse_address("https://s.example.com")[1:], ("", False))

    def test_bad_addresses(self):
        for typed in ["", "   ", "ftp://s.example.com", "http://", "https://s.example.com:99999"]:
            with self.assertRaises(NightscoutError, msg=typed):
                parse_address(typed)


class EntryParsingTests(unittest.TestCase):
    def test_only_real_glucose_newest_first(self):
        readings = parse_entries([
            entry(10, 120, "FortyFiveUp"),
            entry(5, 5),                                         # a sensor error code, not glucose
            entry(4, 7.2),                                       # an mmol/L number: never read as mg/dL
            {"type": "mbg", "mbg": 100, "date": entry(3, 0)["date"]},  # a fingerstick, not a CGM reading
            entry(2, 39, "NOT COMPUTABLE"),                      # Dexcom's LOW
            {"sgv": 401, "date": entry(1, 0)["date"], "trend": 1},  # HIGH, numeric trend only
            entry(12, "abc"),
            {"sgv": 110},                                        # no time
            entry(10, 121, "FortyFiveUp"),                       # same reading from a second uploader
        ])
        self.assertEqual([(r.value, r.trend_direction) for r in readings],
                         [(401, "DoubleUp"), (39, "NotComputable"), (120, "FortyFiveUp")])
        self.assertTrue(all(r.datetime.tzinfo for r in readings))

    def test_seconds_and_iso_times(self):
        when = NOW - timedelta(minutes=3)
        by_seconds = parse_entries([{"sgv": 100, "date": int(when.timestamp())}])[0]
        by_string = parse_entries([{"sgv": 100, "dateString": "2026-11-18T17:57:00.000Z"}])[0]
        self.assertEqual(by_seconds.datetime, when)
        self.assertEqual(by_string.datetime, when)

    def test_trend_names_match_dexcoms(self):
        # The rising arrows HyLight looks for use the same names on all sources.
        self.assertTrue(app.RISING_TRENDS <= set(nightscout.TREND_NAMES))


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.close()

    def server(self, **kwargs):
        server = FakeCGMServer(**kwargs)
        server.entries = [entry(2, 150)]
        self.servers.append(server)
        return server

    def test_token_goes_as_a_query_parameter(self):
        server = self.server(token=TOKEN)
        self.assertEqual(client_for(server, TOKEN).get_current_glucose_reading().value, 150)
        path, query, headers = server.requests[-1]
        self.assertEqual(path, "/api/v1/entries/sgv.json")
        self.assertEqual(query["token"], [TOKEN])
        self.assertNotIn("api-secret", {k.lower() for k in headers})

    def test_api_secret_goes_as_its_sha1(self):
        server = self.server(secret=API_SECRET)
        self.assertEqual(client_for(server, API_SECRET).get_current_glucose_reading().value, 150)
        _, query, headers = server.requests[-1]
        self.assertNotIn("token", query)
        self.assertEqual(headers["api-secret"], hashlib.sha1(API_SECRET.encode()).hexdigest())

    def test_a_token_in_the_address_works(self):
        server = self.server(token=TOKEN)
        client = Nightscout(f"{server.address}/?token={TOKEN}", verify=True)  # verify: one real request
        self.assertEqual(server.requests[-1][1]["token"], [TOKEN])
        self.assertEqual(client.label, f"127.0.0.1:{server.port}")

    def test_xdrip_secret_matches_xdrips_own_example(self):
        server = self.server(kind="xdrip", secret="Abc")
        client_for(server, "Abc").get_current_glucose_reading()
        self.assertEqual(server.requests[-1][2]["api-secret"], SHA1_OF_ABC)

    def test_xdrip_without_a_secret_gets_no_header(self):
        server = self.server(kind="xdrip")
        with mock.patch.object(nightscout, "XDRIP_PORTS", {server.port}):
            self.assertEqual(client_for(server).get_current_glucose_reading().value, 150)
            self.assertNotIn("api-secret", {k.lower() for k in server.requests[-1][2]})
            with self.assertRaises(NightscoutError) as caught:
                client_for(server, "typed-anyway").get_current_glucose_reading()
        self.assertIn("leave it blank if xDrip+ has none", str(caught.exception))

    def test_refused_and_unreachable_never_quote_the_token(self):
        server = self.server(token=TOKEN)
        wrong = "school-0123456789abcdef"
        with self.assertRaises(NightscoutError) as refused:
            Nightscout(server.address, wrong)
        self.assertIn("refused access", str(refused.exception))
        with self.assertRaises(NightscoutError) as unreachable:
            Nightscout(f"{closed_port_address()}/?token={wrong}")
        self.assertIn("Couldn't reach", str(unreachable.exception))
        for caught in (refused, unreachable):
            # What log.exception would write to the log file testers send back.
            exc = caught.exception
            text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            self.assertNotIn(wrong, text)
            self.assertNotIn("0123456789abcdef", text)

    def test_not_nightscout(self):
        server = self.server()
        with self.assertRaises(NightscoutError) as missing:
            Nightscout(server.address + "/not-here")
        self.assertIn("no glucose data at that address", str(missing.exception))
        server.override = (200, "<html>Welcome!</html>", "text/html")
        with self.assertRaises(NightscoutError) as html:
            Nightscout(server.address)
        self.assertIn("didn't answer like Nightscout", str(html.exception))


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeCGMServer()
        self.addCleanup(self.server.close)
        self.client = client_for(self.server)

    def test_current_is_the_newest_from_the_last_10_minutes(self):
        self.server.entries = [entry(2, 150), entry(7, 140)]
        self.assertEqual(self.client.get_current_glucose_reading().value, 150)
        self.server.entries = [entry(11, 150)]
        self.assertIsNone(self.client.get_current_glucose_reading())  # stale: like pydexcom
        self.server.entries = [entry(-20, 99), entry(-3, 150), entry(4, 140)]  # a phone clock running ahead
        self.assertEqual(self.client.get_current_glucose_reading().value, 150)
        self.server.entries = []
        self.assertIsNone(self.client.get_current_glucose_reading())

    def test_five_minute_data_passes_through_unchanged(self):
        self.server.entries = [entry(m, 100 + m) for m in range(0, 60, 5)]
        readings = self.client.get_glucose_readings(minutes=30, max_count=6)
        self.assertEqual([round((NOW - r.datetime).total_seconds() / 60) for r in readings], [0, 5, 10, 15, 20, 25])

    def test_one_minute_data_comes_back_on_a_five_minute_rhythm(self):
        self.server.entries = [entry(m, 100 + m) for m in range(0, 45)]
        readings = self.client.get_glucose_readings(minutes=30, max_count=6)
        self.assertEqual([round((NOW - r.datetime).total_seconds() / 60) for r in readings], [0, 5, 10, 15, 20, 25])
        self.assertGreaterEqual(int(self.server.requests[-1][1]["count"][0]), 36)  # enough for 1-minute data

    def test_newest_returned_moves_on_only_every_five_minutes(self):
        newest_seen = []
        for minute in range(11):
            now = NOW + timedelta(minutes=minute)
            self.server.entries = [entry(m, 120, now=now) for m in range(0, 40)]
            self.client.now = lambda now=now: now
            newest = self.client.get_glucose_readings(30, 6)[0].datetime
            newest_seen.append(round((newest - NOW).total_seconds() / 60))
        self.assertEqual(newest_seen, [0, 0, 0, 0, 0, 5, 5, 5, 5, 5, 10])


class RampWithOneMinuteDataTests(unittest.TestCase):
    """detect_ramp on a Libre-style reading every minute should behave as it
    does on Dexcom's 5-minute data: a rise confirmed on 2 samples ~5 minutes
    apart, measured against a reading ~15 minutes older."""

    def setUp(self):
        self.server = FakeCGMServer()
        self.addCleanup(self.server.close)
        self.client = client_for(self.server)
        self.cfg = test_core.fresh_config()
        self.cfg.update(ramp_sensitivity="medium")  # 14 mg/dL over ~15 minutes
        app.state.__init__()

    def poll_minutes(self, glucose_at, minutes, cadence=1):
        """detect_ramp once a minute; glucose_at(t) is mg/dL t minutes after NOW."""
        results = []
        for minute in range(minutes):
            now = NOW + timedelta(minutes=minute)
            newest = minute - minute % cadence
            self.server.entries = [entry(minute - t, glucose_at(t), now=now)
                                   for t in range(newest, newest - 45, -cadence)]
            self.client.now = lambda now=now: now
            results.append(app.detect_ramp(self.client, self.cfg))
        return results

    def test_a_steady_climb_is_confirmed_after_about_five_minutes_not_one(self):
        climb = lambda t: 110 + 1.2 * t  # 18 mg/dL per 15 minutes, Flat arrows
        self.assertEqual(self.poll_minutes(climb, 7), [False] * 5 + [True, True])

    def test_same_timing_as_five_minute_data(self):
        climb = lambda t: 110 + 1.2 * t
        app.state.__init__()
        five = self.poll_minutes(climb, 7, cadence=5)
        app.state.__init__()
        self.client._anchor = None
        one = self.poll_minutes(climb, 7, cadence=1)
        self.assertEqual(one, five)

    def test_noise_without_a_rise_never_confirms(self):
        zigzag = lambda t: 120 + (6 if t % 2 else -6)
        self.assertFalse(any(self.poll_minutes(zigzag, 20)))

    def test_a_rise_too_small_for_the_setting_never_confirms(self):
        gentle = lambda t: 110 + 0.8 * t  # 12 mg/dL per 15 minutes, under "medium"
        self.assertFalse(any(self.poll_minutes(gentle, 20)))


class FlakyClient:
    """get_current_glucose_reading raises a NightscoutError (or returns None)."""

    source_name = "xDrip+"

    def __init__(self, error=True):
        self.error, self.calls = error, 0

    def get_current_glucose_reading(self):
        self.calls += 1
        if self.error:
            raise NightscoutError("Couldn't reach 192.168.1.20:17580.")
        return None

    def get_glucose_readings(self, minutes=30, max_count=6):
        return []


class StaleDataTests(unittest.TestCase):
    """A light (or the status line's BG) mustn't outlive its reading's 10
    minutes just because the source went quiet or out of reach."""

    def run_one_poll(self, client):
        button, wake, stop = FakeButton(), threading.Event(), threading.Event()
        thread = threading.Thread(target=app.glucose_loop, args=(client, button, test_core.fresh_config(), wake, stop),
                                  daemon=True)
        thread.start()
        deadline = time.time() + 3
        while client.calls == 0 and time.time() < deadline:
            time.sleep(0.005)
        time.sleep(0.05)
        stop.set()
        wake.set()
        thread.join(2)
        return button.colors

    def cached(self, minutes_ago):
        app.state.__init__()
        app.state.set_latest_reading(160, False, datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))
        app.state.set_currently_lit("yellow")

    def test_unreachable_source_turns_an_old_light_off(self):
        self.cached(minutes_ago=11)
        self.assertEqual(self.run_one_poll(FlakyClient()), ["off"])
        self.assertEqual(app.state.get_latest_reading(), (None, False))
        self.assertEqual(app.state.get_currently_lit(), "off")
        self.assertEqual(app.state.get_data_problem(), "Couldn't reach 192.168.1.20:17580.")

    def test_a_short_blip_keeps_the_light(self):
        self.cached(minutes_ago=3)
        self.assertEqual(self.run_one_poll(FlakyClient()), [])
        self.assertEqual(app.state.get_latest_reading(), (160, False))
        self.assertEqual(app.state.get_currently_lit(), "yellow")

    def test_no_current_reading_drops_the_old_value(self):
        self.cached(minutes_ago=3)
        self.assertEqual(self.run_one_poll(FlakyClient(error=False)), ["off"])
        self.assertEqual(app.state.get_latest_reading(), (None, False))
        self.assertEqual(app.state.get_data_problem(), "no reading in the last 10 minutes")


class ControllerTests(unittest.TestCase):
    def setUp(self):
        _keychain.clear()
        config_module.CONFIG_PATH.unlink(missing_ok=True)
        app.state.__init__()
        self.server = FakeCGMServer(token=TOKEN)
        self.server.entries = [entry(0, 130)]
        self.addCleanup(self.server.close)
        patcher = mock.patch.object(app, "Dexcom", test_core.FakeDexcom)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = test_core.fresh_config()
        self.button = FakeButton()
        self.controller = app.MonitorController(self.button, self.cfg)
        self.addCleanup(self.controller.disconnect)

    def test_switching_sources_keeps_one_button_thread(self):
        with mock.patch.object(app, "button_loop") as button_loop:
            self.controller.start("wearer", "x", "us")
            self.controller.disconnect()
            self.controller.start_nightscout(self.server.address, TOKEN)
            self.controller.disconnect()
            self.controller.start("wearer", "x", "us")
            time.sleep(0.05)
        self.assertEqual(button_loop.call_count, 1)

    def test_a_loop_stuck_on_its_source_stops_after_a_quick_reconnect(self):
        release = threading.Event()

        class SlowClient:
            calls = 0

            def get_current_glucose_reading(self):
                SlowClient.calls += 1
                release.wait(3)
                return test_core.FakeReading(300)  # would light red

            def get_glucose_readings(self, minutes=30, max_count=6):
                return []

        with mock.patch.object(app, "Dexcom", lambda **kw: SlowClient()):
            self.controller.start("wearer", "x", "us")
            deadline = time.time() + 2
            while SlowClient.calls == 0 and time.time() < deadline:
                time.sleep(0.005)
            self.controller.disconnect()
        self.controller.start_nightscout(self.server.address, TOKEN)  # the old loop is still waiting
        self.button.colors.clear()
        release.set()
        time.sleep(0.3)
        self.assertEqual(SlowClient.calls, 1)
        self.assertNotIn("red", self.button.colors)

    def test_resume_saved_nightscout_even_when_its_unreachable_now(self):
        address = closed_port_address()  # e.g. the phone isn't on the Wi-Fi yet
        app.save_nightscout_credentials(address, TOKEN)
        self.cfg.update(glucose_source="nightscout")
        app.resume_saved_source(self.controller, self.cfg)
        self.assertTrue(self.controller.is_running())
        self.assertEqual(self.controller.source, "nightscout")
        self.assertEqual(app.get_nightscout_credentials(), (address, TOKEN))  # not forgotten

    def test_resume_saved_dexcom_uses_the_saved_region(self):
        seen = {}

        def fake_dexcom(username=None, password=None, region=None):
            seen["region"] = region
            return test_core.FakeDexcom()

        app.save_credentials_to_keychain("wearer", "x")
        self.cfg.update(dexcom_region="ous")
        with mock.patch.object(app, "Dexcom", fake_dexcom), mock.patch.dict(app.os.environ, {}, clear=False):
            app.os.environ.pop("DEXCOM_REGION", None)
            app.resume_saved_source(self.controller, self.cfg)
        self.assertEqual(seen["region"], "ous")
        self.assertTrue(self.controller.is_running())


class NightscoutWebFlowTests(unittest.TestCase):
    def setUp(self):
        _keychain.clear()
        config_module.CONFIG_PATH.unlink(missing_ok=True)
        app.state.__init__()
        self.server = FakeCGMServer(token=TOKEN)
        self.server.entries = [entry(0, 130)]
        self.addCleanup(self.server.close)
        self.cfg = test_core.fresh_config()
        self.controller = app.MonitorController(FakeButton(), self.cfg)
        self.controller.web_server = FakeServer()
        self.addCleanup(self.controller.disconnect)
        self.client = web_ui.create_app(self.cfg, self.controller).test_client()
        self.host = f"127.0.0.1:{self.server.port}"

    def test_connect_from_the_login_page(self):
        self.cfg.update(onboarding_done=True)
        login = self.client.get("/").get_data(as_text=True)
        self.assertIn('name="ns_address"', login)
        self.assertIn('id="src-dexcom" checked', login)  # Dexcom stays the default tab
        r = self.client.post("/login/nightscout", data={"ns_address": self.server.address, "ns_secret": TOKEN})
        self.assertEqual(r.status_code, 302)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn(f"Connected to Nightscout at {self.host}", page)
        self.assertIn("Disconnect Nightscout", page)
        self.assertNotIn("Disconnect Dexcom account", page)
        self.assertEqual(self.cfg.get_glucose_source(), "nightscout")
        self.assertEqual(app.get_nightscout_credentials(), (self.server.address, TOKEN))
        status = self.client.get("/status").json
        self.assertEqual((status["source"], status["source_name"]), ("nightscout", "Nightscout"))

    def test_a_refused_token_explains_and_saves_nothing(self):
        self.cfg.update(onboarding_done=True)
        r = self.client.post("/login/nightscout",
                             data={"ns_address": self.server.address, "ns_secret": "school-0123456789abcdef"})
        page = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("refused access: check the access token or API secret", page)
        self.assertIn('id="src-ns" checked', page)
        self.assertIn(f'value="{self.server.address}"', page)
        self.assertNotIn("0123456789abcdef", page)
        self.assertFalse(self.controller.is_running())
        self.assertEqual(app.get_nightscout_credentials(), (None, ""))

    def test_disconnect_forgets_every_saved_source(self):
        self.cfg.update(onboarding_done=True)
        app.save_credentials_to_keychain("wearer", "x")
        self.client.post("/login/nightscout", data={"ns_address": self.server.address, "ns_secret": TOKEN})
        self.client.post("/disconnect")
        self.assertFalse(self.controller.is_running())
        self.assertEqual(_keychain, {})
        self.assertIn('id="src-ns" checked', self.client.get("/").get_data(as_text=True))  # last source's tab

    def test_connect_from_the_tour_cover(self):
        cover = self.client.get("/setup/1").get_data(as_text=True)
        self.assertIn('name="ns_address"', cover)
        self.assertIn('name="source" value="nightscout"', cover)
        r = self.client.post("/setup/1", data={"source": "nightscout", "ns_address": self.server.address,
                                                "ns_secret": TOKEN})
        self.assertTrue(r.headers["Location"].endswith("/setup/2"))
        self.assertIn(f"Connected to Nightscout at <b>{self.host}</b>", self.client.get("/setup/1").get_data(as_text=True))

    def test_status_says_why_there_is_no_bg(self):
        self.cfg.update(onboarding_done=True)
        half_hour_ago = datetime.now(timezone.utc) - timedelta(minutes=30)  # the phone stopped uploading
        self.server.entries = [entry(0, 130, now=half_hour_ago)]
        self.controller.start_nightscout(self.server.address, TOKEN)
        deadline = time.time() + 3
        while not app.state.get_data_problem() and time.time() < deadline:
            time.sleep(0.01)
        status = self.client.get("/status").json
        self.assertEqual(status["data_problem"], "no reading in the last 10 minutes")
        self.assertIsNone(status["latest_value"])


if __name__ == "__main__":
    unittest.main()
