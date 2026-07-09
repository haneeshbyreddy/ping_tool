import http.client
import json
import os
import sys
import tempfile
import threading
import time
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

class CentralAuthUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "c.db")
        self.store = CentralStore(self.cfg.central_db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_user_validations(self):
        with self.assertRaises(auth.AuthError):
            auth.create_user(self.store, "ispA", "a", "short", "owner")
        with self.assertRaises(auth.AuthError):
            auth.create_user(self.store, "ispA", "a", "longenough", "boss")
        auth.create_user(self.store, "ispA", "alice", "longenough", "owner")
        with self.assertRaises(auth.AuthError):
            auth.create_user(self.store, "ispA", "alice", "longenough", "owner")

    def test_verify_login(self):
        auth.create_user(self.store, "ispA", "alice", "correcthorse", "owner")
        self.assertIsNone(auth.verify_login(self.store, "alice", "wrong"))
        self.assertIsNotNone(auth.verify_login(self.store, "alice", "correcthorse"))
        uid = self.store.get_user_by_username("alice")["id"]
        self.store.set_user_active(uid, False)
        self.assertIsNone(auth.verify_login(self.store, "alice", "correcthorse"))

    def test_session_round_trip_and_tamper(self):
        uid = auth.create_user(self.store, None, "root", "supersecret")
        tok = auth.issue_session(uid, self.cfg)
        self.assertEqual(auth.verify_session(tok, cfg=self.cfg, timeout_h=12), uid)
        self.assertIsNone(auth.verify_session(tok + "x", cfg=self.cfg, timeout_h=12))
        old = auth.issue_session(uid, self.cfg, now=time.time() - 13 * 3600)
        self.assertIsNone(auth.verify_session(old, cfg=self.cfg, timeout_h=12, now=time.time()))
        user = auth.resolve_session(self.store, tok, cfg=self.cfg)
        self.assertTrue(user["is_superadmin"])
        self.assertNotIn("pw_hash", user)

class CentralAuthHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "c.db",
                          central_bind="127.0.0.1", central_port=0, central_token="tok")
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, None, "root", "rootpassword")
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispA", "oper", "operpassword", "operator")
        self.store.touch_node("ispA", "edge-1")
        self.store.touch_node("ispB", "edge-1")
        self.notifier = RecordingNotifier()
        self.server = make_server(self.cfg, self.store, notifier=self.notifier)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    def _req(self, method, path, body=None, cookie=None, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body); headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        if token:
            headers["Authorization"] = f"Bearer {token}"
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        setcookie = resp.getheader("Set-Cookie")
        conn.close()
        return resp.status, (json.loads(raw) if raw else {}), setcookie

    def _login(self, username, password):
        status, body, setcookie = self._req("POST", "/api/login",
                                            {"username": username, "password": password})
        if status != 200:
            return status, None
        return status, setcookie.split(";")[0]

    def test_login_sets_cookie_and_me(self):
        status, cookie = self._login("owner", "ownerpassword")
        self.assertEqual(status, 200)
        self.assertTrue(cookie.startswith("wisp_central_session="))
        status, body, _ = self._req("GET", "/api/me", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["org_id"], "ispA")
        self.assertEqual(body["user"]["role"], "owner")

    def test_bad_login_401_and_me_requires_session(self):
        self.assertEqual(self._login("owner", "nope")[0], 401)
        self.assertEqual(self._req("GET", "/api/me")[0], 401)

    def test_login_throttle(self):
        for _ in range(5):
            self._login("owner", "wrong")
        self.assertEqual(self._login("owner", "wrong")[0], 429)

    def _seed_outage_event(self, org, name):
        dev = self.store.create_org_device(org, {
            "name": name, "ip_address": "10.0.0.5", "device_type": None,
            "region": None, "parent_device_id": None})
        self.store.open_outage_if_absent(org, dev, "2026-06-23T08:00:00+00:00", "DOWN")

    def test_org_user_is_pinned_to_their_org(self):
        self._seed_outage_event("ispA", "Tower")
        self._seed_outage_event("ispB", "Relay")
        _, cookie = self._login("owner", "ownerpassword")
        status, body, _ = self._req("GET", "/api/logs", cookie=cookie)
        self.assertEqual({e["org_id"] for e in body["events"]}, {"ispA"})
        _, body, _ = self._req("GET", "/api/logs?org=ispB", cookie=cookie)
        self.assertEqual({e["org_id"] for e in body["events"]}, {"ispA"})

    def test_superadmin_sees_all_and_can_narrow(self):
        _, cookie = self._login("root", "rootpassword")
        _, body, _ = self._req("GET", "/api/orgs", cookie=cookie)
        self.assertEqual({o["org_id"] for o in body["orgs"]}, {"ispA", "ispB"})
        _, body, _ = self._req("GET", "/api/orgs?org=ispB", cookie=cookie)
        self.assertEqual({o["org_id"] for o in body["orgs"]}, {"ispB"})

    def test_system_stats_superadmin_only(self):
        _, cookie = self._login("root", "rootpassword")
        status, body, _ = self._req("GET", "/api/system", cookie=cookie)
        self.assertEqual(status, 200)
        for key in ("hostname", "cpu", "memory", "process", "uptime_s",
                    "release_sync", "latest_release"):
            self.assertIn(key, body)
        _, cookie = self._login("owner", "ownerpassword")
        status, _, _ = self._req("GET", "/api/system", cookie=cookie)
        self.assertEqual(status, 403)
        status, _, _ = self._req("GET", "/api/system")
        self.assertEqual(status, 401)

    def test_bearer_token_reads_as_machine_superadmin(self):
        status, body, _ = self._req("GET", "/api/orgs", token="tok")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["orgs"]), 2)

    def test_operator_cannot_write_team_owner_can(self):
        _, op = self._login("oper", "operpassword")
        status, _, _ = self._req("POST", "/api/team",
                                 {"org_id": "ispA", "name": "Bob"}, cookie=op)
        self.assertEqual(status, 403)
        _, own = self._login("owner", "ownerpassword")
        status, body, _ = self._req("POST", "/api/team",
                                    {"org_id": "ispA", "name": "Bob", "role": "operator"}, cookie=own)
        self.assertEqual(status, 200)
        status, _, _ = self._req("POST", "/api/team",
                                 {"org_id": "ispB", "name": "X"}, cookie=own)
        self.assertEqual(status, 403)

    def test_team_and_attendance_round_trip(self):
        _, own = self._login("owner", "ownerpassword")
        self._req("POST", "/api/team", {"org_id": "ispA", "name": "Asha", "role": "operator"}, cookie=own)
        _, team, _ = self._req("GET", "/api/team", cookie=own)
        self.assertEqual(team["team"][0]["name"], "Asha")
        wid = team["team"][0]["id"]
        self._req("POST", "/api/attendance", {"worker_id": wid, "present": True}, cookie=own)
        _, att, _ = self._req("GET", "/api/attendance", cookie=own)
        op = next(o for o in att["operators"] if o["id"] == wid)
        self.assertTrue(op["present_today"])

    def test_superadmin_provisions_org_user(self):
        _, root = self._login("root", "rootpassword")
        status, body, _ = self._req("POST", "/api/users",
            {"org_id": "ispB", "username": "bowner", "password": "bpassword12", "role": "owner"},
            cookie=root)
        self.assertEqual(status, 200)
        self.assertEqual(self._login("bowner", "bpassword12")[0], 200)

    def test_self_service_password_change_requires_current_password(self):
        _, own = self._login("owner", "ownerpassword")
        status, _, _ = self._req("POST", "/api/users/password",
            {"current_password": "wrongpassword", "new_password": "newpassword1"}, cookie=own)
        self.assertEqual(status, 422)
        status, _, _ = self._req("POST", "/api/users/password",
            {"current_password": "ownerpassword", "new_password": "newpassword1"}, cookie=own)
        self.assertEqual(status, 200)
        self.assertEqual(self._login("owner", "ownerpassword")[0], 401)
        self.assertEqual(self._login("owner", "newpassword1")[0], 200)

    def test_owner_can_reset_teammate_password_without_current(self):
        _, own = self._login("owner", "ownerpassword")
        oper_id = self.store.get_user_by_username("oper")["id"]
        status, _, _ = self._req("POST", "/api/users/password",
            {"id": oper_id, "new_password": "resetpassword1"}, cookie=own)
        self.assertEqual(status, 200)
        self.assertEqual(self._login("oper", "resetpassword1")[0], 200)

    def test_operator_cannot_reset_teammate_password(self):
        _, op = self._login("oper", "operpassword")
        owner_id = self.store.get_user_by_username("owner")["id"]
        status, _, _ = self._req("POST", "/api/users/password",
            {"id": owner_id, "new_password": "hijacked12"}, cookie=op)
        self.assertEqual(status, 403)

    def test_owner_cannot_reset_password_of_another_org(self):
        _, own = self._login("owner", "ownerpassword")
        root_id = self.store.get_user_by_username("root")["id"]
        status, _, _ = self._req("POST", "/api/users/password",
            {"id": root_id, "new_password": "hijackroot1"}, cookie=own)
        self.assertEqual(status, 403)

    def test_ingest_uses_bearer_not_session(self):
        _, op = self._login("oper", "operpassword")
        env = {"v": 1, "org_id": "ispA", "node_id": "edge-2", "kind": "heartbeat",
               "body": {"fleet_size": 1}}
        self.assertEqual(self._req("POST", "/heartbeat", env, cookie=op)[0], 401)
        self.assertEqual(self._req("POST", "/heartbeat", env, token="tok")[0], 200)

    def test_orgs_endpoint_is_org_scoped_for_org_users(self):
        self.store.set_org("ispA", ntfy_topic_owner="secret-a-topic")
        self.store.set_org("ispB", ntfy_topic_owner="secret-b-topic")
        _, own = self._login("owner", "ownerpassword")
        status, body, _ = self._req("GET", "/api/orgs", cookie=own)
        self.assertEqual(status, 200)
        self.assertEqual([o["org_id"] for o in body["orgs"]], ["ispA"])
        self.assertEqual(body["orgs"][0]["ntfy_topic_owner"], "secret-a-topic")
        status, body, _ = self._req("GET", "/api/orgs?org=ispB", cookie=own)
        self.assertEqual([o["org_id"] for o in body["orgs"]], ["ispA"])

    def test_superadmin_orgs_sees_all_or_narrows(self):
        self.store.set_org("ispA", name="A"); self.store.set_org("ispB", name="B")
        _, root = self._login("root", "rootpassword")
        _, body, _ = self._req("GET", "/api/orgs", cookie=root)
        self.assertEqual({o["org_id"] for o in body["orgs"]}, {"ispA", "ispB"})
        _, body, _ = self._req("GET", "/api/orgs?org=ispB", cookie=root)
        self.assertEqual([o["org_id"] for o in body["orgs"]], ["ispB"])

    def test_inventory_create_update_delete_round_trip(self):
        _, own = self._login("owner", "ownerpassword")
        status, body, _ = self._req("POST", "/api/inventory",
            {"name": "Core", "ip_address": "10.0.0.1", "device_type": "core"}, cookie=own)
        self.assertEqual(status, 200)
        root_id = body["id"]
        status, body, _ = self._req("POST", "/api/inventory",
            {"name": "Tower", "ip_address": "10.0.0.2", "parent_device_id": root_id}, cookie=own)
        self.assertEqual(status, 200)
        child_id = body["id"]

        status, body, _ = self._req("GET", "/api/inventory", cookie=own)
        self.assertEqual(status, 200)
        self.assertEqual({d["id"] for d in body["devices"]}, {root_id, child_id})

        status, body, _ = self._req("POST", "/api/inventory/update",
            {"id": child_id, "name": "Tower 1", "ip_address": "10.0.0.2",
             "parent_device_id": root_id}, cookie=own)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

        status, body, _ = self._req("POST", "/api/inventory/delete", {"id": root_id}, cookie=own)
        self.assertEqual(status, 409)
        self._req("POST", "/api/inventory/delete", {"id": child_id}, cookie=own)
        status, body, _ = self._req("POST", "/api/inventory/delete", {"id": root_id}, cookie=own)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_inventory_rejects_bad_payload_with_422(self):
        _, own = self._login("owner", "ownerpassword")
        status, body, _ = self._req("POST", "/api/inventory",
            {"name": "Bad", "ip_address": "not-an-ip"}, cookie=own)
        self.assertEqual(status, 422)
        self.assertIn("error", body)

    def test_inventory_operator_cannot_write_owner_can(self):
        _, op = self._login("oper", "operpassword")
        status, _, _ = self._req("POST", "/api/inventory",
            {"name": "X", "ip_address": "10.0.0.5"}, cookie=op)
        self.assertEqual(status, 403)

    def test_inventory_write_cannot_cross_org(self):
        _, own = self._login("owner", "ownerpassword")
        _, body, _ = self._req("POST", "/api/inventory",
            {"name": "A", "ip_address": "10.0.0.1"}, cookie=own)
        dev_id = body["id"]
        status, _, _ = self._req("POST", "/api/inventory",
            {"org_id": "ispB", "name": "B", "ip_address": "10.0.1.1"}, cookie=own)
        self.assertEqual(status, 403)
        status, _, _ = self._req("GET", "/api/inventory?org=ispB", cookie=own)
        self.assertEqual(status, 200)

    def test_inventory_maintenance_and_snmp(self):
        _, own = self._login("owner", "ownerpassword")
        _, body, _ = self._req("POST", "/api/inventory",
            {"name": "Sw1", "ip_address": "10.0.0.9", "device_type": "switch"}, cookie=own)
        did = body["id"]
        status, body, _ = self._req("POST", "/api/inventory/maintenance",
            {"id": did, "on": True}, cookie=own)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        status, body, _ = self._req("POST", "/api/inventory/snmp",
            {"id": did, "snmp_enabled": True, "snmp_community": "public"}, cookie=own)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        status, body, _ = self._req("POST", "/api/inventory/snmp",
            {"id": did, "snmp_enabled": True}, cookie=own)
        self.assertEqual(status, 422)

    def test_org_role_topics_round_trip(self):
        _, own = self._login("owner", "ownerpassword")
        status, body, _ = self._req("POST", "/api/org",
            {"ntfy_topic_owner": "a-owner", "ntfy_topic_operator": "a-op"}, cookie=own)
        self.assertEqual(status, 200)
        status, body, _ = self._req("GET", "/api/orgs", cookie=own)
        org = body["orgs"][0]
        self.assertEqual(org["ntfy_topic_owner"], "a-owner")
        self.assertEqual(org["ntfy_topic_operator"], "a-op")

    def test_test_alert_sends_via_injected_notifier(self):
        _, own = self._login("owner", "ownerpassword")
        self._req("POST", "/api/org", {"ntfy_topic_owner": "a-owner-topic"}, cookie=own)
        status, body, _ = self._req("POST", "/api/test-alert", {"role": "owner"}, cookie=own)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0]["recipient"], "a-owner-topic")

    def test_test_alert_requires_configured_topic(self):
        _, own = self._login("owner", "ownerpassword")
        status, body, _ = self._req("POST", "/api/test-alert", {"role": "tech"}, cookie=own)
        self.assertEqual(status, 422)
        self.assertEqual(len(self.notifier.sent), 0)

    def test_test_alert_operator_cannot_send(self):
        _, own = self._login("owner", "ownerpassword")
        self._req("POST", "/api/org", {"ntfy_topic_owner": "a-owner-topic"}, cookie=own)
        _, op = self._login("oper", "operpassword")
        status, _, _ = self._req("POST", "/api/test-alert", {"role": "owner"}, cookie=op)
        self.assertEqual(status, 403)

    def test_analytics_requires_auth(self):
        self.assertEqual(self._req("GET", "/api/analytics")[0], 401)

    def test_analytics_is_org_scoped_for_org_users(self):
        dev = self.store.create_org_device("ispA", {
            "name": "Tower", "ip_address": "10.0.0.1", "device_type": None,
            "region": None, "parent_device_id": None})
        other = self.store.create_org_device("ispB", {
            "name": "Other", "ip_address": "10.0.0.2", "device_type": None,
            "region": None, "parent_device_id": None})
        _, own = self._login("owner", "ownerpassword")
        status, body, _ = self._req("GET", "/api/analytics", cookie=own)
        self.assertEqual(status, 200)
        ids = {d["device_id"] for d in body["devices"]}
        self.assertIn(dev, ids)
        self.assertNotIn(other, ids)
        status, body, _ = self._req("GET", "/api/analytics?org=ispB", cookie=own)
        self.assertNotIn(other, {d["device_id"] for d in body["devices"]})

    def test_analytics_superadmin_can_narrow(self):
        self.store.create_org_device("ispB", {
            "name": "Other", "ip_address": "10.0.0.2", "device_type": None,
            "region": None, "parent_device_id": None})
        _, root = self._login("root", "rootpassword")
        status, body, _ = self._req("GET", "/api/analytics?org=ispB&days=7", cookie=root)
        self.assertEqual(status, 200)
        self.assertEqual(body["devices"][0]["name"], "Other")

    def test_backup_link_round_trip_and_cross_org_rejected(self):
        _, own = self._login("owner", "ownerpassword")
        primary = self.store.create_org_device("ispA", {
            "name": "Primary", "ip_address": "10.0.1.1", "device_type": None,
            "region": None, "parent_device_id": None})
        backup = self.store.create_org_device("ispA", {
            "name": "Backup", "ip_address": "10.0.1.2", "device_type": None,
            "region": None, "parent_device_id": None})
        child = self.store.create_org_device("ispA", {
            "name": "Relay", "ip_address": "10.0.1.3", "device_type": None,
            "region": None, "parent_device_id": primary})
        status, body, _ = self._req(
            "POST", "/api/inventory/links",
            {"child_id": child, "parent_id": backup}, cookie=own)
        self.assertEqual(status, 200)
        devices = self._req("GET", "/api/inventory", cookie=own)[1]["devices"]
        relay = next(d for d in devices if d["id"] == child)
        self.assertEqual(relay["backup_parents"], [backup])

        other = self.store.create_org_device("ispB", {
            "name": "Other", "ip_address": "10.0.9.9", "device_type": None,
            "region": None, "parent_device_id": None})
        status, body, _ = self._req(
            "POST", "/api/inventory/links",
            {"child_id": child, "parent_id": other}, cookie=own)
        self.assertEqual(status, 422)

        status, _, _ = self._req(
            "POST", "/api/inventory/links/delete",
            {"child_id": child, "parent_id": backup}, cookie=own)
        self.assertEqual(status, 200)
        devices = self._req("GET", "/api/inventory", cookie=own)[1]["devices"]
        relay = next(d for d in devices if d["id"] == child)
        self.assertEqual(relay["backup_parents"], [])

    def test_backup_link_rejects_a_topology_loop(self):
        _, own = self._login("owner", "ownerpassword")
        a = self.store.create_org_device("ispA", {
            "name": "A", "ip_address": "10.0.2.1", "device_type": None,
            "region": None, "parent_device_id": None})
        b = self.store.create_org_device("ispA", {
            "name": "B", "ip_address": "10.0.2.2", "device_type": None,
            "region": None, "parent_device_id": a})
        status, body, _ = self._req(
            "POST", "/api/inventory/links", {"child_id": a, "parent_id": b}, cookie=own)
        self.assertEqual(status, 422)

    def test_operator_cannot_write_backup_links(self):
        _, own = self._login("owner", "ownerpassword")
        a = self.store.create_org_device("ispA", {
            "name": "A", "ip_address": "10.0.3.1", "device_type": None,
            "region": None, "parent_device_id": None})
        b = self.store.create_org_device("ispA", {
            "name": "B", "ip_address": "10.0.3.2", "device_type": None,
            "region": None, "parent_device_id": None})
        _, op = self._login("oper", "operpassword")
        status, _, _ = self._req(
            "POST", "/api/inventory/links", {"child_id": a, "parent_id": b}, cookie=op)
        self.assertEqual(status, 403)

    def test_port_bandwidth_config_round_trip(self):
        _, own = self._login("owner", "ownerpassword")
        switch = self.store.create_org_device("ispA", {
            "name": "Switch", "ip_address": "10.0.4.1", "device_type": "switch",
            "region": None, "parent_device_id": None})
        self.store.upsert_switch_port("ispA", switch, 1, "Gi0/1", None, "up", "up",
                                      None, 0, False, None, "2026-01-01T00:00:00+00:00")
        pid = self.store.list_switch_ports("ispA", switch)[0]["id"]
        status, body, _ = self._req(
            "POST", "/api/inventory/ports/bandwidth",
            {"id": pid, "threshold_mbps": 25, "direction": "out"}, cookie=own)
        self.assertEqual(status, 200)
        row = self.store.list_switch_ports("ispA", switch)[0]
        self.assertEqual(row["bw_threshold_mbps"], 25.0)
        self.assertEqual(row["bw_direction"], "out")

    def test_port_bandwidth_rejects_bad_direction(self):
        _, own = self._login("owner", "ownerpassword")
        switch = self.store.create_org_device("ispA", {
            "name": "Switch", "ip_address": "10.0.4.2", "device_type": "switch",
            "region": None, "parent_device_id": None})
        self.store.upsert_switch_port("ispA", switch, 1, "Gi0/1", None, "up", "up",
                                      None, 0, False, None, "2026-01-01T00:00:00+00:00")
        pid = self.store.list_switch_ports("ispA", switch)[0]["id"]
        status, _, _ = self._req(
            "POST", "/api/inventory/ports/bandwidth",
            {"id": pid, "direction": "sideways"}, cookie=own)
        self.assertEqual(status, 422)

    def test_outage_acknowledge_and_postmortem_round_trip(self):
        dev = self.store.create_org_device("ispA", {
            "name": "Tower", "ip_address": "10.0.5.1", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-a"})
        self.store.open_outage_if_absent("ispA", dev, "2026-06-23T08:00:00+00:00", "DOWN")
        oid = self.store.open_outage_id("ispA", dev)

        _, op = self._login("oper", "operpassword")
        status, body, _ = self._req("GET", "/api/outages", cookie=op)
        self.assertEqual(status, 200)
        self.assertEqual(body["outages"][0]["status"], "unassigned")
        status, _, _ = self._req("POST", "/api/outages/acknowledge", {"outage_id": oid}, cookie=op)
        self.assertEqual(status, 403)

        _, own = self._login("owner", "ownerpassword")
        status, body, _ = self._req("POST", "/api/outages/acknowledge", {"outage_id": oid}, cookie=own)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        status, body, _ = self._req("GET", "/api/outages", cookie=own)
        self.assertEqual(body["outages"][0]["status"], "in_progress")

        status, body, _ = self._req("POST", "/api/outages/postmortem",
                                    {"outage_id": oid, "root_cause": "fiber cut"}, cookie=own)
        self.assertEqual(status, 404)

        self.store.resolve_outage("ispA", dev, "2026-06-23T08:10:00+00:00")
        status, body, _ = self._req(
            "POST", "/api/outages/postmortem",
            {"outage_id": oid, "root_cause": "fiber cut", "resolution_notes": "spliced"},
            cookie=own)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        status, body, _ = self._req("GET", "/api/outages", cookie=own)
        self.assertNotIn(oid, {o["id"] for o in body["outages"]})

    def test_outage_write_cannot_cross_org(self):
        dev = self.store.create_org_device("ispB", {
            "name": "Relay", "ip_address": "10.0.5.2", "device_type": None,
            "region": None, "parent_device_id": None})
        self.store.open_outage_if_absent("ispB", dev, "2026-06-23T08:00:00+00:00", "DOWN")
        oid = self.store.open_outage_id("ispB", dev)
        _, own = self._login("owner", "ownerpassword")
        status, _, _ = self._req("POST", "/api/outages/acknowledge", {"outage_id": oid}, cookie=own)
        self.assertEqual(status, 403)

    def test_logs_endpoint_is_org_scoped(self):
        self._seed_outage_event("ispA", "Tower")
        self._seed_outage_event("ispB", "Relay")
        _, own = self._login("owner", "ownerpassword")
        status, body, _ = self._req("GET", "/api/logs", cookie=own)
        self.assertEqual(status, 200)
        self.assertTrue(body["events"])
        self.assertTrue(all(e["org_id"] == "ispA" for e in body["events"]))

    def test_static_index_served(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.getheader("Content-Type"))
        self.assertIn(b"WISP Central", resp.read())
        conn.close()

if __name__ == "__main__":
    unittest.main()
