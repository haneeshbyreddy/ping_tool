"""Worker location tracking: the public ingest, shifts, and what stays shut.

Workers run the off-the-shelf Traccar Client, which POSTs OsmAnd fixes to
`/field/track` with a per-worker token in the `id` field. Four properties are
worth pinning, and they are the four that would be quietly wrong if nobody did:

  * **the ingest accepts every shape a client build might send** — GET and POST,
    params in the query string or a form body. A fix dropped because we handled
    only one verb is the worst failure this feature has, and it is invisible;
  * **a junk fix never reaches the table, and a refusal never wedges the
    client.** Traccar retries anything it did not get a 2xx for, in order, so a
    4xx on a fix we will never accept blocks every newer one behind it forever —
    the refusals that are ours (too vague, too old) answer 200 with `stored:
    false`, and only a malformed request 400s;
  * **identity comes from the credential**, per-worker, org-scoped, and a
    deactivated account stops reporting;
  * **the retention prune runs**, because the 7-day window is the whole answer to
    what this feature keeps about the people who work for the org.
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from support import RecordingNotifier
from wisp.central import auth, field
from wisp.central.server import make_server
from wisp.central.store import CentralStore
from wisp.config import Config

# Somewhere in Hyderabad, where the fleet this was built for actually works.
LAT, LNG = 17.385044, 78.486671


class _Base(unittest.TestCase):
    """Two orgs; org A has an owner, a worker with a tracker, and one without."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org("ispA", name="A")
        self.store.set_org("ispB", name="B")
        self.owner = auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        self.ravi = auth.create_user(self.store, "ispA", "ravi", "ravipassword", "worker")
        self.kiran = auth.create_user(self.store, "ispA", "kiran", "kiranpassword", "worker")
        self.bowner = auth.create_user(self.store, "ispB", "bowner", "bownerpassword", "owner")
        self.bworker = auth.create_user(self.store, "ispB", "bravo", "bravopassword", "worker")
        self.token = self.store.issue_field_token("ispA", self.ravi)
        self.btoken = self.store.issue_field_token("ispB", self.bworker)

        self.notifier = RecordingNotifier()
        self.server = make_server(self.cfg, self.store, notifier=self.notifier)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

    # ----- transport helpers --------------------------------------------------

    def _req(self, method, path, body=None, cookie=None, ctype=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        payload = None
        if isinstance(body, (dict, list)):
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        elif body is not None:
            payload = body
            headers["Content-Type"] = ctype or "application/x-www-form-urlencoded"
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(raw) if raw else {}
        except ValueError:
            return resp.status, {"_text": raw.decode()}

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

    def _fix_params(self, token=None, **over):
        p = {"id": token if token is not None else self.token,
             "lat": LAT, "lon": LNG, "timestamp": int(time.time()),
             "accuracy": 8.0}
        p.update(over)
        return {k: v for k, v in p.items() if v is not None}

    def _track(self, method="GET", token=None, form=False, **over):
        q = urllib.parse.urlencode(self._fix_params(token, **over))
        if form:
            return self._req("POST", "/field/track", body=q)
        return self._req(method, f"/field/track?{q}")

    def _fixes(self, org="ispA", user_id=None):
        with self.store._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM worker_locations WHERE org_id=?"
                + (" AND user_id=?" if user_id else "") + " ORDER BY ts",
                (org, user_id) if user_id else (org,))]


class IngestTest(_Base):

    def test_a_fix_arrives_over_GET_with_query_params(self):
        status, body = self._track("GET")
        self.assertEqual(status, 200)
        self.assertTrue(body["stored"])
        rows = self._fixes()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["lat"], LAT, places=5)
        self.assertEqual(rows[0]["user_id"], self.ravi)

    def test_a_fix_arrives_over_POST_with_query_params(self):
        # Client builds differ over the verb and nothing about the payload says
        # which one it was, so both have to land — a fix silently dropped for
        # using the other one is invisible from every screen.
        status, body = self._track("POST", timestamp=int(time.time()) - 30)
        self.assertEqual(status, 200)
        self.assertTrue(body["stored"])
        self.assertEqual(len(self._fixes()), 1)

    def test_a_fix_arrives_as_a_form_encoded_POST_body(self):
        status, body = self._track(form=True, timestamp=int(time.time()) - 60)
        self.assertEqual(status, 200)
        self.assertTrue(body["stored"])
        self.assertEqual(len(self._fixes()), 1)

    def test_speed_is_converted_from_knots_to_metres_per_second(self):
        # The OsmAnd protocol's unit is KNOTS — Traccar Client converts the
        # platform's m/s before it sends. Storing the wire number would put a
        # figure ~2x high on every screen, the same class of mistake as the dbc
        # profile's distance-in-time-quanta.
        self._track(speed=10.0)
        row = self._fixes()[0]
        self.assertAlmostEqual(row["speed_mps"], 5.14, places=2)

    def test_a_replayed_fix_lands_once(self):
        # Traccar re-sends anything it did not get a 200 for, so the same
        # position legitimately arrives twice. Twice in the table would render
        # as a stutter in the trail.
        ts = int(time.time())
        first = self._track(timestamp=ts)
        second = self._track(timestamp=ts)
        self.assertEqual((first[0], second[0]), (200, 200))
        self.assertTrue(first[1]["stored"])
        self.assertFalse(second[1]["stored"])
        self.assertEqual(len(self._fixes()), 1)

    def test_battery_bearing_and_accuracy_ride_along(self):
        self._track(batt=63, bearing=181.5, accuracy=12.25)
        row = self._fixes()[0]
        self.assertEqual(row["battery_pct"], 63)
        self.assertAlmostEqual(row["heading"], 181.5, places=1)
        self.assertAlmostEqual(row["accuracy_m"], 12.2, places=1)


class RefusalTest(_Base):

    def test_an_unknown_token_is_a_flat_401_and_writes_nothing(self):
        status, _ = self._track(token="not-a-real-token")
        self.assertEqual(status, 401)
        self.assertEqual(self._fixes(), [])

    def test_no_token_at_all_is_a_401(self):
        status, _ = self._req("GET", f"/field/track?lat={LAT}&lon={LNG}")
        self.assertEqual(status, 401)
        self.assertEqual(self._fixes(), [])

    def test_a_revoked_token_stops_reporting(self):
        self.assertTrue(self.store.revoke_field_token("ispA", self.ravi))
        self.assertEqual(self._track()[0], 401)
        self.assertEqual(self._fixes(), [])

    def test_rotating_replaces_the_old_token_and_un_revokes(self):
        old = self.token
        self.store.revoke_field_token("ispA", self.ravi)
        fresh = self.store.issue_field_token("ispA", self.ravi)
        self.assertEqual(self._track(token=old)[0], 401)
        self.assertEqual(self._track(token=fresh)[0], 200)

    def test_a_deactivated_account_stops_reporting(self):
        # Switching an account off has to stop its phone too, or "deactivated"
        # means less than it says. Same instinct as the paging assignment map
        # joining users.is_active.
        self.store.set_user_active(self.ravi, False)
        self.assertEqual(self._track()[0], 401)
        self.assertEqual(self._fixes(), [])

    def test_a_missing_coordinate_is_a_400(self):
        # Malformed, not merely unwanted: Traccar never sends this, so wedging
        # its buffer is not a risk, and a silent 200 would hide a broken client.
        status, _ = self._req("GET", f"/field/track?id={self.token}&lat={LAT}")
        self.assertEqual(status, 400)
        self.assertEqual(self._fixes(), [])

    def test_an_out_of_range_coordinate_is_a_400(self):
        self.assertEqual(self._track(lat=101.0)[0], 400)
        self.assertEqual(self._track(lon=-999.0)[0], 400)
        self.assertEqual(self._fixes(), [])

    def test_a_cell_tower_estimate_is_dropped_but_ANSWERED_200(self):
        # THE refusal that must not be a 4xx. A phone indoors emits these all
        # day; a 400 would park one at the head of the offline buffer and every
        # real fix behind it would never arrive.
        status, body = self._track(accuracy=2000)
        self.assertEqual(status, 200)
        self.assertFalse(body["stored"])
        self.assertIn("cell-tower", body["reason"])
        self.assertEqual(self._fixes(), [])

    def test_a_stale_fix_is_dropped_and_a_buffered_one_is_not(self):
        # Offline buffering is a setting we RECOMMEND — the crew drives through
        # dead zones — so a morning replaying at once has to land. Only past the
        # retention window does a fix stop being worth keeping.
        now = int(time.time())
        self.assertTrue(self._track(timestamp=now - 3 * 3600)[1]["stored"])
        old = self._track(timestamp=now - 40 * 24 * 3600)
        self.assertEqual(old[0], 200)
        self.assertFalse(old[1]["stored"])
        self.assertEqual(len(self._fixes()), 1)

    def test_a_fix_from_the_future_is_dropped(self):
        # A broken phone clock. It would sort to the head of the trail and read
        # as "here now", which is the one thing this layer may not get wrong.
        status, body = self._track(timestamp=int(time.time()) + 9999)
        self.assertEqual(status, 200)
        self.assertFalse(body["stored"])
        self.assertIn("future", body["reason"])
        self.assertEqual(self._fixes(), [])

    def test_a_looping_client_is_rate_capped(self):
        # The cap is a token BUCKET, so a buffer flush passes and a loop does
        # not. Drain the bucket, then expect 429 rather than an unbounded write
        # rate against the DB.
        seen_429 = False
        for i in range(400):
            status, _ = self._track(timestamp=int(time.time()) - i)
            if status == 429:
                seen_429 = True
                break
        self.assertTrue(seen_429, "a looping client should eventually be capped")

    def test_a_burst_the_size_of_an_hours_buffer_still_lands(self):
        # At the designed 90s cadence an hour is 40 fixes. If the cap threw that
        # away, "offline buffering ON" would be advice that loses data.
        now = int(time.time())
        stored = 0
        for i in range(40):
            status, body = self._track(timestamp=now - i * 90)
            self.assertEqual(status, 200)
            stored += 1 if body.get("stored") else 0
        self.assertEqual(stored, 40)


class ScopingTest(_Base):

    def test_a_token_writes_only_into_its_own_org(self):
        # Identity comes FROM the credential; the request carries no other claim
        # about who is reporting.
        self._track(token=self.btoken)
        self.assertEqual(self._fixes("ispA"), [])
        rows = self._fixes("ispB")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_id"], self.bworker)

    def test_the_owners_view_is_scoped_to_its_own_org(self):
        self._track()
        self._track(token=self.btoken)
        cookie = self._login("bowner", "bownerpassword")
        status, body = self._req("GET", "/api/field/workers?org=ispB", cookie=cookie)
        self.assertEqual(status, 200)
        names = {w["username"] for w in body["workers"]}
        self.assertEqual(names, {"bowner", "bravo"})

    def test_an_owner_cannot_read_another_orgs_crew(self):
        cookie = self._login("owner", "ownerpassword")
        # _scope_org pins a non-superadmin to its own org whatever ?org= says,
        # so this must answer about ispA, never ispB.
        status, body = self._req("GET", "/api/field/workers?org=ispB", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertNotIn("bravo", {w["username"] for w in body["workers"]})

    def test_an_owner_cannot_mint_a_credential_in_another_org(self):
        # The org comes from the CALLER, never the body — so naming somebody
        # else's user_id resolves against ispA, finds no such account and 404s
        # rather than minting a credential inside ispB. The existing token there
        # is untouched, and ispB's owner gains none.
        before = {r["user_id"]: r["issued_at"]
                  for r in self.store.list_field_tokens("ispB")}
        cookie = self._login("owner", "ownerpassword")
        for uid in (self.bworker, self.bowner):
            # naming the other org outright: refused at the org gate
            self.assertEqual(self._req("POST", "/api/field/token",
                                       {"user_id": uid, "org_id": "ispB"},
                                       cookie=cookie)[0], 403)
            # …and without it the write is scoped to ispA, where that account
            # does not exist
            self.assertEqual(self._req("POST", "/api/field/token",
                                       {"user_id": uid}, cookie=cookie)[0], 404)
        after = {r["user_id"]: r["issued_at"]
                 for r in self.store.list_field_tokens("ispB")}
        self.assertEqual(after, before)
        self.assertIsNone(after[self.bowner])
        self.assertEqual(self.store.resolve_field_token(self.btoken),
                         ("ispB", self.bworker))


class WorkerAllowlistTest(_Base):
    """Deny-by-default is the property, not the individual entries.

    A worker gets the full shell read-only, so a NEW route stays worker-blocked
    until it is deliberately placed in `_WORKER_GET`/`_WORKER_POST`. Exactly two
    of this feature's routes were: the worker's OWN shift, read and written.
    """

    def test_a_worker_may_read_and_write_only_its_own_shift(self):
        wrk = self._login("ravi", "ravipassword")
        self.assertEqual(self._req("GET", "/api/field/shift", cookie=wrk)[0], 200)
        self.assertEqual(self._req("POST", "/api/field/shift",
                                   {"action": "start"}, cookie=wrk)[0], 200)

    def test_a_worker_cannot_see_where_the_crew_is(self):
        # Where everyone is, is the owner's view of the org. A worker needing to
        # know that about a colleague is a phone call, not a screen.
        wrk = self._login("ravi", "ravipassword")
        for path in ("/api/field/workers", "/api/field/tokens"):
            self.assertEqual(self._req("GET", path, cookie=wrk)[0], 403,
                             f"GET {path} should be worker-blocked")

    def test_a_worker_cannot_issue_or_revoke_a_tracker_credential(self):
        wrk = self._login("ravi", "ravipassword")
        for path in ("/api/field/token", "/api/field/token/revoke"):
            self.assertEqual(
                self._req("POST", path, {"user_id": self.kiran}, cookie=wrk)[0], 403,
                f"POST {path} should be worker-blocked")


class ShiftTest(_Base):

    def test_start_and_end_are_both_idempotent(self):
        # The dashboard button and a stale tab press the same thing; two
        # overlapping shifts would make "when did he start" unanswerable.
        wrk = self._login("ravi", "ravipassword")
        first = self._req("POST", "/api/field/shift", {"action": "start"}, cookie=wrk)[1]
        second = self._req("POST", "/api/field/shift", {"action": "start"}, cookie=wrk)[1]
        self.assertFalse(first["already"])
        self.assertTrue(second["already"])
        with self.store._connect() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM worker_shifts WHERE org_id='ispA'").fetchone()[0], 1)
        self.assertFalse(
            self._req("POST", "/api/field/shift", {"action": "end"}, cookie=wrk)[1]["already"])
        self.assertTrue(
            self._req("POST", "/api/field/shift", {"action": "end"}, cookie=wrk)[1]["already"])

    def test_the_shift_is_the_callers_own_and_the_body_cannot_say_otherwise(self):
        # org_id and user_id come from the SESSION. A shift is a statement about
        # who is working, and a body-supplied identity would let anyone make it
        # about somebody else.
        wrk = self._login("ravi", "ravipassword")
        self._req("POST", "/api/field/shift",
                  {"action": "start", "user_id": self.kiran, "org_id": "ispB"},
                  cookie=wrk)
        self.assertIsNotNone(self.store.open_shift("ispA", self.ravi))
        self.assertIsNone(self.store.open_shift("ispA", self.kiran))
        self.assertIsNone(self.store.open_shift("ispB", self.kiran))

    def test_a_bad_action_is_refused(self):
        wrk = self._login("ravi", "ravipassword")
        self.assertEqual(self._req("POST", "/api/field/shift",
                                   {"action": "pause"}, cookie=wrk)[0], 422)

    def test_the_shift_reply_says_whether_a_tracker_exists_at_all(self):
        # "On shift" with no credential is a declaration nothing can
        # corroborate, and the worker should be told rather than left wondering.
        ravi = self._login("ravi", "ravipassword")
        kiran = self._login("kiran", "kiranpassword")
        self.assertTrue(self._req("GET", "/api/field/shift", cookie=ravi)[1]["has_token"])
        self.assertFalse(self._req("GET", "/api/field/shift", cookie=kiran)[1]["has_token"])


class OwnerViewTest(_Base):

    def test_every_account_is_listed_even_one_that_never_reported(self):
        # "Set up but never worked" and "on shift and gone quiet" are two of the
        # four states the map must tell apart, and neither can be rendered from
        # a list containing only people who sent something.
        self._track()
        cookie = self._login()
        body = self._req("GET", "/api/field/workers?org=ispA", cookie=cookie)[1]
        by_name = {w["username"]: w for w in body["workers"]}
        self.assertEqual(set(by_name), {"owner", "ravi", "kiran"})
        self.assertIsNotNone(by_name["ravi"]["last_fix"])
        self.assertIsNone(by_name["kiran"]["last_fix"])
        self.assertTrue(by_name["ravi"]["has_token"])
        self.assertFalse(by_name["kiran"]["has_token"])

    def test_the_reply_carries_the_freshness_threshold_it_is_judged_by(self):
        # The four states are classified in the SPA (freshness ticks with the
        # clock), but the THRESHOLD has one source — here.
        cookie = self._login()
        body = self._req("GET", "/api/field/workers?org=ispA", cookie=cookie)[1]
        self.assertEqual(body["fresh_s"], self.cfg.field_track_fresh_s)
        self.assertEqual(body["retention_days"], self.cfg.field_track_retention_days)

    def test_the_trail_is_todays_fixes_in_time_order(self):
        now = int(time.time())
        for i in (3, 1, 2):      # deliberately out of order on the wire
            self._track(timestamp=now - i * 60, lat=LAT + i / 1000)
        cookie = self._login()
        body = self._req("GET", "/api/field/workers?org=ispA", cookie=cookie)[1]
        trail = next(w for w in body["workers"] if w["username"] == "ravi")["trail"]
        self.assertEqual(len(trail), 3)
        # oldest → newest: the fix 3 minutes ago is the first point
        self.assertAlmostEqual(trail[0][0], LAT + 0.003, places=5)
        self.assertAlmostEqual(trail[-1][0], LAT + 0.001, places=5)

    def test_the_shift_state_rides_the_worker_row(self):
        wrk = self._login("ravi", "ravipassword")
        self._req("POST", "/api/field/shift", {"action": "start"}, cookie=wrk)
        cookie = self._login()
        body = self._req("GET", "/api/field/workers?org=ispA", cookie=cookie)[1]
        ravi = next(w for w in body["workers"] if w["username"] == "ravi")
        self.assertTrue(ravi["on_shift"])
        self.assertIsNotNone(ravi["shift_started_at"])

    def test_the_token_roster_never_carries_a_token(self):
        # Only a SHA-256 hash is stored; the plaintext is shown once at issue and
        # is not recoverable. The panel says so, and this pins that it is true.
        cookie = self._login()
        body = self._req("GET", "/api/field/tokens?org=ispA", cookie=cookie)[1]
        blob = json.dumps(body)
        self.assertNotIn(self.token, blob)
        for row in body["accounts"]:
            self.assertNotIn("token", row)
        self.assertTrue(body["server_url"].endswith("/field/track"))

    def test_issuing_returns_the_plaintext_once_and_it_works(self):
        cookie = self._login()
        status, body = self._req("POST", "/api/field/token",
                                 {"user_id": self.kiran}, cookie=cookie)
        self.assertEqual(status, 200)
        fresh = body["token"]
        self.assertEqual(self._track(token=fresh)[0], 200)
        rows = self._fixes("ispA", self.kiran)
        self.assertEqual(len(rows), 1)
        # …and it is never readable again
        roster = self._req("GET", "/api/field/tokens?org=ispA", cookie=cookie)[1]
        self.assertNotIn(fresh, json.dumps(roster))


class PruneTest(_Base):

    def test_fixes_past_the_retention_window_are_deleted(self):
        # Ship this without the prune and the feature stops being "a short trail"
        # and becomes a movement archive of staff, which is a different thing to
        # be holding.
        now = datetime.now(timezone.utc)
        for days in (0, 1, 6, 8, 30):
            self.store.record_worker_fix("ispA", self.ravi, {
                "ts": (now - timedelta(days=days)).isoformat(timespec="seconds"),
                "lat": LAT, "lng": LNG})
        self.assertEqual(len(self._fixes()), 5)
        removed = field.prune_worker_locations(self.store, self.cfg, now)
        self.assertEqual(removed, 2)
        self.assertEqual(len(self._fixes()), 3)

    def test_the_prune_is_org_wide_and_leaves_fresh_rows_alone(self):
        now = datetime.now(timezone.utc)
        fresh = (now - timedelta(hours=2)).isoformat(timespec="seconds")
        stale = (now - timedelta(days=20)).isoformat(timespec="seconds")
        for org, uid in (("ispA", self.ravi), ("ispB", self.bworker)):
            self.store.record_worker_fix(org, uid, {"ts": fresh, "lat": LAT, "lng": LNG})
            self.store.record_worker_fix(org, uid, {"ts": stale, "lat": LAT, "lng": LNG})
        field.prune_worker_locations(self.store, self.cfg, now)
        self.assertEqual(len(self._fixes("ispA")), 1)
        self.assertEqual(len(self._fixes("ispB")), 1)


class OrgLifecycleTest(_Base):

    def test_deleting_an_org_takes_its_tracking_data_with_it(self):
        # delete_org DISCOVERS org-scoped tables rather than listing them, and
        # org ids are reusable — rows left behind would surface inside a later
        # org of the same name.
        self._track(token=self.btoken)
        self.store.start_shift("ispB", self.bworker)
        self.store.delete_org("ispB")
        with self.store._connect() as conn:
            for table in ("worker_locations", "worker_shifts", "field_tokens"):
                left = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE org_id='ispB'").fetchone()[0]
                self.assertEqual(left, 0, f"{table} kept rows for a deleted org")
        # …and org A is untouched
        self.assertEqual(len(self.store.list_field_tokens("ispA")), 3)

    def test_deleting_an_account_takes_its_trail_with_it(self):
        self._track()
        self.store.start_shift("ispA", self.ravi)
        self.store.delete_user(self.ravi)
        self.assertEqual(self._fixes(), [])
        with self.store._connect() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM field_tokens WHERE user_id=?",
                (self.ravi,)).fetchone()[0], 0)


class BillingTest(_Base):

    def test_a_locked_org_keeps_recording_where_its_staff_are(self):
        # Consistent with edge ingest: a lapsed bill must never silently stop
        # monitoring, and it must not silently stop this either.
        self.store.set_org_plan("ispA", "pro")
        status, body = self._track()
        self.assertEqual(status, 200)
        self.assertTrue(body["stored"])


class PureRulesTest(unittest.TestCase):
    """`field.clean_fix` on its own — the shapes a real client sends."""

    def setUp(self):
        self.cfg = Config()
        self.now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

    def _clean(self, **params):
        return field.clean_fix({k: [str(v)] for k, v in params.items()},
                               self.cfg, self.now)

    def test_a_millisecond_timestamp_is_understood(self):
        ms = int(self.now.timestamp() * 1000)
        fix = self._clean(lat=LAT, lon=LNG, timestamp=ms)
        self.assertEqual(fix["ts"], self.now.isoformat(timespec="seconds"))

    def test_an_iso_timestamp_is_understood(self):
        fix = self._clean(lat=LAT, lon=LNG, timestamp="2026-08-01T11:59:00Z")
        self.assertTrue(fix["ts"].startswith("2026-08-01T11:59:00"))

    def test_an_absent_timestamp_means_now(self):
        # A fix with no clock is still a position, and the request only just
        # arrived — refusing it would lose data for a field nobody needs.
        fix = self._clean(lat=LAT, lon=LNG)
        self.assertEqual(fix["ts"], self.now.isoformat(timespec="seconds"))

    def test_lng_and_longitude_are_both_accepted(self):
        # Cheaper to accept every spelling than to diagnose one silent field
        # months later.
        self.assertAlmostEqual(self._clean(lat=LAT, lng=LNG)["lng"], LNG, places=5)
        self.assertAlmostEqual(self._clean(lat=LAT, longitude=LNG)["lng"], LNG, places=5)

    def test_a_nonsense_battery_or_heading_is_dropped_not_fatal(self):
        # One junk optional field must not lose the position it came with.
        fix = self._clean(lat=LAT, lon=LNG, batt=999, bearing=-40)
        self.assertIsNone(fix["battery_pct"])
        self.assertIsNone(fix["heading"])
        self.assertAlmostEqual(fix["lat"], LAT, places=5)

    def test_a_fix_with_no_accuracy_is_kept(self):
        # Absent is not the same as bad: some builds omit it, and the
        # coordinates are still worth having.
        self.assertIsNone(self._clean(lat=LAT, lon=LNG)["accuracy_m"])

    def test_the_rate_bucket_refills(self):
        rate = field.TrackRate(per_min=60)
        t = 1000.0
        allowed = sum(1 for _ in range(200) if rate.allow("k", now=t))
        self.assertEqual(allowed, 120)           # the burst ceiling
        self.assertFalse(rate.allow("k", now=t))
        self.assertTrue(rate.allow("k", now=t + 2))   # …and it refills
        # a second worker has its own bucket
        self.assertTrue(rate.allow("other", now=t))


if __name__ == "__main__":
    unittest.main()
