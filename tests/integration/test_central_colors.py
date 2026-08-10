"""Operator colour-coding for tags and probes (org_colors).

Presentation only — a colour never reaches the engine, an alert or the edge.
The two properties worth pinning are that the palette stays CLOSED (a free hex
would let an operator paint a healthy device the same red as a broken one, on
the screens that exist to show alarms) and that colours are org-scoped, since
they key on free text rather than a foreign key.
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
from wisp.central import auth, inventory
from wisp.central.server import make_server
from wisp.central.store import CentralStore


class ColorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispB", "bowner", "bownerpassword", "owner")
        self.dev = self.store.create_org_device("ispA", {
            "name": "CORE", "ip_address": "10.0.0.1", "device_type": "switch",
            # stored comma-joined (clean_device_payload's shape); the wire
            # carries a real list back out
            "region": None, "parent_device_id": None, "tags": "core,bsnl"})
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

    # --- the closed palette --------------------------------------------------

    def test_free_hex_is_refused(self):
        # THE invariant: no operator colour may impersonate a status tone
        cookie = self._login()
        for bad in ("#ff0000", "red", "rgb(255,0,0)", "destructive", "var(--destructive)"):
            status, body, _ = self._req("POST", "/api/inventory/tag-color",
                                        {"org_id": "ispA", "tag": "core", "color": bad},
                                        cookie=cookie)
            self.assertEqual(status, 422, f"{bad!r} was accepted: {body}")
        self.assertEqual(self.store.org_colors("ispA", "tag"), {})

    def test_palette_carries_no_status_hue(self):
        # a name here can never mean "down"/"degraded"/"ok" to a reader
        for name in inventory.PALETTE:
            self.assertNotIn(name, ("red", "amber", "orange", "green", "yellow"))

    def test_the_palette_is_not_reachable_as_a_link_colour(self):
        # The map's per-link tint was REMOVED (2026-08-08) once `org_cables` gave
        # "these spans are one physical cable" a real place to live — six names in
        # one org-wide namespace could never say it (`magenta` named two different
        # cables at two different sites). The palette itself survives for tags and
        # probes. Re-exporting it under the old name is how it creeps back into
        # the link write path, so the alias must stay gone.
        self.assertFalse(hasattr(inventory, "LINK_COLORS"))
        with self.assertRaises(inventory.InventoryError):
            inventory.clean_link_style_payload(
                {"child_id": 1, "parent_id": 2, "color": "teal"})

    # --- round trip ----------------------------------------------------------

    def test_tag_colour_round_trips_on_the_inventory_reply(self):
        cookie = self._login()
        status, body, _ = self._req("POST", "/api/inventory/tag-color",
                                    {"org_id": "ispA", "tag": "core", "color": "teal"},
                                    cookie=cookie)
        self.assertEqual(status, 200, body)
        status, body, _ = self._req("GET", "/api/inventory?org=ispA", cookie=cookie)
        self.assertEqual(body["tag_colors"], {"core": "teal"})
        # and it is presentation only — the device row itself is untouched
        self.assertEqual(body["devices"][0]["tags"], ["core", "bsnl"])

    def test_probe_colour_round_trips_on_the_nodes_reply(self):
        cookie = self._login()
        self.store.issue_node_token("ispA", "edge1", created_by=1)
        status, body, _ = self._req("POST", "/api/nodes/color",
                                    {"org_id": "ispA", "node_id": "edge1", "color": "violet"},
                                    cookie=cookie)
        self.assertEqual(status, 200, body)
        status, body, _ = self._req("GET", "/api/nodes?org=ispA", cookie=cookie)
        self.assertEqual(body["node_colors"], {"edge1": "violet"})

    def test_null_clears_the_colour(self):
        cookie = self._login()
        self.store.set_org_color("ispA", "tag", "core", "lime")
        status, body, _ = self._req("POST", "/api/inventory/tag-color",
                                    {"org_id": "ispA", "tag": "core", "color": None},
                                    cookie=cookie)
        self.assertEqual(status, 200, body)
        # cleared means NO ROW, not a stored sentinel — an uncoloured tag costs
        # nothing and re-reads as the default
        self.assertEqual(self.store.org_colors("ispA", "tag"), {})

    def test_recolouring_replaces_rather_than_duplicates(self):
        self.store.set_org_color("ispA", "tag", "core", "lime")
        self.store.set_org_color("ispA", "tag", "core", "indigo")
        self.assertEqual(self.store.org_colors("ispA", "tag"), {"core": "indigo"})

    # --- scoping -------------------------------------------------------------

    def test_colours_are_org_scoped(self):
        # tag text is not unique across orgs; "core" in ispB is a different tag
        self.store.set_org_color("ispA", "tag", "core", "teal")
        self.store.set_org_color("ispB", "tag", "core", "magenta")
        self.assertEqual(self.store.org_colors("ispA", "tag"), {"core": "teal"})
        self.assertEqual(self.store.org_colors("ispB", "tag"), {"core": "magenta"})

    def test_kinds_do_not_collide(self):
        # a probe and a tag may legitimately share a name
        self.store.set_org_color("ispA", "tag", "edge1", "teal")
        self.store.set_org_color("ispA", "node", "edge1", "lime")
        self.assertEqual(self.store.org_colors("ispA", "tag"), {"edge1": "teal"})
        self.assertEqual(self.store.org_colors("ispA", "node"), {"edge1": "lime"})

    def test_another_orgs_colours_are_refused(self):
        cookie = self._login()
        status, _, _ = self._req("POST", "/api/inventory/tag-color",
                                 {"org_id": "ispB", "tag": "core", "color": "teal"},
                                 cookie=cookie)
        self.assertIn(status, (403, 404))
        self.assertEqual(self.store.org_colors("ispB", "tag"), {})

    def test_signed_out_is_refused(self):
        status, _, _ = self._req("POST", "/api/inventory/tag-color",
                                 {"org_id": "ispA", "tag": "core", "color": "teal"})
        self.assertEqual(status, 401)
        self.assertEqual(self.store.org_colors("ispA", "tag"), {})

    def test_deleting_the_org_sweeps_its_colours(self):
        # org ids are reusable — colours must not surface inside a later org of
        # the same name (store_orgs.delete_org finds the table by introspection)
        self.store.set_org_color("ispA", "tag", "core", "teal")
        self.store.delete_org("ispA")
        self.assertEqual(self.store.org_colors("ispA", "tag"), {})


if __name__ == "__main__":
    unittest.main()
