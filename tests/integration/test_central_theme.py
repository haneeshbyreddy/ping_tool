"""Superadmin colour overrides end-to-end: who may set them, and the fact that
they reach the page as injected CSS rather than as an API call the SPA has to
make after mount."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.config import Config
from wisp.central import auth
from wisp.central.store import CentralStore
from wisp.central.server import make_server
from support import RecordingNotifier


class CentralThemeHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "c.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, None, "root", "rootpassword")
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        self.store.set_org("ispA", name="Acme")
        self.server = make_server(self.cfg, self.store, notifier=RecordingNotifier())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    def _req(self, method, path, body=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body); headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            return resp.status, (json.loads(raw) if raw else {})
        except ValueError:
            return resp.status, raw

    def _login(self, username, password):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/login",
                     body=json.dumps({"username": username, "password": password}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        cookie = resp.getheader("Set-Cookie").split(";")[0]
        conn.close()
        return cookie

    # ----- access ------------------------------------------------------------

    def test_org_owner_cannot_set_colours(self):
        """The look is server-wide, so an org owner must not reach it — this is
        the same boundary as the Google Maps key, not an org preference."""
        cookie = self._login("owner", "ownerpassword")
        status, _ = self._req("POST", "/api/admin/settings",
                              {"theme_overrides": {"dark": {"--card": "#ff0000"}}},
                              cookie=cookie)
        self.assertEqual(status, 403)
        self.assertEqual(self.store.get_setting("theme_overrides"), None)

    # ----- round trip --------------------------------------------------------

    def test_superadmin_round_trip(self):
        cookie = self._login("root", "rootpassword")
        status, _ = self._req("POST", "/api/admin/settings", {
            "theme_overrides": {"dark": {"--card": "#222831",
                                         "--primary": "#8ec5d6"}}},
            cookie=cookie)
        self.assertEqual(status, 200)
        status, body = self._req("GET", "/api/admin/settings", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["theme_overrides"],
                         {"dark": {"--card": "#222831", "--primary": "#8ec5d6"}})

    def test_omitting_the_key_leaves_colours_alone(self):
        """Saving the Google Maps key must not wipe the palette — the two live
        on the same endpoint, and every other field there is 'absent = leave
        it', so colours have to behave the same way."""
        cookie = self._login("root", "rootpassword")
        self._req("POST", "/api/admin/settings",
                  {"theme_overrides": {"dark": {"--card": "#222831"}}}, cookie=cookie)
        self._req("POST", "/api/admin/settings",
                  {"google_maps_key": "AIzaTest"}, cookie=cookie)
        _, body = self._req("GET", "/api/admin/settings", cookie=cookie)
        self.assertEqual(body["theme_overrides"], {"dark": {"--card": "#222831"}})

    def test_empty_dict_resets_to_the_shipped_palette(self):
        """`{}` is the reset button and must be distinguishable from absent."""
        cookie = self._login("root", "rootpassword")
        self._req("POST", "/api/admin/settings",
                  {"theme_overrides": {"dark": {"--card": "#222831"}}}, cookie=cookie)
        self._req("POST", "/api/admin/settings",
                  {"theme_overrides": {}}, cookie=cookie)
        _, body = self._req("GET", "/api/admin/settings", cookie=cookie)
        self.assertEqual(body["theme_overrides"], {})
        self.assertIsNone(self.store.get_setting("theme_overrides"))

    # ----- delivery ----------------------------------------------------------

    def test_colours_are_injected_into_the_spa_head_without_a_session(self):
        """The whole delivery argument in one test.

        Injected (not fetched) so there is no flash of the default palette on
        load, and reachable with NO cookie so the login and billing-lock
        screens — which render before any session exists — are themed too.
        """
        cookie = self._login("root", "rootpassword")
        self._req("POST", "/api/admin/settings",
                  {"theme_overrides": {"dark": {"--card": "#222831"}}}, cookie=cookie)
        status, raw = self._req("GET", "/app")
        self.assertEqual(status, 200)
        html = raw.decode("utf-8") if isinstance(raw, bytes) else json.dumps(raw)
        self.assertIn('<style id="wisp-theme">', html)
        self.assertIn(":root.dark{--card:#222831;}", html)
        # before </head>, so it beats the bundle stylesheet on source order
        self.assertLess(html.index('id="wisp-theme"'), html.index("</head>"))

    def test_stock_install_injects_nothing(self):
        for path in ("/app", "/"):
            status, raw = self._req("GET", path)
            self.assertEqual(status, 200)
            html = raw.decode("utf-8") if isinstance(raw, bytes) else json.dumps(raw)
            self.assertNotIn('<style id="wisp-theme">', html, path)

    def test_the_marketing_page_is_themed_too(self):
        """`/` shipped its own hardcoded palette, so repainting the product left
        the front door on the old colours. It reads the same injected block the
        SPA does — see landing.html's --lp-* layer for the consuming half, and
        test_theme.py:LandingArtifactTest for what keeps that half honest."""
        cookie = self._login("root", "rootpassword")
        self._req("POST", "/api/admin/settings",
                  {"theme_overrides": {"dark": {"--background": "#141414",
                                                "--card": "#1f1f1f",
                                                "--primary": "#6196a5"}}},
                  cookie=cookie)
        status, raw = self._req("GET", "/")
        self.assertEqual(status, 200)
        html = raw.decode("utf-8") if isinstance(raw, bytes) else json.dumps(raw)
        self.assertIn('<style id="wisp-theme">', html)
        self.assertIn("--background:#141414;", html)
        self.assertLess(html.index('id="wisp-theme"'), html.index("</head>"))

    def test_a_light_only_palette_leaves_the_marketing_page_alone(self):
        """The page is `<html class="dark">`, so it takes the dark half and only
        the dark half. Getting this backwards is the regression that blew out
        dark mode in the SPA — there it showed as light text on white, here it
        would be a black-on-black front door."""
        cookie = self._login("root", "rootpassword")
        self._req("POST", "/api/admin/settings",
                  {"theme_overrides": {"light": {"--background": "#ffffff"}}},
                  cookie=cookie)
        status, raw = self._req("GET", "/")
        self.assertEqual(status, 200)
        html = raw.decode("utf-8") if isinstance(raw, bytes) else json.dumps(raw)
        self.assertIn(":root:not(.dark){--background:#ffffff;}", html)
        self.assertNotIn(":root.dark{", html)

    def test_hostile_stored_value_cannot_escape_the_style_element(self):
        """Defence in depth: even with a bad row written straight to the DB
        (bypassing the API's validation entirely), nothing that could close the
        <style> element or inject markup may reach the page."""
        self.store.set_setting("theme_overrides", json.dumps(
            {"dark": {"--card": "#fff</style><script>alert(1)</script>"}}))
        status, raw = self._req("GET", "/app")
        self.assertEqual(status, 200)
        html = raw.decode("utf-8") if isinstance(raw, bytes) else json.dumps(raw)
        self.assertNotIn("<script>alert(1)</script>", html)


if __name__ == "__main__":
    unittest.main()
