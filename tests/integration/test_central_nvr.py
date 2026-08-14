"""CCTV NVR: eligibility, the digest sweep, batched camera paging, the API.

The paging rules matter most: a sweep that finds several cameras newly dark
sends ONE page per NVR (the DBC-storm lesson applied to CCTV — an evening
power cut darkens cameras in bulk), a failed read pages nobody, and unknown
states page in neither direction.
"""
from __future__ import annotations

import base64
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

from wisp.central import auth  # noqa: E402
from wisp.central.server import make_server  # noqa: E402
from wisp.central.store import CentralStore  # noqa: E402
from wisp.central.weboptics_sweep import WebOpticsSweeper  # noqa: E402
from wisp.config import Config  # noqa: E402
from wisp.egress.notifiers import NotifyResult  # noqa: E402

ORG = "ispA"

CHANNELS_PATH = "/cgi-bin/configManager.cgi?action=getConfig&name=RemoteDevice"
NAMES_PATH = "/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle"
RPC_REALM = "Login to a1b2c3"
RPC_RANDOM = "1877777777"


def channels_page(cams):
    out = []
    for no, ip, enabled in cams:
        out.append(f"table.RemoteDevice[{no}].Address={ip}")
        out.append(f"table.RemoteDevice[{no}].Port=37777")
        out.append(f"table.RemoteDevice[{no}].Enable="
                   f"{'true' if enabled else 'false'}")
    return "\n".join(out)


def names_page(names):
    return "\n".join(f"table.ChannelTitle[{no}].Name={name}"
                     for no, name in names.items())


class DigestHub:
    """A CP-UNR-shaped fake: digest on /cgi-bin, RPC2 for camera states."""

    def __init__(self, pages: dict[str, str], states=None, refuse_login=False,
                 dead=False) -> None:
        self.pages = pages
        self.states = states
        self.refuse_login = refuse_login
        self.dead = dead
        self.asked: list[str] = []
        self.authed: list[str] = []
        self.browsing = False

    def polled_recently(self, org, node, hold):
        return True

    def active_sessions_for(self, org, node, idle_s=None):
        return ["sid"] if self.browsing else []

    def reap_expired(self):
        return []

    def _json(self, out) -> dict:
        return {"status": 200, "headers": [],
                "body_b64": base64.b64encode(json.dumps(out).encode()).decode()}

    def _rpc2(self, body):
        import hashlib
        if self.states is None:
            return {"status": 400, "headers": [],
                    "body_b64": base64.b64encode(b"Error").decode()}
        req = json.loads(body or b"{}")
        req_method = req.get("method")
        if req_method == "global.login":
            params = req.get("params") or {}
            if not params.get("password"):
                return self._json({
                    "result": False, "session": "S1",
                    "error": {"code": 268632079, "message": "challenge"},
                    "params": {"random": RPC_RANDOM, "realm": RPC_REALM,
                               "encryption": "Default"}})
            ha = hashlib.md5(
                f"admin:{RPC_REALM}:secret".encode()).hexdigest().upper()
            want = hashlib.md5(
                f"admin:{RPC_RANDOM}:{ha}".encode()).hexdigest().upper()
            ok = params.get("password") == want and not self.refuse_login
            out = {"result": ok, "session": "S1"}
            if not ok:
                out["error"] = {"code": 268632081, "message": "invalid password"}
            return self._json(out)
        if req_method == "LogicDeviceManager.getCameraState":
            return self._json({"result": True, "session": "S1", "params": {
                "states": [{"channel": no, "connectionState": word}
                           for no, word in self.states.items()]}})
        return self._json({"result": True, "session": "S1"})

    def _snapshot(self, path):
        try:
            ch = int(path.split("channel=")[1].split("&")[0])
        except (IndexError, ValueError):
            return {"status": 400, "headers": [], "body_b64": ""}
        word = (self.states or {}).get(ch - 1)
        if word == "Connected":
            raw = b"\xff\xd8\xff\xe0" + b"0" * 64
            return {"status": 200,
                    "headers": [("Content-Type", "image/jpeg")],
                    "body_b64": base64.b64encode(raw).decode()}
        return {"status": 400, "headers": [],
                "body_b64": base64.b64encode(b"Error").decode()}

    def submit(self, session, *, method, path, headers, body, timeout,
               extra=None):
        if extra and extra.get("kind") == "preflight":
            return None
        if self.dead:
            return {"error": "connect timeout to 10.0.0.9:80"}
        self.asked.append(f"{method} {path}")
        if path in ("/RPC2_Login", "/RPC2"):
            return self._rpc2(body)
        auth_hdr = (headers or {}).get("Authorization", "")
        if path.startswith("/cgi-bin/snapshot.cgi") and auth_hdr:
            return self._snapshot(path)
        if auth_hdr:
            self.authed.append(auth_hdr)
        if self.refuse_login or not auth_hdr:
            return {"status": 401, "headers": [
                ("WWW-Authenticate",
                 'Digest realm="LoginToNVR", qop="auth", nonce="abc123", '
                 'opaque="xyz"')], "body_b64": ""}
        if path not in self.pages:
            return {"status": 404, "headers": [], "body_b64": ""}
        raw = self.pages[path].encode()
        return {"status": 200, "headers": [],
                "body_b64": base64.b64encode(raw).decode()}


class RecordingNotifier:
    channel = "whatsapp"

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, title, body, priority=3, *, whatsapp=None, facts=None):
        self.sent.append({"title": title, "body": body,
                          "whatsapp": list(whatsapp or [])})
        return NotifyResult(True, "ok")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db",
                          central_bind="127.0.0.1", central_port=0)
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org(ORG, ntfy_topic_owner="own")
        self.store.set_org_web_proxy(ORG, True)

    def tearDown(self):
        self.tmp.cleanup()

    def _nvr(self, name="CCTV NVR", vendor="cpplus", node="edge-1", org=ORG):
        return self.store.create_org_device(org, {
            "name": name, "ip_address": "10.0.0.9", "device_type": "nvr",
            "region": None, "parent_device_id": None,
            "nvr_vendor": vendor, "assigned_node_id": node})

    def _creds(self, device_id, org=ORG):
        self.store.set_device_webui_credentials(
            org, device_id, username="admin", password_enc="enc:x",
            set_password=True, auth_mode="form", updated_by="t")

    def _sweeper(self, hub, notifier=None):
        class Box:
            def decrypt(self, enc):
                return "secret"
        return WebOpticsSweeper(self.store, hub, Box(), self.cfg, notifier)

    def _hub(self, cams, states=None, names=None, **kw):
        pages = {CHANNELS_PATH: channels_page(cams)}
        if names is not None:
            pages[NAMES_PATH] = names_page(names)
        return DigestHub(pages, states=states, **kw)


class NvrStoreTest(Base):

    def test_LAST_ONLINE_FREEZES_when_a_camera_goes_dark(self):
        did = self._nvr()
        row = {"channel_no": 0, "ip_address": "10.1.1.1", "enabled": True}
        self.store.upsert_nvr_channels(
            ORG, did, [dict(row, state="online")], "T1")
        self.store.upsert_nvr_channels(
            ORG, did, [dict(row, state="offline")], "T2")
        chan = self.store.list_nvr_channels(ORG, did)[0]
        self.assertEqual(chan["state"], "offline")
        self.assertEqual(chan["last_online_at"], "T1")
        self.assertEqual(chan["updated_at"], "T2")

    def test_a_channel_removed_from_the_nvr_is_pruned_on_a_complete_read(self):
        did = self._nvr()
        self.store.upsert_nvr_channels(ORG, did, [
            {"channel_no": 0, "ip_address": "10.1.1.1", "state": "online"},
            {"channel_no": 1, "ip_address": "10.1.1.2", "state": "online"},
        ], "T1")
        self.store.upsert_nvr_channels(ORG, did, [
            {"channel_no": 1, "ip_address": "10.1.1.2", "state": "online"},
        ], "T2", prune=True)
        self.assertEqual(
            [c["channel_no"] for c in self.store.list_nvr_channels(ORG, did)],
            [1])

    def test_a_partial_read_prunes_nothing(self):
        did = self._nvr()
        self.store.upsert_nvr_channels(ORG, did, [
            {"channel_no": 0, "ip_address": "10.1.1.1", "state": "online"},
        ], "T1")
        self.store.upsert_nvr_channels(ORG, did, [], "T2", prune=False)
        self.assertEqual(len(self.store.list_nvr_channels(ORG, did)), 1)

    def test_one_orgs_cameras_are_invisible_to_another(self):
        self.store.set_org("ispB", ntfy_topic_owner="own")
        did = self._nvr()
        self.store.upsert_nvr_channels(ORG, did, [
            {"channel_no": 0, "ip_address": "10.1.1.1", "state": "online"},
        ], "T1")
        self.assertEqual(self.store.list_nvr_channels("ispB", did), [])

    def test_deleting_the_nvr_takes_channels_and_status_with_it(self):
        did = self._nvr()
        self.store.upsert_nvr_channels(ORG, did, [
            {"channel_no": 0, "ip_address": "10.1.1.1", "state": "online"},
        ], "T1")
        self.store.set_nvr_status(ORG, did, "cpplus", "ok", None, 1)
        self.assertTrue(self.store.delete_org_device(ORG, did)["ok"])
        self.assertEqual(self.store.list_nvr_channels(ORG, did), [])
        self.assertIsNone(self.store.get_nvr_status(ORG, did))

    def test_the_camera_counts_ride_the_device_list(self):
        did = self._nvr()
        self.store.upsert_nvr_channels(ORG, did, [
            {"channel_no": 0, "ip_address": "10.1.1.1", "enabled": True,
             "state": "online"},
            {"channel_no": 1, "ip_address": "10.1.1.2", "enabled": True,
             "state": "offline"},
            {"channel_no": 2, "ip_address": "10.1.1.3", "enabled": False,
             "state": "offline"},
        ], "T1")
        dev = next(d for d in self.store.list_org_devices(ORG)
                   if d["id"] == did)
        self.assertEqual(dev["cameras_total"], 3)
        self.assertEqual(dev["cameras_down"], 1)
        self.assertEqual(dev["cameras_updated_at"], "T1")


class NvrTargetTest(Base):

    def _ids(self):
        return [t["id"] for t in self.store.nvr_targets(("cpplus",))]

    def test_a_configured_nvr_is_a_target(self):
        did = self._nvr()
        self._creds(did)
        self.assertEqual(self._ids(), [did])

    def test_no_stored_login_is_not_a_target(self):
        self._nvr()
        self.assertEqual(self._ids(), [])

    def test_a_brand_nobody_declared_is_not_a_target(self):
        did = self.store.create_org_device(ORG, {
            "name": "N", "ip_address": "10.0.0.9", "device_type": "nvr",
            "region": None, "parent_device_id": None,
            "assigned_node_id": "edge-1"})
        self._creds(did)
        self.assertEqual(self._ids(), [])

    def test_an_olt_is_never_an_nvr_target(self):
        did = self.store.create_org_device(ORG, {
            "name": "OLT", "ip_address": "10.0.0.8", "device_type": "OLT",
            "region": None, "parent_device_id": None,
            "gpon_vendor": "dbc", "assigned_node_id": "edge-1"})
        self._creds(did)
        self.assertEqual(self._ids(), [])

    def test_an_org_without_the_web_proxy_grant_is_not_a_target(self):
        self.store.set_org_web_proxy(ORG, False)
        did = self._nvr()
        self._creds(did)
        self.assertEqual(self._ids(), [])


class NvrSweepTest(Base):

    def _run(self, hub, notifier=None):
        did = self._nvr()
        self._creds(did)
        self._sweeper(hub, notifier).sweep_nvrs()
        return did

    def test_the_sweep_answers_a_digest_challenge_and_stores_channels(self):
        hub = self._hub(
            cams=[(0, "10.1.1.1", True), (3, "10.1.1.4", True)],
            states={0: "Connected", 3: "Unconnected"},
            names={0: "MAIN GATE"})
        did = self._run(hub)
        self.assertTrue(hub.authed)
        self.assertIn("username=\"admin\"", hub.authed[0])
        chans = self.store.list_nvr_channels(ORG, did)
        self.assertEqual([c["channel_no"] for c in chans], [0, 3])
        self.assertEqual(chans[0]["name"], "MAIN GATE")
        self.assertEqual(chans[0]["state"], "online")
        self.assertEqual(chans[1]["state"], "offline")
        status = self.store.get_nvr_status(ORG, did)
        self.assertEqual(status["state"], "ok")
        self.assertEqual(status["channels"], 2)

    def test_a_refused_password_reads_as_login_and_stores_nothing(self):
        hub = self._hub(cams=[(0, "10.1.1.1", True)], refuse_login=True)
        did = self._run(hub)
        status = self.store.get_nvr_status(ORG, did)
        self.assertEqual(status["state"], "login")
        self.assertEqual(self.store.list_nvr_channels(ORG, did), [])

    def test_THE_CREDENTIAL_IS_NOT_SENT_when_the_device_does_not_answer(self):
        hub = self._hub(cams=[(0, "10.1.1.1", True)], dead=True)
        did = self._run(hub)
        self.assertEqual(hub.authed, [])
        status = self.store.get_nvr_status(ORG, did)
        self.assertEqual(status["state"], "unreachable")

    def test_a_missing_state_page_reads_unknown_and_partial(self):
        hub = self._hub(cams=[(0, "10.1.1.1", True)], states=None)
        did = self._run(hub)
        chan = self.store.list_nvr_channels(ORG, did)[0]
        self.assertEqual(chan["state"], "unknown")
        self.assertEqual(self.store.get_nvr_status(ORG, did)["state"],
                         "partial")

    def test_a_browsed_probe_is_skipped_and_says_why(self):
        hub = self._hub(cams=[(0, "10.1.1.1", True)])
        hub.browsing = True
        did = self._run(hub)
        status = self.store.get_nvr_status(ORG, did)
        self.assertEqual(status["state"], "skipped")
        self.assertEqual(hub.asked, [])

    def test_an_org_tombstone_on_the_recipe_records_no_profile(self):
        with self.store._connect() as conn:
            conn.execute(
                "INSERT INTO nvr_profiles (org_id, name, spec, enabled,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (ORG, "cpplus", "{}", 0, "T", "T"))
            conn.commit()
        hub = self._hub(cams=[(0, "10.1.1.1", True)])
        did = self._run(hub)
        status = self.store.get_nvr_status(ORG, did)
        self.assertEqual(status["state"], "no_profile")
        self.assertEqual(hub.asked, [])


class CameraPagingTest(Base):

    def setUp(self):
        super().setUp()
        uid = auth.create_user(self.store, ORG, "owner", "ownerpassword",
                               "owner")
        self.store.set_user_whatsapp(uid, "919000000001")

    def _cams(self, states):
        return self._hub(
            cams=[(no, f"10.1.1.{no + 1}", True) for no in states],
            states={no: w for no, w in states.items()})

    def test_first_sight_of_the_fleet_pages_nothing(self):
        notifier = RecordingNotifier()
        did = self._nvr()
        self._creds(did)
        sweeper = self._sweeper(
            self._cams({0: "Unconnected", 1: "Connected"}), notifier)
        sweeper.sweep_nvrs()
        self.assertEqual(notifier.sent, [])

    def test_TWO_CAMERAS_DARK_IN_ONE_SWEEP_IS_ONE_PAGE_not_two(self):
        notifier = RecordingNotifier()
        did = self._nvr()
        self._creds(did)
        sweeper = self._sweeper(
            self._cams({0: "Connected", 1: "Connected", 2: "Connected"}),
            notifier)
        sweeper.sweep_nvrs()
        self.assertEqual(notifier.sent, [])
        sweeper = self._sweeper(
            self._cams({0: "Unconnected", 1: "Unconnected", 2: "Connected"}),
            notifier)
        sweeper.sweep_nvrs()
        self.assertEqual(len(notifier.sent), 1)
        self.assertIn("2 cameras dark", notifier.sent[0]["title"])
        self.assertIn("CH1", notifier.sent[0]["body"])
        self.assertIn("CH2", notifier.sent[0]["body"])
        self.assertEqual(notifier.sent[0]["whatsapp"], ["919000000001"])

    def test_a_restored_camera_pages_once(self):
        notifier = RecordingNotifier()
        did = self._nvr()
        self._creds(did)
        self._sweeper(self._cams({0: "Unconnected"}), notifier).sweep_nvrs()
        self._sweeper(self._cams({0: "Connected"}), notifier).sweep_nvrs()
        self.assertEqual(len(notifier.sent), 1)
        self.assertIn("camera back", notifier.sent[0]["title"])

    def test_an_unchanged_state_never_repages(self):
        notifier = RecordingNotifier()
        did = self._nvr()
        self._creds(did)
        for _ in range(3):
            self._sweeper(self._cams({0: "Unconnected"}),
                          notifier).sweep_nvrs()
        self.assertEqual(notifier.sent, [])

    def test_an_UNKNOWN_state_pages_in_neither_direction(self):
        notifier = RecordingNotifier()
        did = self._nvr()
        self._creds(did)
        self._sweeper(self._cams({0: "Connected"}), notifier).sweep_nvrs()
        no_state = self._hub(cams=[(0, "10.1.1.1", True)], states=None)
        self._sweeper(no_state, notifier).sweep_nvrs()
        self._sweeper(self._cams({0: "Connected"}), notifier).sweep_nvrs()
        self.assertEqual(notifier.sent, [])

    def test_an_UNWATCHED_camera_goes_dark_without_a_page(self):
        notifier = RecordingNotifier()
        did = self._nvr()
        self._creds(did)
        sweeper = self._sweeper(
            self._cams({0: "Connected", 1: "Connected"}), notifier)
        sweeper.sweep_nvrs()
        self.assertTrue(self.store.set_nvr_channel_watch(ORG, did, 0, False))
        sweeper = self._sweeper(
            self._cams({0: "Unconnected", 1: "Unconnected"}), notifier)
        sweeper.sweep_nvrs()
        self.assertEqual(len(notifier.sent), 1)
        self.assertIn("1 camera dark", notifier.sent[0]["title"])
        self.assertIn("CH2", notifier.sent[0]["body"])
        self.assertNotIn("CH1", notifier.sent[0]["body"])

    def test_the_toggle_survives_the_sweep(self):
        did = self._nvr()
        self._creds(did)
        sweeper = self._sweeper(self._cams({0: "Connected"}))
        sweeper.sweep_nvrs()
        self.store.set_nvr_channel_watch(ORG, did, 0, False)
        self._sweeper(self._cams({0: "Connected"})).sweep_nvrs()
        chan = self.store.list_nvr_channels(ORG, did)[0]
        self.assertFalse(chan["monitored"])

    def test_the_cams_down_count_is_WATCHED_dark_only(self):
        did = self._nvr()
        self.store.upsert_nvr_channels(ORG, did, [
            {"channel_no": 0, "ip_address": "10.1.1.1", "enabled": True,
             "state": "offline"},
            {"channel_no": 1, "ip_address": "10.1.1.2", "enabled": True,
             "state": "offline"},
        ], "T1")
        self.store.set_nvr_channel_watch(ORG, did, 0, False)
        dev = next(d for d in self.store.list_org_devices(ORG)
                   if d["id"] == did)
        self.assertEqual(dev["cameras_down"], 1)

    def test_a_failed_read_pages_nobody(self):
        notifier = RecordingNotifier()
        did = self._nvr()
        self._creds(did)
        self._sweeper(self._cams({0: "Connected"}), notifier).sweep_nvrs()
        hub = self._hub(cams=[(0, "10.1.1.1", True)], dead=True)
        self._sweeper(hub, notifier).sweep_nvrs()
        self.assertEqual(notifier.sent, [])

    def test_the_page_is_logged_under_its_kind(self):
        notifier = RecordingNotifier()
        did = self._nvr()
        self._creds(did)
        self._sweeper(self._cams({0: "Connected"}), notifier).sweep_nvrs()
        self._sweeper(self._cams({0: "Unconnected"}), notifier).sweep_nvrs()
        with self.store._connect() as conn:
            kinds = [r["kind"] for r in conn.execute(
                "SELECT kind FROM alert_log WHERE org_id=?", (ORG,))]
        self.assertIn("CAMERA_DOWN", kinds)


class SnapshotTest(Base):

    def test_an_online_camera_yields_a_frame_on_the_ONE_BASED_channel(self):
        did = self._nvr()
        self._creds(did)
        hub = self._hub(cams=[(3, "10.1.1.4", True)],
                        states={3: "Connected"})
        sweeper = self._sweeper(hub)
        frame, err, code = sweeper.snapshot(ORG, did, 3)
        self.assertEqual(code, 200, err)
        self.assertTrue(frame.startswith(b"\xff\xd8"))
        self.assertIn("GET /cgi-bin/snapshot.cgi?channel=4", hub.asked)

    def test_a_dark_channel_is_a_502_with_the_reason(self):
        did = self._nvr()
        self._creds(did)
        hub = self._hub(cams=[(0, "10.1.1.1", True)],
                        states={0: "Unconnect"})
        frame, err, code = self._sweeper(hub).snapshot(ORG, did, 0)
        self.assertIsNone(frame)
        self.assertEqual(code, 502)
        self.assertIn("dark or empty", err)

    def test_an_unconfigured_nvr_is_a_400_with_the_pointer(self):
        did = self._nvr()
        frame, err, code = self._sweeper(self._hub(cams=[])).snapshot(
            ORG, did, 0)
        self.assertEqual(code, 400)
        self.assertIn("Cameras tab", err)


class CameraIssuesTest(Base):
    """The tile's number and the /issues list must be the same number."""

    def _channels(self, did, states, watched=None):
        rows = [{"channel_no": no, "name": f"CAM{no + 1}",
                 "ip_address": f"10.1.1.{no + 1}", "enabled": True,
                 "state": st} for no, st in states.items()]
        self.store.upsert_nvr_channels(ORG, did, rows, "T1")
        for no in (watched or {}):
            self.store.set_nvr_channel_watch(ORG, did, no, watched[no])

    def _collect(self):
        from wisp.central import issues
        return issues.collect(self.store, self.cfg, ORG,
                              kinds=["camera_down"])

    def test_a_dark_watched_camera_is_one_issue_row_and_counts_agree(self):
        did = self._nvr()
        self._channels(did, {0: "offline", 1: "online"})
        rows = self._collect()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "camera_down")
        self.assertEqual(rows[0]["severity"], "critical")
        self.assertIn("CH1", rows[0]["subject"])
        self.assertIn("10.1.1.1", rows[0]["detail"])
        dev = next(d for d in self.store.list_org_devices(ORG)
                   if d["id"] == did)
        self.assertEqual(dev["cameras_down"], len(rows))

    def test_an_unwatched_dark_camera_is_not_an_issue_on_EITHER_surface(self):
        did = self._nvr()
        self._channels(did, {0: "offline"}, watched={0: False})
        self.assertEqual(self._collect(), [])
        dev = next(d for d in self.store.list_org_devices(ORG)
                   if d["id"] == did)
        self.assertEqual(dev["cameras_down"], 0)

    def test_behind_a_down_nvr_the_row_is_KEPT_but_demoted(self):
        from datetime import datetime, timezone
        did = self._nvr()
        self._channels(did, {0: "offline"})
        self.store.write_device_states(
            ORG, [(did, "DOWN", None, 100.0, None)],
            datetime.now(timezone.utc).isoformat())
        rows = self._collect()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["severity"], "info")
        self.assertIn("frozen", rows[0]["detail"])

    def test_an_unknown_state_is_not_an_issue(self):
        did = self._nvr()
        self._channels(did, {0: "unknown"})
        self.assertEqual(self._collect(), [])


class NvrApiTest(Base):

    def setUp(self):
        super().setUp()
        auth.create_user(self.store, ORG, "owner", "ownerpassword", "owner")
        auth.create_user(self.store, ORG, "field", "fieldpassword", "worker")
        self.did = self._nvr()
        self.store.upsert_nvr_channels(ORG, self.did, [
            {"channel_no": 0, "name": "GATE", "ip_address": "10.1.1.1",
             "enabled": True, "state": "online"},
        ], "T1")
        self.store.set_nvr_status(ORG, self.did, "cpplus", "ok", None, 1)
        self.server = make_server(self.cfg, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        super().tearDown()

    def _cookie(self, username, password):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/login",
                     body=json.dumps({"username": username,
                                      "password": password}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        cookie = (resp.getheader("Set-Cookie") or "").split(";")[0]
        conn.close()
        return cookie

    def _get(self, cookie, device=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(
            "GET",
            f"/api/inventory/nvr-channels?device_id={device or self.did}",
            headers={"Cookie": cookie})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, json.loads(raw) if raw else {}

    def test_the_reply_carries_channels_status_and_eligibility_facts(self):
        status, body = self._get(self._cookie("owner", "ownerpassword"))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["channels"][0]["name"], "GATE")
        self.assertTrue(body["channels"][0]["enabled"] is True)
        self.assertEqual(body["scrape"]["state"], "ok")
        self.assertEqual(body["vendor"], "cpplus")
        self.assertIn("cpplus", body["known_vendors"])
        self.assertFalse(body["has_credentials"])
        self.assertFalse(body["can_refresh"])

    def test_a_worker_sees_no_unassigned_device(self):
        status, _ = self._get(self._cookie("field", "fieldpassword"))
        self.assertNotEqual(status, 200)

    def test_refresh_refuses_an_unconfigured_nvr_with_a_reason(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/inventory/nvr-refresh",
                     body=json.dumps({"device_id": self.did}),
                     headers={"Content-Type": "application/json",
                              "Cookie": self._cookie("owner", "ownerpassword")})
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        self.assertEqual(resp.status, 400, body)
        self.assertIn("Cameras tab", body["error"])

    def test_a_worker_may_not_trigger_a_refresh(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/inventory/nvr-refresh",
                     body=json.dumps({"device_id": self.did}),
                     headers={"Content-Type": "application/json",
                              "Cookie": self._cookie("field", "fieldpassword")})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 403)

    def _watch(self, cookie, channel_no, on):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/inventory/nvr-watch",
                     body=json.dumps({"device_id": self.did,
                                      "channel_no": channel_no, "on": on}),
                     headers={"Content-Type": "application/json",
                              "Cookie": cookie})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status

    def test_the_owner_toggles_a_watch_and_it_sticks(self):
        cookie = self._cookie("owner", "ownerpassword")
        self.assertEqual(self._watch(cookie, 0, False), 200)
        chan = self.store.list_nvr_channels(ORG, self.did)[0]
        self.assertFalse(chan["monitored"])
        self.assertEqual(self._watch(cookie, 0, True), 200)
        self.assertTrue(self.store.list_nvr_channels(ORG, self.did)[0]["monitored"])

    def test_a_watch_on_a_channel_that_is_not_there_is_a_404(self):
        self.assertEqual(
            self._watch(self._cookie("owner", "ownerpassword"), 99, False), 404)

    def test_a_worker_may_not_toggle_a_watch(self):
        self.assertEqual(
            self._watch(self._cookie("field", "fieldpassword"), 0, False), 403)

    def test_the_snapshot_route_refuses_an_unconfigured_nvr_with_a_reason(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(
            "GET",
            f"/api/inventory/nvr-snapshot?device_id={self.did}&channel_no=0",
            headers={"Cookie": self._cookie("owner", "ownerpassword")})
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        self.assertEqual(resp.status, 400, body)
        self.assertIn("Cameras tab", body["error"])

    def test_a_worker_sees_no_frame_from_an_unassigned_device(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(
            "GET",
            f"/api/inventory/nvr-snapshot?device_id={self.did}&channel_no=0",
            headers={"Cookie": self._cookie("field", "fieldpassword")})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 403)


if __name__ == "__main__":
    unittest.main()
