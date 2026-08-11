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


class PeerLinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispB", "bowner", "bownerpassword", "owner")
        self.core = self._device("ispA", "CORE", "10.0.0.1", None)
        self.agg1 = self._device("ispA", "AGG-1", "10.0.0.2", self.core)
        self.agg2 = self._device("ispA", "AGG-2", "10.0.0.3", self.core)
        self.agg3 = self._device("ispA", "AGG-3", "10.0.0.4", self.core)
        self.leaf = self._device("ispA", "LEAF", "10.0.0.5", self.agg1)
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

    def _peers_of(self, device_id, org="ispA"):
        return next(d["peer_ids"] for d in self.store.list_org_devices(org)
                    if d["id"] == device_id)


    def test_a_peer_link_never_rebuilds_the_engine(self):
        registry = EngineRegistry(self.store, self.cfg)
        before = registry.get("ispA")
        self.store.create_peer_link("ispA", self.agg1, self.agg2)
        self.assertIs(registry.get("ispA"), before)
        self.store.create_backup_link("ispA", self.leaf, self.agg2)
        self.assertIsNot(registry.get("ispA"), before)

    def test_peers_are_invisible_to_the_dependency_graph(self):
        self.store.create_peer_link("ispA", self.agg1, self.agg2)
        for meta in load_device_meta(self.store, "ispA"):
            self.assertEqual(meta.parents, (), f"{meta.name} gained a peer as a parent")
            if meta.id == self.agg1:
                self.assertEqual([e.parent_id for e in meta.effective_parents()],
                                 [self.core])
        self.assertEqual(self.store.org_device_backup_map("ispA"), {})
        self.assertEqual(self.store.org_device_backup_edges("ispA"), [])

    def test_a_ring_of_cross_links_is_allowed(self):
        cookie = self._login()
        for a, b in ((self.agg1, self.agg2), (self.agg2, self.agg3),
                     (self.agg3, self.agg1)):
            status, body, _ = self._req("POST", "/api/inventory/peers",
                                        {"a_id": a, "b_id": b}, cookie=cookie)
            self.assertEqual(status, 200, body)
        self.assertEqual(self._peers_of(self.agg1), sorted([self.agg2, self.agg3]))


    def test_declaring_from_either_end_is_the_same_link(self):
        cookie = self._login()
        self._req("POST", "/api/inventory/peers",
                  {"a_id": self.agg2, "b_id": self.agg1}, cookie=cookie)
        status, body, _ = self._req("POST", "/api/inventory/peers",
                                    {"a_id": self.agg1, "b_id": self.agg2}, cookie=cookie)
        self.assertEqual(status, 422, body)
        rows = self.store.org_device_peer_map("ispA")
        self.assertEqual(rows[self.agg1], {self.agg2})
        self.assertEqual(rows[self.agg2], {self.agg1})

    def test_both_ends_list_the_link(self):
        self.store.create_peer_link("ispA", self.agg2, self.agg1)
        self.assertEqual(self._peers_of(self.agg1), [self.agg2])
        self.assertEqual(self._peers_of(self.agg2), [self.agg1])

    def test_delete_works_from_either_end(self):
        cookie = self._login()
        self.store.create_peer_link("ispA", self.agg1, self.agg2)
        status, body, _ = self._req("POST", "/api/inventory/peers/delete",
                                    {"a_id": self.agg2, "b_id": self.agg1}, cookie=cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(self._peers_of(self.agg1), [])


    def test_refuses_self_and_existing_edges(self):
        cookie = self._login()
        for a, b in ((self.agg1, self.agg1),
                     (self.leaf, self.agg1),
                     (self.agg1, self.leaf)):
            status, body, _ = self._req("POST", "/api/inventory/peers",
                                        {"a_id": a, "b_id": b}, cookie=cookie)
            self.assertEqual(status, 422, body)
        self.store.create_backup_link("ispA", self.leaf, self.agg2)
        status, body, _ = self._req("POST", "/api/inventory/peers",
                                    {"a_id": self.leaf, "b_id": self.agg2}, cookie=cookie)
        self.assertEqual(status, 422, body)

    def test_cross_org_is_refused(self):
        cookie = self._login()
        status, body, _ = self._req("POST", "/api/inventory/peers",
                                    {"a_id": self.agg1, "b_id": self.other}, cookie=cookie)
        self.assertEqual(status, 422, body)

    def test_another_orgs_owner_cannot_declare_one(self):
        cookie = self._login("bowner", "bownerpassword")
        status, body, _ = self._req("POST", "/api/inventory/peers",
                                    {"a_id": self.agg1, "b_id": self.agg2}, cookie=cookie)
        self.assertEqual(status, 403, body)


    def test_deleting_a_device_purges_its_cross_links(self):
        self.store.create_peer_link("ispA", self.agg2, self.agg3)
        self.store.delete_org_device("ispA", self.agg3)
        self.assertEqual(self._peers_of(self.agg2), [])
        self.assertEqual(self.store.org_device_peer_map("ispA").get(self.agg3), None)

    def test_a_cross_link_does_not_block_deleting_a_device(self):
        self.store.create_peer_link("ispA", self.agg2, self.agg3)
        self.assertTrue(self.store.delete_org_device("ispA", self.agg3)["ok"])


if __name__ == "__main__":
    unittest.main()
