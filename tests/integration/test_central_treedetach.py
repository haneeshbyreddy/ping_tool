"""`tree_detached`: lift a device to the top level of the Network tree.

An aggregation switch can carry a subtree big enough that a device the operator
reads every day is buried inside it. This flag moves that ROW — and its own
subtree — to the top level of the tree view, and does nothing else: the parent
link stays exactly where it was, so suppression, the map, `/edge/devices` and
paging are all untouched. Same discipline as tags. This module pins that
separation; a regression here would mean a presentation click re-paged a fleet.
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
from wisp.central.engine import EngineRegistry, load_device_meta
from wisp.central.server import make_server
from wisp.central.store import CentralStore


class TreeDetachTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispB", "bowner", "bownerpassword", "owner")
        self.core = self._device("ispA", "CORE", "10.0.0.1", None)
        self.chsw = self._device("ispA", "CH-SW", "10.0.0.2", self.core)
        self.tvsw = self._device("ispA", "TV-SW", "10.0.0.3", self.chsw)
        self.leaf = self._device("ispA", "LEAF", "10.0.0.4", self.tvsw)
        self.other = self._device("ispB", "B-SW", "10.9.9.9", None)
        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    def _device(self, org, name, ip, parent):
        return self.store.create_org_device(org, {
            "name": name, "ip_address": ip, "device_type": "switch",
            "region": None, "parent_device_id": parent})

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

    def _row(self, device_id, org="ispA"):
        return next(d for d in self.store.list_org_devices(org) if d["id"] == device_id)

    # --- the safety property -------------------------------------------------

    def test_detaching_never_rebuilds_the_engine(self):
        # THE invariant. A rebuild rehydrates every FSM and can re-page a fleet,
        # so a view preference must be inert to the engine. Same object identity
        # before and after proves the topology fingerprint never moved.
        registry = EngineRegistry(self.store, self.cfg)
        before = registry.get("ispA")
        self.store.set_org_device_tree_detached("ispA", self.tvsw, True)
        self.assertIs(registry.get("ispA"), before)
        self.store.set_org_device_tree_detached("ispA", self.tvsw, False)
        self.assertIs(registry.get("ispA"), before)

    def test_the_parent_link_survives_everywhere_that_matters(self):
        self.store.set_org_device_tree_detached("ispA", self.tvsw, True)
        # the dependency graph still folds TV-SW under CH-SW: an outage on the
        # parent must still suppress it, and the row must still ship to the edge
        meta = {m.id: m for m in load_device_meta(self.store, "ispA")}
        self.assertEqual(meta[self.tvsw].parent_device_id, self.chsw)
        self.assertEqual(meta[self.leaf].parent_device_id, self.tvsw)
        topo = {d["id"]: d for d in self.store.org_device_topology("ispA")}
        self.assertEqual(topo[self.tvsw]["parent_device_id"], self.chsw)
        # and the dashboard row still carries it — the tree renders the marker
        # ("under CH-SW") off exactly this, so a lifted row never hides the truth
        self.assertEqual(self._row(self.tvsw)["parent_device_id"], self.chsw)

    # --- the flag itself -----------------------------------------------------

    def test_flag_round_trips_through_the_api(self):
        cookie = self._login()
        self.assertEqual(self._row(self.tvsw)["tree_detached"], 0)
        status, body, _ = self._req("POST", "/api/inventory/tree-detached",
                                    {"id": self.tvsw, "on": True}, cookie=cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(self._row(self.tvsw)["tree_detached"], 1)
        status, body, _ = self._req("POST", "/api/inventory/tree-detached",
                                    {"id": self.tvsw, "on": False}, cookie=cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(self._row(self.tvsw)["tree_detached"], 0)

    def test_an_ordinary_edit_leaves_the_flag_alone(self):
        # renaming/re-IPing a device must not silently drop the operator's
        # placement — update_org_device doesn't list the column, keep it so
        self.store.set_org_device_tree_detached("ispA", self.tvsw, True)
        self.store.update_org_device("ispA", self.tvsw, {
            "name": "TV-SW-2", "ip_address": "10.0.0.33", "device_type": "switch",
            "region": None, "parent_device_id": self.chsw})
        self.assertEqual(self._row(self.tvsw)["tree_detached"], 1)

    def test_clearing_the_parent_clears_the_flag(self):
        # the row menu hides the toggle once there's no parent, so a flag left
        # set here would be unreachable — and would surprise whoever re-parents
        self.store.set_org_device_tree_detached("ispA", self.tvsw, True)
        self.store.update_org_device("ispA", self.tvsw, {
            "name": "TV-SW", "ip_address": "10.0.0.3", "device_type": "switch",
            "region": None, "parent_device_id": None})
        self.assertEqual(self._row(self.tvsw)["tree_detached"], 0)

    # --- scoping -------------------------------------------------------------

    def test_another_orgs_device_is_refused(self):
        cookie = self._login()
        status, _, _ = self._req("POST", "/api/inventory/tree-detached",
                                 {"id": self.other, "on": True}, cookie=cookie)
        self.assertIn(status, (403, 404))
        self.assertEqual(self._row(self.other, org="ispB")["tree_detached"], 0)

    def test_signed_out_is_refused(self):
        status, _, _ = self._req("POST", "/api/inventory/tree-detached",
                                 {"id": self.tvsw, "on": True})
        self.assertEqual(status, 401)
        self.assertEqual(self._row(self.tvsw)["tree_detached"], 0)


if __name__ == "__main__":
    unittest.main()
