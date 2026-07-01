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

    def _device(self):
        return self.store.create_org_device("ispA", {
            "name": "Tower", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None})

    def test_open_outage_if_absent_does_not_stack(self):
        """A second open (e.g. a stray duplicate poller, or a redundant call from the
        engine) must not create a second open row while one is already open for that
        device — the same invariant `central/engine.py`'s `apply_events` relies on."""
        dev = self._device()
        self.store.open_outage_if_absent("ispA", dev, "2026-06-23T08:34:54+00:00", "DOWN")
        self.store.open_outage_if_absent("ispA", dev, "2026-06-23T08:34:57+00:00", "DOWN")
        self.assertIsNotNone(self.store.open_outage_id("ispA", dev))
        with self.store._connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM outages WHERE tenant_id='ispA' AND device_id=?"
                " AND resolved_at IS NULL", (dev,)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_open_outage_reopens_after_resolve(self):
        dev = self._device()
        self.store.open_outage_if_absent("ispA", dev, "2026-06-23T08:00:00+00:00", "DOWN")
        self.store.resolve_outage("ispA", dev, "2026-06-23T08:05:00+00:00")
        self.store.open_outage_if_absent("ispA", dev, "2026-06-23T09:00:00+00:00", "DOWN")
        with self.store._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM outages WHERE tenant_id='ispA' AND device_id=?",
                (dev,)).fetchone()[0]
        self.assertEqual(total, 2)
        self.assertIsNotNone(self.store.open_outage_id("ispA", dev))

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

    def test_set_org_role_topics(self):
        self.store.set_org("ispA", ntfy_topic_owner="isp-a-owner",
                           ntfy_topic_operator="isp-a-op", ntfy_topic_tech="isp-a-tech")
        self.assertEqual(self.store.org_role_topic("ispA", "owner"), "isp-a-owner")
        self.assertEqual(self.store.org_role_topic("ispA", "operator"), "isp-a-op")
        self.assertEqual(self.store.org_role_topic("ispA", "tech"), "isp-a-tech")
        self.assertIsNone(self.store.org_role_topic("ispA", "bogus"))
        # COALESCE semantics: setting one topic doesn't clobber the others.
        self.store.set_org("ispA", ntfy_topic_owner="isp-a-owner-2")
        self.assertEqual(self.store.org_role_topic("ispA", "owner"), "isp-a-owner-2")
        self.assertEqual(self.store.org_role_topic("ispA", "operator"), "isp-a-op")


class NodeTokenTest(unittest.TestCase):
    """Self-service node enrollment: an ISP owner/operator issues one of these per node
    from the dashboard instead of a platform superadmin running a CLI command
    (central/pki.py's enroll-edge/mTLS is still there for whoever wants it)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_never_registered_has_no_status(self):
        self.assertIsNone(self.store.get_node_token_status("ispA", "edge-1"))
        self.assertFalse(self.store.node_token_registered("ispA", "edge-1"))

    def test_issue_then_resolve(self):
        token = self.store.issue_node_token("ispA", "edge-1")
        self.assertEqual(self.store.resolve_node_token(token), ("ispA", "edge-1"))
        self.assertTrue(self.store.node_token_registered("ispA", "edge-1"))
        status = self.store.get_node_token_status("ispA", "edge-1")
        self.assertIsNotNone(status["created_at"])
        self.assertIsNone(status["revoked_at"])

    def test_wrong_token_does_not_resolve(self):
        self.store.issue_node_token("ispA", "edge-1")
        self.assertIsNone(self.store.resolve_node_token("not-the-token"))
        self.assertIsNone(self.store.resolve_node_token(""))

    def test_reissue_invalidates_the_old_token(self):
        old = self.store.issue_node_token("ispA", "edge-1")
        new = self.store.issue_node_token("ispA", "edge-1")
        self.assertNotEqual(old, new)
        self.assertIsNone(self.store.resolve_node_token(old))
        self.assertEqual(self.store.resolve_node_token(new), ("ispA", "edge-1"))

    def test_revoke_stops_resolving_but_keeps_the_row(self):
        token = self.store.issue_node_token("ispA", "edge-1")
        self.assertTrue(self.store.revoke_node_token("ispA", "edge-1"))
        self.assertIsNone(self.store.resolve_node_token(token))
        self.assertFalse(self.store.node_token_registered("ispA", "edge-1"))
        status = self.store.get_node_token_status("ispA", "edge-1")
        self.assertIsNotNone(status)          # history kept, just deactivated
        self.assertIsNotNone(status["revoked_at"])

    def test_revoke_twice_is_a_no_op(self):
        self.store.issue_node_token("ispA", "edge-1")
        self.assertTrue(self.store.revoke_node_token("ispA", "edge-1"))
        self.assertFalse(self.store.revoke_node_token("ispA", "edge-1"))

    def test_revoke_of_never_registered_node_is_false(self):
        self.assertFalse(self.store.revoke_node_token("ispA", "ghost"))

    def test_reissue_after_revoke_reactivates(self):
        token1 = self.store.issue_node_token("ispA", "edge-1")
        self.store.revoke_node_token("ispA", "edge-1")
        token2 = self.store.issue_node_token("ispA", "edge-1")
        self.assertIsNone(self.store.resolve_node_token(token1))
        self.assertEqual(self.store.resolve_node_token(token2), ("ispA", "edge-1"))
        self.assertIsNone(self.store.get_node_token_status("ispA", "edge-1")["revoked_at"])

    def test_tenant_isolation(self):
        a = self.store.issue_node_token("ispA", "edge-1")
        self.store.issue_node_token("ispB", "edge-1")   # same node_id, different tenant
        self.assertEqual(self.store.resolve_node_token(a), ("ispA", "edge-1"))
        self.assertEqual(len(self.store.list_node_tokens("ispA")), 1)
        self.assertEqual(len(self.store.list_node_tokens("ispB")), 1)

    def test_list_joins_heartbeat_info(self):
        self.store.issue_node_token("ispA", "edge-1")
        self.store.issue_node_token("ispA", "edge-2")
        self.store.record_heartbeat("ispA", "edge-1", {"version": "0.11.0"})
        rows = {r["node_id"]: r for r in self.store.list_node_tokens("ispA")}
        self.assertEqual(rows["edge-1"]["version"], "0.11.0")
        self.assertIsNotNone(rows["edge-1"]["last_seen"])
        self.assertIsNone(rows["edge-2"]["version"])   # registered, never connected


class OrgDevicesTest(unittest.TestCase):
    """Phase A: the ISP-managed device topology — distinct from the edge-ingest `devices`
    registry above (this table has no edge behind it yet; an ISP builds it by hand)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_list_update_delete_round_trip(self):
        root = self.store.create_org_device("ispA", {
            "name": "Core Router", "ip_address": "10.0.0.1", "device_type": "core",
            "region": "north", "parent_device_id": None})
        child = self.store.create_org_device("ispA", {
            "name": "Tower 1", "ip_address": "10.0.0.2", "device_type": "backhaul",
            "region": "north", "parent_device_id": root})
        devs = self.store.list_org_devices("ispA")
        self.assertEqual(len(devs), 2)
        root_row = next(d for d in devs if d["id"] == root)
        self.assertEqual(root_row["child_count"], 1)

        ok = self.store.update_org_device("ispA", child, {
            "name": "Tower 1 (renamed)", "ip_address": "10.0.0.2", "device_type": "backhaul",
            "region": "north", "parent_device_id": root})
        self.assertTrue(ok)
        self.assertEqual(self.store.get_org_device("ispA", child)["name"], "Tower 1 (renamed)")

        # blocked while it still has a child
        result = self.store.delete_org_device("ispA", root)
        self.assertFalse(result["ok"])
        self.assertIn("child", result["reason"])
        self.store.delete_org_device("ispA", child)
        result = self.store.delete_org_device("ispA", root)
        self.assertTrue(result["ok"])
        self.assertEqual(self.store.list_org_devices("ispA"), [])

    def test_tenant_isolation(self):
        a = self.store.create_org_device("ispA", {
            "name": "A", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None})
        self.store.create_org_device("ispB", {
            "name": "B", "ip_address": "10.0.1.1", "device_type": None,
            "region": None, "parent_device_id": None})
        self.assertEqual(len(self.store.list_org_devices("ispA")), 1)
        self.assertEqual(len(self.store.list_org_devices("ispB")), 1)
        # ispB can't reach into ispA's row by id
        self.assertIsNone(self.store.get_org_device("ispB", a))
        self.assertFalse(self.store.update_org_device("ispB", a, {
            "name": "hijacked", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None}))
        self.assertEqual(self.store.device_tenant(a), "ispA")

    def test_parent_map_is_tenant_scoped(self):
        a = self.store.create_org_device("ispA", {
            "name": "A", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None})
        self.store.create_org_device("ispB", {
            "name": "B", "ip_address": "10.0.1.1", "device_type": None,
            "region": None, "parent_device_id": None})
        pmap = self.store.org_device_parent_map("ispA")
        self.assertEqual(set(pmap.keys()), {a})

    def test_maintenance_and_snmp_toggle(self):
        d = self.store.create_org_device("ispA", {
            "name": "Sw1", "ip_address": "10.0.0.9", "device_type": "switch",
            "region": None, "parent_device_id": None})
        self.assertTrue(self.store.set_org_device_maintenance("ispA", d, True))
        self.assertTrue(self.store.get_org_device("ispA", d)["maintenance"])
        self.assertTrue(self.store.set_org_device_snmp("ispA", d, {
            "snmp_enabled": 1, "snmp_version": "2c", "snmp_community": "public",
            "snmp_port": 161}))
        row = self.store.get_org_device("ispA", d)
        self.assertTrue(row["snmp_enabled"])
        self.assertEqual(row["snmp_community"], "public")


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
