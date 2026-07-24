"""Per-link map cartography: line colour and where the bandwidth chip rides it.

Both live on link_routes — same key, same "one link" grain as the drawn cable
path — so the interesting behaviour is that geometry and styling share a row
without clobbering each other, and that a colour is a name from a CLOSED palette
rather than free text (the map's loudest colours must stay the status tones).

Also pins the cross-link fix: a peer's presentation is keyed (child=higher,
parent=lower) so waypoints still run parent→child, and the write path used to
reject that pair outright — the map offered a route editor whose every save
400'd.
"""
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
from wisp.central.server import make_server
from wisp.central.store import CentralStore


class LinkStyleApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispA", "hand", "handpassword", "worker")
        auth.create_user(self.store, "ispB", "bowner", "bownerpassword", "owner")
        self.core = self.store.create_org_device("ispA", {
            "name": "CORE-SW", "ip_address": "10.0.0.1", "device_type": "switch",
            "region": None, "parent_device_id": None})
        self.edge = self.store.create_org_device("ispA", {
            "name": "EDGE-SW", "ip_address": "10.0.0.2", "device_type": "switch",
            "region": None, "parent_device_id": self.core})
        self.far = self.store.create_org_device("ispA", {
            "name": "FAR-SW", "ip_address": "10.0.0.3", "device_type": "switch",
            "region": None, "parent_device_id": None})
        self.server = make_server(self.cfg, self.store)
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
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        setcookie = resp.getheader("Set-Cookie")
        conn.close()
        return resp.status, (json.loads(raw) if raw else {}), setcookie

    def _login(self, username="owner", password="ownerpassword"):
        _, _, setcookie = self._req("POST", "/api/login",
                                    {"username": username, "password": password})
        return setcookie.split(";")[0] if setcookie else None

    def _routes(self, cookie):
        status, body, _ = self._req("GET", "/api/inventory/routes", cookie=cookie)
        self.assertEqual(status, 200, body)
        return {(r["child_id"], r["parent_id"]): r for r in body["routes"]}

    # --- colour --------------------------------------------------------------

    def test_colour_survives_on_a_link_with_no_drawn_route(self):
        # The whole point of colouring is telling apart near-parallel chords, and
        # a chord is exactly the link nobody has drawn a route for — styling must
        # not require geometry to exist first.
        cookie = self._login()
        status, body, _ = self._req("POST", "/api/inventory/link-style",
                                    {"child_id": self.edge, "parent_id": self.core,
                                     "color": "violet"}, cookie=cookie)
        self.assertEqual(status, 200, body)
        row = self._routes(cookie)[(self.edge, self.core)]
        self.assertEqual(row["color"], "violet")
        self.assertEqual(row["waypoints"], [])
        self.assertIsNone(row["label_pos"])

    def test_colour_is_a_closed_palette(self):
        cookie = self._login()
        for bad in ("#ff0000", "red", "destructive", "rgb(255,0,0)"):
            status, body, _ = self._req("POST", "/api/inventory/link-style",
                                        {"child_id": self.edge, "parent_id": self.core,
                                         "color": bad}, cookie=cookie)
            self.assertEqual(status, 422, f"{bad} was accepted: {body}")
        self.assertEqual(self._routes(cookie), {})

    def test_clearing_the_colour_drops_a_row_that_holds_nothing_else(self):
        cookie = self._login()
        self._req("POST", "/api/inventory/link-style",
                  {"child_id": self.edge, "parent_id": self.core, "color": "teal"},
                  cookie=cookie)
        status, body, _ = self._req("POST", "/api/inventory/link-style",
                                    {"child_id": self.edge, "parent_id": self.core,
                                     "color": None}, cookie=cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(self._routes(cookie), {})

    # --- geometry and styling share one row ----------------------------------

    def test_clearing_a_route_keeps_the_colour(self):
        # The row is geometry AND styling now; "empty" has to mean all of it.
        # Erasing a drawn path must not silently repaint the line.
        cookie = self._login()
        self._req("POST", "/api/inventory/link-style",
                  {"child_id": self.edge, "parent_id": self.core, "color": "lime"},
                  cookie=cookie)
        self._req("POST", "/api/inventory/route",
                  {"child_id": self.edge, "parent_id": self.core,
                   "waypoints": [[17.4, 78.4]]}, cookie=cookie)
        status, body, _ = self._req("POST", "/api/inventory/route",
                                    {"child_id": self.edge, "parent_id": self.core,
                                     "waypoints": []}, cookie=cookie)
        self.assertEqual(status, 200, body)
        row = self._routes(cookie)[(self.edge, self.core)]
        self.assertEqual(row["color"], "lime")
        self.assertEqual(row["waypoints"], [])

    def test_a_style_write_is_sparse(self):
        # Moving a label and picking a colour are different panels; neither may
        # clear the other by omitting it.
        cookie = self._login()
        self._req("POST", "/api/inventory/link-style",
                  {"child_id": self.edge, "parent_id": self.core, "color": "indigo"},
                  cookie=cookie)
        self._req("POST", "/api/inventory/link-style",
                  {"child_id": self.edge, "parent_id": self.core, "label_pos": 0.25},
                  cookie=cookie)
        row = self._routes(cookie)[(self.edge, self.core)]
        self.assertEqual(row["color"], "indigo")
        self.assertAlmostEqual(row["label_pos"], 0.25)

        self._req("POST", "/api/inventory/link-style",
                  {"child_id": self.edge, "parent_id": self.core, "color": "chalk"},
                  cookie=cookie)
        row = self._routes(cookie)[(self.edge, self.core)]
        self.assertEqual(row["color"], "chalk")
        self.assertAlmostEqual(row["label_pos"], 0.25)

    def test_label_pos_stays_on_the_line(self):
        cookie = self._login()
        for bad in (-0.1, 1.5, "half"):
            status, body, _ = self._req("POST", "/api/inventory/link-style",
                                        {"child_id": self.edge, "parent_id": self.core,
                                         "label_pos": bad}, cookie=cookie)
            self.assertEqual(status, 422, f"{bad} was accepted: {body}")

    # --- which links can be styled at all ------------------------------------

    def test_a_cross_link_can_be_styled_and_routed(self):
        # Peers are canonicalized (min, max) in org_device_links but keyed
        # (child=higher, parent=lower) here so waypoints run parent→child. The
        # write path used to know only about primary and backup edges, so this
        # pair 400'd even though the map offered the editor.
        cookie = self._login()
        lo, hi = min(self.core, self.far), max(self.core, self.far)
        status, body, _ = self._req("POST", "/api/inventory/peers",
                                    {"a_id": self.core, "b_id": self.far}, cookie=cookie)
        self.assertEqual(status, 200, body)
        status, body, _ = self._req("POST", "/api/inventory/link-style",
                                    {"child_id": hi, "parent_id": lo,
                                     "color": "magenta"}, cookie=cookie)
        self.assertEqual(status, 200, body)
        status, body, _ = self._req("POST", "/api/inventory/route",
                                    {"child_id": hi, "parent_id": lo,
                                     "waypoints": [[17.5, 78.5]]}, cookie=cookie)
        self.assertEqual(status, 200, body)
        row = self._routes(cookie)[(hi, lo)]
        self.assertEqual(row["color"], "magenta")
        self.assertEqual(row["waypoints"], [[17.5, 78.5]])

    def test_unlinked_devices_cannot_be_styled(self):
        cookie = self._login()
        status, body, _ = self._req("POST", "/api/inventory/link-style",
                                    {"child_id": self.far, "parent_id": self.edge,
                                     "color": "teal"}, cookie=cookie)
        self.assertEqual(status, 422, body)

    def test_styling_is_an_owner_write(self):
        # Cartography is inventory, and a worker triages rather than reconfigures.
        cookie = self._login("hand", "handpassword")
        status, body, _ = self._req("POST", "/api/inventory/link-style",
                                    {"child_id": self.edge, "parent_id": self.core,
                                     "color": "teal"}, cookie=cookie)
        self.assertEqual(status, 403, body)

    def test_another_orgs_owner_is_refused(self):
        cookie = self._login("bowner", "bownerpassword")
        status, body, _ = self._req("POST", "/api/inventory/link-style",
                                    {"child_id": self.edge, "parent_id": self.core,
                                     "color": "teal"}, cookie=cookie)
        self.assertIn(status, (403, 404), body)
        self.assertEqual(self._routes(self._login()), {})


if __name__ == "__main__":
    unittest.main()
