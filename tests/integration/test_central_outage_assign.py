import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.central import auth
from wisp.central.server import make_server
from wisp.central.store import CentralStore
from wisp.config import Config
from support import RecordingNotifier

T0 = "2026-07-26T04:00:00+00:00"


class OutageAssignTest(unittest.TestCase):
    """Assigning an open outage to named field accounts.

    Two properties carry this feature: the page reaches EXACTLY the assignees
    (not the org audience — the whole point of naming two people), and assignment
    counts as triage so the card stops rendering as untouched while somebody is
    driving to the site."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        self.ravi = auth.create_user(self.store, "ispA", "ravi", "ravipassword", "worker")
        self.kiran = auth.create_user(self.store, "ispA", "kiran", "kiranpassword", "worker")
        auth.create_user(self.store, "ispA", "nonumber", "nonumberpassword", "worker")
        auth.create_user(self.store, "ispB", "bowner", "bownerpassword", "owner")
        self.store.set_user_whatsapp(self._uid("ravi"), "+919000000001")
        self.store.set_user_whatsapp(self._uid("kiran"), "+919000000002")
        self.store.set_user_whatsapp(self._uid("owner"), "+919000000009")
        self.device = self.store.create_org_device("ispA", {
            "name": "PYLON-OLT", "ip_address": "10.0.0.1", "device_type": "olt",
            "region": "north", "parent_device_id": None,
            "assigned_node_id": "probe1"})
        self.store.open_outage_if_absent("ispA", self.device, T0, "DOWN")
        self.outage = self.store.triage_outages("ispA")[0]["id"]
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

    def _uid(self, username):
        return next(u["id"] for u in self.store.list_users("ispA")
                    if u["username"] == username)

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
        conn.close()
        return resp.status, (json.loads(raw) if raw else {})

    def _login(self, username="owner", password="ownerpassword"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/login",
                     body=json.dumps({"username": username, "password": password}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        cookie = resp.getheader("Set-Cookie")
        conn.close()
        return cookie.split(";")[0] if cookie else None

    def _row(self, org="ispA"):
        return next(o for o in self.store.triage_outages(org)
                    if o["id"] == self.outage)

    def _assign(self, names, cookie=None):
        return self._req("POST", "/api/outages/assign",
                         {"outage_id": self.outage, "usernames": names},
                         cookie=cookie or self._login())

    def _accept(self, cookie=None):
        return self._req("POST", "/api/outages/accept",
                         {"outage_id": self.outage},
                         cookie=cookie or self._login())

    # --- the happy path -------------------------------------------------------

    def test_owner_assigns_several_workers_at_once(self):
        status, body = self._assign(["ravi", "kiran"])
        self.assertEqual(status, 200)
        self.assertEqual(body["assigned_to"], ["ravi", "kiran"])
        row = self._row()
        self.assertEqual(row["assigned_to"], ["ravi", "kiran"])
        self.assertEqual(row["assigned_by"], "owner")
        self.assertIsNotNone(row["assigned_at"])

    def test_assignment_is_an_ask_not_an_answer(self):
        # The property this feature turns on: naming somebody does NOT make the
        # device less down. The card stays destructive-toned and says what is
        # missing (a reply) until an assignee accepts — an owner sending a
        # message is not a human taking the job on.
        self.assertEqual(self._row()["status"], "unassigned")
        self._assign(["ravi"])
        row = self._row()
        self.assertEqual(row["status"], "assigned")
        self.assertIsNone(row["acknowledged_at"])
        self.assertEqual(row["accepted_by"], [])

    def test_an_earlier_acknowledgement_keeps_its_own_name(self):
        # An explicit ack is still a human owning it, so assigning on top of one
        # leaves the outage `in_progress` under that person's name and clock —
        # assignment records who was asked, it never rewrites who answered.
        self.store.acknowledge_outage("ispA", self.outage, "ravi")
        acked_at = self._row()["acknowledged_at"]
        self._assign(["kiran"])
        row = self._row()
        self.assertEqual(row["acknowledged_by"], "ravi")
        self.assertEqual(row["acknowledged_at"], acked_at)
        self.assertEqual(row["assigned_to"], ["kiran"])
        self.assertEqual(row["status"], "in_progress")

    def test_reassigning_replaces_the_set(self):
        self._assign(["ravi", "kiran"])
        self._assign(["kiran"])
        self.assertEqual(self._row()["assigned_to"], ["kiran"])

    # --- accepting ------------------------------------------------------------

    def test_an_assignee_accepting_moves_it_to_in_progress(self):
        self._assign(["ravi", "kiran"])
        status, body = self._accept(cookie=self._login("ravi", "ravipassword"))
        self.assertEqual(status, 200)
        self.assertFalse(body["already"])
        row = self._row()
        self.assertEqual(row["status"], "in_progress")
        self.assertEqual(row["accepted_by"], ["ravi"])
        self.assertIsNotNone(row["accepted_at"])
        # accepting IS acknowledging — a worker shouldn't press two buttons
        self.assertEqual(row["acknowledged_by"], "ravi")

    def test_the_other_assignee_is_still_shown_as_unanswered(self):
        # One yes moves the outage, but the roster of who has actually replied is
        # the thing the owner is reading — it must not collapse into "assigned".
        self._assign(["ravi", "kiran"])
        self._accept(cookie=self._login("ravi", "ravipassword"))
        row = self._row()
        self.assertEqual(row["assigned_to"], ["ravi", "kiran"])
        self.assertEqual(row["accepted_by"], ["ravi"])

    def test_accepting_twice_is_not_an_error(self):
        # The dashboard button and the WhatsApp button press the same thing.
        self._assign(["ravi"])
        cookie = self._login("ravi", "ravipassword")
        self._accept(cookie=cookie)
        status, body = self._accept(cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(body["already"])
        self.assertEqual(self._row()["accepted_by"], ["ravi"])

    def test_only_a_named_assignee_may_accept(self):
        # Accepting answers a question that was put to you; a yes from whoever
        # else saw the card would make "who accepted" mean nothing.
        self._assign(["ravi"])
        status, _ = self._accept(cookie=self._login("kiran", "kiranpassword"))
        self.assertEqual(status, 403)
        self.assertEqual(self._row()["accepted_by"], [])
        self.assertEqual(self._row()["status"], "assigned")

    def test_another_orgs_account_cannot_accept(self):
        self._assign(["ravi"])
        status, _ = self._accept(cookie=self._login("bowner", "bownerpassword"))
        self.assertEqual(status, 403)

    def test_reassignment_keeps_a_yes_from_whoever_is_still_named(self):
        # Adding a second name must not ask somebody who already said yes to say
        # it again; whoever is dropped loses their acceptance with the job.
        self._assign(["ravi", "kiran"])
        self._accept(cookie=self._login("ravi", "ravipassword"))
        self._assign(["ravi", "nonumber"])
        self.assertEqual(self._row()["accepted_by"], ["ravi"])
        self._assign(["kiran"])
        row = self._row()
        self.assertEqual(row["accepted_by"], [])
        # the ack stands (ravi really did take it on at the time), so this is
        # still a touched outage — but nobody currently named has answered
        self.assertIsNone(row["accepted_at"])

    def test_accepting_writes_a_log_event(self):
        self._assign(["ravi"])
        self._accept(cookie=self._login("ravi", "ravipassword"))
        ev = next(e for e in self.store.list_events("ispA")
                  if e["type"] == "OUTAGE_ACCEPTED")
        self.assertEqual(ev["payload"]["by"], "ravi")
        self.assertEqual(ev["device_name"], "PYLON-OLT")

    def test_the_owner_who_assigned_it_hears_the_answer(self):
        self._assign(["ravi"])
        self.notifier.texts.clear()
        self._accept(cookie=self._login("ravi", "ravipassword"))
        told = [t for t in self.notifier.texts if t["to"] == "+919000000009"]
        self.assertTrue(told)
        self.assertIn("ravi", told[0]["body"])

    def test_an_assignment_writes_a_log_event(self):
        self._assign(["ravi", "kiran"])
        ev = next(e for e in self.store.list_events("ispA")
                  if e["type"] == "OUTAGE_ASSIGNED")
        self.assertEqual(ev["payload"]["to"], ["ravi", "kiran"])
        self.assertEqual(ev["payload"]["by"], "owner")
        self.assertEqual(ev["device_name"], "PYLON-OLT")

    # --- who hears about it ---------------------------------------------------

    def test_only_the_assignees_are_paged(self):
        # NOT org_alert_recipients: the owner's own number is deliberately absent,
        # or "assigned to you" would reach everybody and mean nothing.
        self._assign(["ravi"])
        self.assertEqual([b["to"] for b in self.notifier.buttons],
                         ["+919000000001"])

    def test_the_page_carries_the_accept_button(self):
        # The reason a worker at a pole never has to open the dashboard: the
        # assignment arrives with the yes attached to it.
        self._assign(["ravi"])
        ids = [bid for bid, _ in self.notifier.buttons[0]["buttons"]]
        self.assertIn(f"acc:{self.outage}", ids)

    def test_a_shut_24h_window_falls_back_to_the_template(self):
        # Meta only allows a free-form (buttoned) message inside the recipient's
        # own 24h window. When it is shut the page must still LAND — one message
        # each either way, never a silent nothing.
        self.notifier.free_ok = False
        _, body = self._assign(["ravi"])
        self.assertEqual(len(self.notifier.sent), 1)
        sent = self.notifier.sent[0]
        self.assertEqual(sent["whatsapp"], ["+919000000001"])
        self.assertEqual(sent["facts"].status, "ASSIGNED")
        self.assertIn("PYLON-OLT", sent["facts"].subject)
        self.assertEqual(body["notified"], 1)

    def test_both_assignees_are_paged(self):
        self._assign(["ravi", "kiran"])
        self.assertEqual(sorted(b["to"] for b in self.notifier.buttons),
                         ["+919000000001", "+919000000002"])

    def test_an_assignee_without_a_number_is_reported_not_silently_dropped(self):
        # The assignment stands (they will see it in their own view), but the
        # owner is told the page didn't reach them.
        _, body = self._assign(["ravi", "nonumber"])
        self.assertEqual(body["assigned_to"], ["ravi", "nonumber"])
        self.assertEqual(body["notified"], 1)

    def test_a_failing_whatsapp_send_never_undoes_the_assignment(self):
        self.notifier.ok = False
        status, body = self._assign(["ravi"])
        self.assertEqual(status, 200)
        self.assertEqual(self._row()["assigned_to"], ["ravi"])

    # --- who may do it --------------------------------------------------------

    def test_a_worker_cannot_assign(self):
        # Dispatch is owner-only; a worker triages (ack/post-mortem), it doesn't
        # decide who goes out.
        status, _ = self._assign(["kiran"],
                                 cookie=self._login("ravi", "ravipassword"))
        self.assertEqual(status, 403)
        self.assertEqual(self._row()["assigned_to"], [])

    def test_another_orgs_owner_cannot_assign(self):
        status, _ = self._assign(["ravi"],
                                 cookie=self._login("bowner", "bownerpassword"))
        self.assertEqual(status, 403)

    def test_signed_out_callers_get_401(self):
        status, _ = self._req("POST", "/api/outages/assign",
                              {"outage_id": self.outage, "usernames": ["ravi"]})
        self.assertEqual(status, 401)

    # --- refusals -------------------------------------------------------------

    def test_an_empty_list_is_refused(self):
        # There is no "assigned to nobody": clearing would be an ambiguous
        # half-state, so re-assigning means naming somebody else.
        status, body = self._assign([])
        self.assertEqual(status, 422)
        self.assertEqual(self.notifier.sent, [])

    def test_a_username_from_another_org_is_refused(self):
        status, _ = self._assign(["bowner"])
        self.assertEqual(status, 422)
        self.assertEqual(self._row()["assigned_to"], [])

    def test_a_deactivated_account_cannot_be_assigned(self):
        self.store.set_user_active(self._uid("kiran"), False)
        status, _ = self._assign(["kiran"])
        self.assertEqual(status, 422)

    def test_a_resolved_outage_cannot_be_assigned(self):
        self.store.resolve_outage("ispA", self.device, T0)
        status, _ = self._assign(["ravi"])
        self.assertEqual(status, 409)

    def test_an_unknown_outage_is_a_404(self):
        status, _ = self._req("POST", "/api/outages/assign",
                              {"outage_id": 9999, "usernames": ["ravi"]},
                              cookie=self._login())
        self.assertEqual(status, 404)

    # --- the wire -------------------------------------------------------------

    def test_the_outage_list_always_carries_a_real_list(self):
        # No consumer should have to know the column was ever NULL.
        for o in self.store.triage_outages("ispA"):
            self.assertIsInstance(o["assigned_to"], list)
        _, body = self._req("GET", "/api/outages?org=ispA", cookie=self._login())
        self.assertEqual(body["outages"][0]["assigned_to"], [])

    def test_a_corrupt_assigned_to_row_reads_as_nobody(self):
        # A hand-edited DB must not turn into a fabricated name on a triage card.
        with self.store._connect() as conn:
            conn.execute("UPDATE outages SET assigned_to='not json' WHERE id=?",
                         (self.outage,))
            conn.commit()
        self.assertEqual(self._row()["assigned_to"], [])


if __name__ == "__main__":
    unittest.main()
