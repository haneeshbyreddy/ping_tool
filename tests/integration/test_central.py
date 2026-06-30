"""Central ingest tests (Phase 10 Part A): the store's idempotent persist + heartbeat
upsert + fleet view, and the HTTP server's auth/version/validation, driven over a real
socket with http.client (mirrors test_auth). No edge daemon involved — pure central side."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.central.store import CentralStore
from wisp.central.server import make_server


def _batch(records):
    return {"v": 1, "tenant_id": "ispA", "node_id": "edge-1",
            "kind": "batch", "records": records}


def _evt(edge_id, **body):
    body.setdefault("type", "OutageOpened")
    return {"id": edge_id, "kind": "event", "body": body}


class CentralStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_ingest_persists_and_is_idempotent(self):
        recs = [_evt(1, device_id=5, device_name="Tower", state="DOWN"),
                _evt(2, device_id=6, state="DOWN")]
        accepted = self.store.ingest("ispA", "edge-1", recs)
        self.assertEqual(sorted(accepted), [1, 2])
        # Re-deliver the same batch (a lost ack): central acks them again, stores no dupes.
        again = self.store.ingest("ispA", "edge-1", recs)
        self.assertEqual(sorted(again), [1, 2])
        self.assertEqual(self.store.counts()["events"], 2)

    def test_same_edge_id_different_node_coexist(self):
        # edge-local ids are per-node; the same id from two nodes are distinct records.
        self.store.ingest("ispA", "edge-1", [_evt(1, device_id=5)])
        self.store.ingest("ispA", "edge-2", [_evt(1, device_id=5)])
        self.assertEqual(self.store.counts()["events"], 2)

    def test_heartbeat_upserts_node(self):
        self.store.record_heartbeat("ispA", "edge-1",
                                    {"version": "0.10.0", "fleet_size": 7, "open_outages": 1,
                                     "last_poll_ts": "2026-06-30T12:00:00+00:00"})
        self.store.record_heartbeat("ispA", "edge-1",
                                    {"version": "0.10.1", "fleet_size": 9, "open_outages": 0})
        fleet = self.store.fleet()
        self.assertEqual(len(fleet["nodes"]), 1)
        node = fleet["nodes"][0]
        self.assertEqual(node["version"], "0.10.1")    # latest beat wins
        self.assertEqual(node["fleet_size"], 9)

    def test_rollup_records_stored(self):
        recs = [{"id": 10, "kind": "rollup",
                 "body": {"device_id": 5, "bucket": "2026-06-30T11:00:00+00:00"}}]
        self.store.ingest("ispA", "edge-1", recs)
        self.assertEqual(self.store.counts()["rollups"], 1)

    def test_fleet_recent_events_newest_first(self):
        self.store.ingest("ispA", "edge-1",
                          [_evt(1, device_name="A"), _evt(2, device_name="B")])
        names = [e["device_name"] for e in self.store.fleet()["recent_events"]]
        self.assertEqual(names, ["B", "A"])

    # --- Part B: orgs, the global id mapping, tenant scoping ---
    def test_org_auto_provisioned_on_first_contact(self):
        self.store.ingest("ispA", "edge-1", [_evt(1, device_id=5)])
        self.store.record_heartbeat("ispB", "edge-9", {"fleet_size": 1})
        tenants = {o["tenant_id"] for o in self.store.orgs()}
        self.assertEqual(tenants, {"ispA", "ispB"})

    def test_device_registry_assigns_one_global_id_per_edge_device(self):
        # same (tenant,node,edge_local_id) across two events -> ONE global id, metadata kept.
        self.store.ingest("ispA", "edge-1",
                          [_evt(1, device_id=5, device_name="Tower", device_ip="10.0.0.5")])
        self.store.ingest("ispA", "edge-1", [_evt(2, device_id=5, state="UP")])
        devs = self.store.devices("ispA")
        self.assertEqual(len(devs), 1)
        d = devs[0]
        self.assertEqual(d["edge_local_id"], 5)
        self.assertEqual(d["name"], "Tower")         # denormalized name retained
        self.assertEqual(d["ip"], "10.0.0.5")
        self.assertTrue(isinstance(d["id"], int))    # a central GLOBAL id

    def test_same_edge_local_id_two_nodes_two_global_ids(self):
        self.store.ingest("ispA", "edge-1", [_evt(1, device_id=5, device_name="A")])
        self.store.ingest("ispA", "edge-2", [_evt(1, device_id=5, device_name="B")])
        gids = {d["id"] for d in self.store.devices("ispA")}
        self.assertEqual(len(gids), 2)               # per-node ids never collide globally

    def test_device_latest_state(self):
        self.store.ingest("ispA", "edge-1",
                          [_evt(1, device_id=5, state="DOWN", type="OutageOpened")])
        self.store.ingest("ispA", "edge-1",
                          [_evt(2, device_id=5, state="UP", type="OutageResolved")])
        d = self.store.devices("ispA")[0]
        self.assertEqual(d["last_state"], "UP")
        self.assertEqual(d["last_event"], "OutageResolved")

    def test_tenant_scoping_filters_reads(self):
        self.store.ingest("ispA", "edge-1", [_evt(1, device_id=5, device_name="A")])
        self.store.ingest("ispB", "edge-1", [_evt(1, device_id=7, device_name="B")])
        self.assertEqual(len(self.store.devices("ispA")), 1)
        self.assertEqual(len(self.store.devices()), 2)               # unscoped = all tenants
        self.assertEqual([n["tenant_id"] for n in self.store.fleet("ispB")["nodes"]], ["ispB"])

    def test_uplink_event_has_no_device_row(self):
        self.store.ingest("ispA", "edge-1", [_evt(1, type="UplinkDown")])  # no device_id
        self.assertEqual(self.store.devices("ispA"), [])

    def test_set_org_topic(self):
        self.store.ingest("ispA", "edge-1", [_evt(1, device_id=5)])
        self.store.set_org("ispA", name="ISP A", ntfy_topic="ispA-ops")
        self.assertEqual(self.store.org_topic("ispA"), "ispA-ops")
        org = next(o for o in self.store.orgs() if o["tenant_id"] == "ispA")
        self.assertEqual(org["name"], "ISP A")
        self.assertEqual(org["node_count"], 1)


class CentralServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0, central_token="s3cret")
        self.store = CentralStore(self.cfg.central_db)
        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    def _req(self, method, path, body=None, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, (json.loads(raw) if raw else {})

    def test_healthz_unauthed(self):
        status, body = self._req("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_ingest_requires_token(self):
        status, _ = self._req("POST", "/ingest", _batch([_evt(1)]))
        self.assertEqual(status, 401)
        status, _ = self._req("POST", "/ingest", _batch([_evt(1)]), token="wrong")
        self.assertEqual(status, 401)

    def test_ingest_with_token_persists(self):
        status, body = self._req("POST", "/ingest",
                                 _batch([_evt(1, device_id=5), _evt(2, device_id=6)]),
                                 token="s3cret")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(body["accepted"]), [1, 2])
        self.assertEqual(self.store.counts()["events"], 2)

    def test_heartbeat_persists(self):
        env = {"v": 1, "tenant_id": "ispA", "node_id": "edge-1", "kind": "heartbeat",
               "body": {"version": "0.10.0", "fleet_size": 3}}
        status, body = self._req("POST", "/heartbeat", env, token="s3cret")
        self.assertEqual(status, 200)
        self.assertEqual(self.store.fleet()["nodes"][0]["fleet_size"], 3)

    def test_unsupported_version_rejected(self):
        env = _batch([_evt(1)])
        env["v"] = 999
        status, body = self._req("POST", "/ingest", env, token="s3cret")
        self.assertEqual(status, 400)

    def test_missing_tenant_rejected(self):
        env = {"v": 1, "node_id": "edge-1", "kind": "batch", "records": []}
        status, _ = self._req("POST", "/ingest", env, token="s3cret")
        self.assertEqual(status, 400)

    def test_bad_json_rejected(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/ingest", body="{not json",
                     headers={"Authorization": "Bearer s3cret",
                              "Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        conn.close()

    def test_fleet_view_authed(self):
        self._req("POST", "/ingest", _batch([_evt(1, device_name="Tower")]), token="s3cret")
        status, body = self._req("GET", "/api/fleet", token="s3cret")
        self.assertEqual(status, 200)
        self.assertEqual(body["recent_events"][0]["device_name"], "Tower")
        # unauthed fleet view is refused
        status, _ = self._req("GET", "/api/fleet")
        self.assertEqual(status, 401)

    def test_orgs_and_devices_endpoints(self):
        self._req("POST", "/ingest",
                  _batch([_evt(1, device_id=5, device_name="Tower")]), token="s3cret")
        status, body = self._req("GET", "/api/orgs", token="s3cret")
        self.assertEqual(status, 200)
        self.assertEqual(body["orgs"][0]["tenant_id"], "ispA")
        status, body = self._req("GET", "/api/devices", token="s3cret")
        self.assertEqual(status, 200)
        self.assertEqual(body["devices"][0]["name"], "Tower")
        self.assertIn("id", body["devices"][0])           # global id surfaced
        # both require the token
        self.assertEqual(self._req("GET", "/api/devices")[0], 401)

    def test_fleet_tenant_query_param_scopes(self):
        self._req("POST", "/ingest", _batch([_evt(1, device_name="A")]), token="s3cret")
        env = {"v": 1, "tenant_id": "ispB", "node_id": "edge-1", "kind": "batch",
               "records": [_evt(1, device_name="B")]}
        self._req("POST", "/ingest", env, token="s3cret")
        status, body = self._req("GET", "/api/fleet?tenant=ispB", token="s3cret")
        self.assertEqual(status, 200)
        self.assertEqual({n["tenant_id"] for n in body["nodes"]}, {"ispB"})


if __name__ == "__main__":
    unittest.main()
