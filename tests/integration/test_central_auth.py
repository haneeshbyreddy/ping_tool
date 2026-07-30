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
from wisp.central import auth, totp
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
        auth.create_user(self.store, "ispA", "wanda", "longenough", "worker")

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
        self.assertEqual(auth.verify_session(tok, cfg=self.cfg), uid)
        self.assertIsNone(auth.verify_session(tok + "x", cfg=self.cfg))
        old = auth.issue_session(uid, self.cfg, now=time.time() - 13 * 3600)
        self.assertIsNone(auth.verify_session(old, cfg=self.cfg, now=time.time()))
        user = auth.resolve_session(self.store, tok, cfg=self.cfg)
        self.assertTrue(user["is_superadmin"])
        self.assertNotIn("pw_hash", user)

    def test_trusted_device_session_outlives_the_short_timeout(self):
        uid = auth.create_user(self.store, None, "root", "supersecret")
        # 13h past issue: a normal session is dead, a trusted one still lives.
        past = time.time() - 13 * 3600
        normal = auth.issue_session(uid, self.cfg, now=past)
        trusted = auth.issue_session(uid, self.cfg, remember=True, now=past)
        self.assertIsNone(auth.verify_session(normal, cfg=self.cfg, now=time.time()))
        self.assertEqual(auth.verify_session(trusted, cfg=self.cfg, now=time.time()), uid)
        # But even a trusted session expires past its own (days-long) window.
        way_past = time.time() - (self.cfg.session_remember_days + 1) * 86400
        stale = auth.issue_session(uid, self.cfg, remember=True, now=way_past)
        self.assertIsNone(auth.verify_session(stale, cfg=self.cfg, now=time.time()))
        # A tampered field breaks the signature (token is user.hard.seen.idle.epoch.sig).
        parts = normal.split(".")
        self.assertEqual(len(parts), 6)
        parts[1] = str(int(parts[1]) + 3600)   # push the absolute expiry out an hour
        self.assertIsNone(auth.verify_session(".".join(parts), cfg=self.cfg))

    def test_trusted_admin_session_is_24h_and_has_no_idle_logout(self):
        uid = auth.create_user(self.store, None, "root", "supersecret")
        t0 = time.time()
        tok = auth.issue_session(uid, self.cfg, trusted_admin=True, now=t0)
        # Outlives the normal 12h absolute cap...
        self.assertEqual(
            auth.verify_session(tok, cfg=self.cfg, now=t0 + 13 * 3600), uid)
        # ...but dies past its own (hours-long, NOT the 30-day worker) window.
        cap = self.cfg.session_trusted_admin_hours * 3600
        self.assertIsNone(
            auth.verify_session(tok, cfg=self.cfg, now=t0 + cap + 5))
        # Shorter than the worker "remember" tier.
        self.assertLess(cap, self.cfg.session_remember_days * 86400)
        # No idle logout for the window: it never slides and survives a long gap.
        self.assertIsNone(auth.slide_session(tok, self.cfg, now=t0 + 10_000))
        self.assertEqual(
            auth.verify_session(tok, cfg=self.cfg, now=t0 + cap - 5), uid)

    def test_idle_session_expires_when_untouched(self):
        uid = auth.create_user(self.store, None, "root", "supersecret")
        t0 = time.time()
        tok = auth.issue_session(uid, self.cfg, now=t0)   # non-remember: idle applies
        idle_s = self.cfg.session_idle_minutes * 60
        self.assertEqual(
            auth.verify_session(tok, cfg=self.cfg, now=t0 + idle_s - 5), uid)
        # Past the idle window but far inside the absolute cap: still dead.
        self.assertIsNone(
            auth.verify_session(tok, cfg=self.cfg, now=t0 + idle_s + 5))
        # A slide on activity re-arms the idle clock from that moment.
        slid = auth.slide_session(tok, self.cfg, now=t0 + idle_s - 5)
        self.assertIsNotNone(slid)
        fresh, _ = slid
        self.assertEqual(
            auth.verify_session(fresh, cfg=self.cfg, now=t0 + 2 * idle_s - 15), uid)
        # A remember-me session has no idle window and never slides.
        trusted = auth.issue_session(uid, self.cfg, remember=True, now=t0)
        self.assertIsNone(auth.slide_session(trusted, self.cfg, now=t0 + 10_000))

    def test_new_login_supersedes_prior_session_via_epoch(self):
        uid = auth.create_user(self.store, None, "root", "supersecret")
        epoch = self.store.bump_session_epoch(uid)
        tok = auth.issue_session(uid, self.cfg, epoch=epoch)
        self.assertIsNotNone(auth.resolve_session(self.store, tok, cfg=self.cfg))
        self.store.bump_session_epoch(uid)   # a newer login (or a logout)
        self.assertIsNone(auth.resolve_session(self.store, tok, cfg=self.cfg))

    def test_scrypt_hash_roundtrip(self):
        h = auth.hash_password("correcthorse")
        self.assertTrue(h.startswith("scrypt$"))
        ok, upgrade = auth.verify_password("correcthorse", h, "")
        self.assertTrue(ok)
        self.assertFalse(upgrade)
        self.assertFalse(auth.verify_password("wrong", h, "")[0])

    def test_legacy_sha256_password_is_upgraded_on_login(self):
        # A pre-migration account: write a legacy salted-SHA-256 hash directly,
        # bypassing create_user (which now writes scrypt).
        salt = "0123456789abcdef"
        legacy = auth.hash_pw("correcthorse", salt)
        self.assertEqual(len(legacy), 64)   # bare sha256 hex, no "scrypt$"
        self.store.add_user(None, "legacyroot", legacy, salt, "owner")
        self.assertIsNotNone(
            auth.verify_login(self.store, "legacyroot", "correcthorse"))
        # The stored hash is now scrypt — upgraded in place on that login.
        row = self.store.get_user_by_username("legacyroot")
        self.assertTrue(row["pw_hash"].startswith("scrypt$"))
        self.assertIsNotNone(
            auth.verify_login(self.store, "legacyroot", "correcthorse"))
        self.assertIsNone(auth.verify_login(self.store, "legacyroot", "wrong"))

    def test_login_throttle_keys_independent_and_decay(self):
        th = auth.LoginThrottle(lock_after=3, base_delay=2.0, cap=300.0, window=900.0)
        t = 1000.0
        for _ in range(3):
            th.fail("ip:1.1.1.1", now=t)
        self.assertGreater(th.retry_after("ip:1.1.1.1", now=t), 0.0)
        self.assertEqual(th.retry_after("user:alice", now=t), 0.0)   # untouched key
        th.reset("ip:1.1.1.1")                                       # a good login
        self.assertEqual(th.retry_after("ip:1.1.1.1", now=t), 0.0)
        for _ in range(3):
            th.fail("ip:2.2.2.2", now=t)
        self.assertGreater(th.retry_after("ip:2.2.2.2", now=t), 0.0)
        self.assertEqual(th.retry_after("ip:2.2.2.2", now=t + 901), 0.0)  # decayed

class CentralAuthHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "c.db",
                          central_bind="127.0.0.1", central_port=0, central_token="tok")
        self.store = CentralStore(self.cfg.central_db)
        auth.create_user(self.store, None, "root", "rootpassword")
        auth.create_user(self.store, "ispA", "owner", "ownerpassword", "owner")
        auth.create_user(self.store, "ispA", "wrk", "workerpassword", "worker")
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

    def _req(self, method, path, body=None, cookie=None, token=None,
             extra_headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body); headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if extra_headers:
            headers.update(extra_headers)
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

    def test_trust_this_device_maps_to_the_right_tier_per_role(self):
        # "Trust this device" is one checkbox, three outcomes decided server-side.
        # An owner is refused the worker 30-day tier but DOES get the shorter
        # trusted-admin cap (24h) — no idle logout for the window.
        admin = f"Max-Age={self.cfg.session_trusted_admin_hours * 3600}"
        worker = f"Max-Age={self.cfg.session_remember_days * 86400}"
        _, _, setcookie = self._req("POST", "/api/login",
            {"username": "owner", "password": "ownerpassword", "remember": True})
        self.assertIn(admin, setcookie)
        # A superadmin (org_id IS NULL) rides the same trusted-admin tier.
        _, _, setcookie = self._req("POST", "/api/login",
            {"username": "root", "password": "rootpassword", "remember": True})
        self.assertIn(admin, setcookie)
        # A worker's remember stays the long-lived 30-day tier — role-scoped.
        _, _, setcookie = self._req("POST", "/api/login",
            {"username": "wrk", "password": "workerpassword", "remember": True})
        self.assertIn(worker, setcookie)
        # Without the box, everyone falls back to the short idle-capped session.
        _, _, setcookie = self._req("POST", "/api/login",
            {"username": "owner", "password": "ownerpassword"})
        self.assertIn(f"Max-Age={self.cfg.session_timeout_h * 3600}", setcookie)

    def test_password_change_invalidates_the_old_cookie(self):
        _, a = self._login("owner", "ownerpassword")
        self.assertEqual(self._req("GET", "/api/me", cookie=a)[0], 200)
        s, _, setcookie = self._req("POST", "/api/users/password",
            {"current_password": "ownerpassword", "new_password": "newpassword1"}, cookie=a)
        self.assertEqual(s, 200)
        fresh = setcookie.split(";")[0]   # re-issued so this tab stays signed in
        self.assertEqual(self._req("GET", "/api/me", cookie=a)[0], 401)     # old cookie dead
        self.assertEqual(self._req("GET", "/api/me", cookie=fresh)[0], 200)  # re-issued works

    def test_bad_login_401_and_me_requires_session(self):
        self.assertEqual(self._login("owner", "nope")[0], 401)
        self.assertEqual(self._req("GET", "/api/me")[0], 401)

    def test_login_throttle(self):
        for _ in range(5):
            self._login("owner", "wrong")
        self.assertEqual(self._login("owner", "wrong")[0], 429)

    def test_throttle_keys_off_the_forwarded_client_ip(self):
        # Distinct usernames so the per-ACCOUNT key never accumulates — this
        # isolates the per-IP bucket, which must key off X-Forwarded-For (every
        # socket peer here is 127.0.0.1, so a broken impl buckets the world).
        for i in range(5):
            self._req("POST", "/api/login",
                      {"username": f"ghost{i}", "password": "nope"},
                      extra_headers={"X-Forwarded-For": "203.0.113.7"})
        self.assertEqual(self._req("POST", "/api/login",
            {"username": "ghostX", "password": "nope"},
            extra_headers={"X-Forwarded-For": "203.0.113.7"})[0], 429)
        # A different forwarded IP has its own bucket → 401, not 429.
        self.assertEqual(self._req("POST", "/api/login",
            {"username": "ghostX", "password": "nope"},
            extra_headers={"X-Forwarded-For": "198.51.100.4"})[0], 401)

    def test_second_login_kills_the_first_session(self):
        _, first = self._login("owner", "ownerpassword")
        self.assertEqual(self._req("GET", "/api/me", cookie=first)[0], 200)
        _, second = self._login("owner", "ownerpassword")
        self.assertEqual(self._req("GET", "/api/me", cookie=first)[0], 401)   # superseded
        self.assertEqual(self._req("GET", "/api/me", cookie=second)[0], 200)
        self._req("POST", "/api/logout", cookie=second)
        self.assertEqual(self._req("GET", "/api/me", cookie=second)[0], 401)  # killed

    def test_session_cookie_is_secure_and_httponly(self):
        _, _, setcookie = self._req("POST", "/api/login",
            {"username": "owner", "password": "ownerpassword"})
        self.assertIn("Secure", setcookie)
        self.assertIn("HttpOnly", setcookie)
        self.assertIn("SameSite=Lax", setcookie)

    # --- TOTP second factor -------------------------------------------------

    def _totp_code(self, secret, now=None):
        step = int((time.time() if now is None else now) // 30)
        return totp._hotp(totp._decode_secret(secret), step)

    def _reset_totp_cursor(self, username):
        # Simulate the clock advancing to a fresh code window without waiting 30s
        # (the confirm code claims its step; login with the same step would be a
        # replay). Reaching into the store is the deterministic stand-in.
        uid = self.store.get_user_by_username(username)["id"]
        with self.store._connect() as conn:
            conn.execute("UPDATE users SET totp_last_step=NULL WHERE id=?", (uid,))
            conn.commit()

    def _enroll_totp(self, username="owner", password="ownerpassword"):
        _, cookie = self._login(username, password)
        _, body, _ = self._req("POST", "/api/users/totp/start", {}, cookie=cookie)
        secret = body["secret"]
        _, body, _ = self._req("POST", "/api/users/totp/confirm",
            {"password": password, "code": self._totp_code(secret)}, cookie=cookie)
        return cookie, secret, body["recovery_codes"]

    def test_totp_enrollment_then_login_requires_the_code(self):
        _, cookie = self._login("owner", "ownerpassword")
        s, body, _ = self._req("POST", "/api/users/totp/start", {}, cookie=cookie)
        self.assertEqual(s, 200)
        secret = body["secret"]
        self.assertTrue(body["otpauth_uri"].startswith("otpauth://totp/"))
        # Confirm is gated on the password AND a correct code.
        self.assertEqual(self._req("POST", "/api/users/totp/confirm",
            {"password": "wrong", "code": self._totp_code(secret)}, cookie=cookie)[0], 422)
        self.assertEqual(self._req("POST", "/api/users/totp/confirm",
            {"password": "ownerpassword", "code": "000000"}, cookie=cookie)[0], 422)
        code = self._totp_code(secret)
        s, body, _ = self._req("POST", "/api/users/totp/confirm",
            {"password": "ownerpassword", "code": code}, cookie=cookie)
        self.assertEqual(s, 200)
        self.assertEqual(len(body["recovery_codes"]), 10)
        _, me, _ = self._req("GET", "/api/me", cookie=cookie)
        self.assertTrue(me["user"]["totp_enabled"])
        # Password alone no longer logs in.
        s, b2 = self._login("owner", "ownerpassword")
        self.assertEqual(s, 401)
        # The confirm code can't be replayed at login.
        self.assertEqual(self._req("POST", "/api/login",
            {"username": "owner", "password": "ownerpassword", "totp": code})[0], 401)
        # A code from the next window logs in.
        self._reset_totp_cursor("owner")
        s, _, setcookie = self._req("POST", "/api/login",
            {"username": "owner", "password": "ownerpassword",
             "totp": self._totp_code(secret)})
        self.assertEqual(s, 200)
        self.assertIn("wisp_central_session=", setcookie)

    def test_totp_required_flag_is_returned_without_burning_the_throttle(self):
        self._enroll_totp()
        # Six password-only attempts (each 'totp_required') must NOT throttle —
        # the password was right, the client is just being asked for the code.
        # (A wrong-password attempt would 429 by the 6th; these must not.)
        for _ in range(5):
            self._req("POST", "/api/login",
                      {"username": "owner", "password": "ownerpassword"})
        s, body, _ = self._req("POST", "/api/login",
            {"username": "owner", "password": "ownerpassword"})
        self.assertEqual(s, 401)
        self.assertTrue(body.get("totp_required"))

    def test_totp_recovery_code_is_single_use(self):
        _, _, recovery = self._enroll_totp()
        rc = recovery[0]
        s, _, setcookie = self._req("POST", "/api/login",
            {"username": "owner", "password": "ownerpassword", "recovery": rc})
        self.assertEqual(s, 200)
        self.assertIn("wisp_central_session=", setcookie)
        # Same code a second time is refused.
        self.assertEqual(self._req("POST", "/api/login",
            {"username": "owner", "password": "ownerpassword", "recovery": rc})[0], 401)

    def test_totp_disable_restores_password_only_login(self):
        cookie, _, _ = self._enroll_totp()
        self.assertEqual(self._req("POST", "/api/users/totp/disable",
            {"password": "wrong"}, cookie=cookie)[0], 422)
        self.assertEqual(self._req("POST", "/api/users/totp/disable",
            {"password": "ownerpassword"}, cookie=cookie)[0], 200)
        self.assertEqual(self._login("owner", "ownerpassword")[0], 200)

    def test_worker_cannot_enroll_totp(self):
        _, wcookie = self._login("wrk", "workerpassword")
        self.assertEqual(
            self._req("POST", "/api/users/totp/start", {}, cookie=wcookie)[0], 403)

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
        wrk_id = self.store.get_user_by_username("wrk")["id"]
        status, _, _ = self._req("POST", "/api/users/password",
            {"id": wrk_id, "new_password": "resetpassword1"}, cookie=own)
        self.assertEqual(status, 200)
        self.assertEqual(self._login("wrk", "resetpassword1")[0], 200)

    def test_worker_cannot_reset_teammate_password(self):
        # /api/users/password is ON _WORKER_ROUTES (a worker changes its OWN
        # password), so the handler's owner-only check is what must refuse this
        # — the route whitelist never sees it.
        _, op = self._login("wrk", "workerpassword")
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
        _, op = self._login("owner", "ownerpassword")
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

    def test_inventory_worker_cannot_write_owner_can(self):
        _, op = self._login("wrk", "workerpassword")
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
            {"ntfy_topic_owner": "a-owner", "ntfy_topic_worker": "a-op"}, cookie=own)
        self.assertEqual(status, 200)
        status, body, _ = self._req("GET", "/api/orgs", cookie=own)
        org = body["orgs"][0]
        self.assertEqual(org["ntfy_topic_owner"], "a-owner")
        self.assertEqual(org["ntfy_topic_worker"], "a-op")

    def test_test_alert_sends_via_injected_notifier(self):
        _, own = self._login("owner", "ownerpassword")
        # WhatsApp is the only channel now: the test pages the org audience, so
        # give the owner account a number.
        me_id = self.store.get_user_by_username("owner")["id"]
        self.store.set_user_whatsapp(me_id, "919000000001")
        status, body, _ = self._req("POST", "/api/test-alert", {}, cookie=own)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0]["whatsapp"], ["919000000001"])

    def test_test_alert_requires_a_whatsapp_recipient(self):
        # no numbers on any account (and no admin number) ⇒ nothing to page ⇒ 422
        _, own = self._login("owner", "ownerpassword")
        status, body, _ = self._req("POST", "/api/test-alert", {}, cookie=own)
        self.assertEqual(status, 422)
        self.assertEqual(len(self.notifier.sent), 0)

    def test_test_alert_worker_cannot_send(self):
        _, own = self._login("owner", "ownerpassword")
        self._req("POST", "/api/org", {"ntfy_topic_owner": "a-owner-topic"}, cookie=own)
        _, op = self._login("wrk", "workerpassword")
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

    def test_worker_cannot_write_backup_links(self):
        _, own = self._login("owner", "ownerpassword")
        a = self.store.create_org_device("ispA", {
            "name": "A", "ip_address": "10.0.3.1", "device_type": None,
            "region": None, "parent_device_id": None})
        b = self.store.create_org_device("ispA", {
            "name": "B", "ip_address": "10.0.3.2", "device_type": None,
            "region": None, "parent_device_id": None})
        _, op = self._login("wrk", "workerpassword")
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

        _, own = self._login("owner", "ownerpassword")
        status, body, _ = self._req("GET", "/api/outages", cookie=own)
        self.assertEqual(status, 200)
        self.assertEqual(body["outages"][0]["status"], "unassigned")
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

    def test_worker_can_acknowledge_and_postmortem(self):
        dev = self.store.create_org_device("ispA", {
            "name": "Tower", "ip_address": "10.0.5.3", "device_type": None,
            "region": None, "parent_device_id": None, "assigned_node_id": "edge-a"})
        self.store.open_outage_if_absent("ispA", dev, "2026-06-23T08:00:00+00:00", "DOWN")
        oid = self.store.open_outage_id("ispA", dev)
        _, wrk = self._login("wrk", "workerpassword")
        status, body, _ = self._req("GET", "/api/outages", cookie=wrk)
        self.assertEqual(status, 200)
        self.assertEqual(body["outages"][0]["id"], oid)
        status, body, _ = self._req("POST", "/api/outages/acknowledge",
                                    {"outage_id": oid}, cookie=wrk)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        _, body, _ = self._req("GET", "/api/outages", cookie=wrk)
        self.assertEqual(body["outages"][0]["acknowledged_by"], "wrk")
        self.store.resolve_outage("ispA", dev, "2026-06-23T08:10:00+00:00")
        status, body, _ = self._req(
            "POST", "/api/outages/postmortem",
            {"outage_id": oid, "root_cause": "fiber cut", "resolution_notes": "spliced"},
            cookie=wrk)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_worker_cannot_touch_another_orgs_outage(self):
        dev = self.store.create_org_device("ispB", {
            "name": "Relay", "ip_address": "10.0.5.4", "device_type": None,
            "region": None, "parent_device_id": None})
        self.store.open_outage_if_absent("ispB", dev, "2026-06-23T08:00:00+00:00", "DOWN")
        oid = self.store.open_outage_id("ispB", dev)
        _, wrk = self._login("wrk", "workerpassword")
        status, _, _ = self._req("POST", "/api/outages/acknowledge",
                                 {"outage_id": oid}, cookie=wrk)
        self.assertEqual(status, 403)

    def test_worker_reads_the_monitoring_surface_but_writes_nothing(self):
        # A worker now gets the FULL dashboard, read-only (2026-07-23): every
        # monitoring GET on _WORKER_GET is allowed, every write and every
        # sensitive read stays a 403. Deny-by-default is preserved — a NEW route
        # is worker-blocked until it is placed in _WORKER_GET/_WORKER_POST.
        _, wrk = self._login("wrk", "workerpassword")
        self.assertEqual(self._req("GET", "/api/me", cookie=wrk)[0], 200)
        # reads the shell renders: allowed
        for path in ("/api/inventory", "/api/logs", "/api/nodes", "/api/orgs",
                     "/api/summary", "/api/regions", "/api/analytics"):
            status, _, _ = self._req("GET", path, cookie=wrk)
            self.assertEqual(status, 200, f"GET {path} should be worker-readable")
        # sensitive reads: still owner/superadmin-only
        for path in ("/api/inventory/credentials?device_id=1", "/api/admin/overview",
                     "/api/admin/settings", "/api/system", "/api/users",
                     "/api/snmp-profiles", "/api/proxy/sessions"):
            status, _, _ = self._req("GET", path, cookie=wrk)
            self.assertEqual(status, 403, f"GET {path} should be worker-blocked")
        # writes: only triage + own-password + the "I've paid" ping; everything
        # else 403s even on a path whose GET the worker may read
        for method, path, body in [
            ("POST", "/api/inventory", {"name": "X", "ip_address": "10.0.0.7"}),
            ("POST", "/api/inventory/update", {"id": 1, "name": "X"}),
            ("POST", "/api/users", {"username": "x2", "password": "longenough1"}),
            ("POST", "/api/regions", {"name": "R"}),
            ("POST", "/api/nodes", {"node_id": "edge-z"}),
            ("POST", "/api/inventory/credentials", {"device_id": 1}),
        ]:
            status, _, _ = self._req(method, path, body, cookie=wrk)
            self.assertEqual(status, 403, f"{method} {path} should be worker-blocked")

    def test_worker_orgs_row_hides_paging_topics(self):
        # The org row a worker reads carries the name and Maps key it needs, but
        # NOT the ntfy paging topics — those are a capability (subscribe to every
        # page / POST a spoofed one), so they stay owner/superadmin-only.
        _, wrk = self._login("wrk", "workerpassword")
        status, body, _ = self._req("GET", "/api/orgs", cookie=wrk)
        self.assertEqual(status, 200)
        row = body["orgs"][0]
        for k in ("ntfy_topic", "ntfy_topic_owner", "ntfy_topic_worker"):
            self.assertNotIn(k, row, f"{k} leaked to a worker")
        # an owner still sees them
        _, own = self._login("owner", "ownerpassword")
        _, obody, _ = self._req("GET", "/api/orgs", cookie=own)
        self.assertIn("ntfy_topic_owner", obody["orgs"][0])

    def test_superadmin_provisioned_bare_is_not_a_worker(self):
        # `admin create-user` with no --role must never leave the platform admin
        # on the ORG default ('worker' since the 2026-07-21 collapse) — the SPA's
        # require-auth serves any worker the stripped field view.
        auth.create_user(self.store, None, "root3", "root3password")
        self.assertEqual(self.store.get_user_by_username("root3")["role"], "owner")

    def test_superadmin_is_never_worker_blocked(self):
        # Defense in depth for a DB an EARLIER build already damaged: force the
        # hostile row directly, since create_user can no longer produce one.
        # A superadmin is org_id IS NULL and its role column is meaningless, so
        # _worker_blocked must gate on identity before role.
        auth.create_user(self.store, None, "root2", "root2password")
        with self.store._connect() as conn:
            conn.execute("UPDATE users SET role='worker' WHERE username='root2'")
            conn.commit()
        self.assertEqual(self.store.get_user_by_username("root2")["role"], "worker")
        _, root2 = self._login("root2", "root2password")
        self.assertEqual(self._req("GET", "/api/orgs", cookie=root2)[0], 200)
        # Never 403: an unscoped superadmin may still get a 400 asking which org
        # it means — that is the route answering, not the whitelist refusing.
        for path in ("/api/inventory", "/api/nodes", "/api/logs"):
            status, _, _ = self._req("GET", path, cookie=root2)
            self.assertNotEqual(status, 403, f"superadmin worker-blocked on {path}")

    def test_worker_can_change_own_password(self):
        _, wrk = self._login("wrk", "workerpassword")
        status, _, _ = self._req("POST", "/api/users/password",
            {"current_password": "workerpassword", "new_password": "newerpassword1"},
            cookie=wrk)
        self.assertEqual(status, 200)
        self.assertEqual(self._login("wrk", "newerpassword1")[0], 200)

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
