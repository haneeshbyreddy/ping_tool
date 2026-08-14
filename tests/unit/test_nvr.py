"""CCTV NVR: the KV parse, digest auth, transition rules and the recipe vocabulary.

Cameras are a roster read off the NVR's own web API — never org_devices rows.
The rules pinned here are the ones that page people or blank a live camera:
an absent state cell is unknown (never offline), unknown pages in neither
direction, and several cameras dark in one sweep is ONE page, not N.
"""
from __future__ import annotations

import os
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))

from wisp.central import nvr, nvr_profiles  # noqa: E402
from wisp.central.inventory import InventoryError  # noqa: E402

CHANNELS = """table.RemoteDevice[0].Address=172.168.103.201
table.RemoteDevice[0].Port=37777
table.RemoteDevice[0].DeviceName=GATE-EAST
table.RemoteDevice[0].DeviceType=IPC
table.RemoteDevice[0].Enable=true
table.RemoteDevice[3].Address=172.168.103.204
table.RemoteDevice[3].Enable=false
table.RemoteDevice[3].DeviceName=OLD-CAM
table.RemoteDevice[7].Address=172.168.103.208
table.RemoteDevice[7].Enable=true
table.RemoteDevice[9].Address=
table.RemoteDevice[9].Enable=true
"""

NAMES = """table.ChannelTitle[0].Name=MAIN GATE
table.ChannelTitle[7].Name=BUS STAND
table.ChannelTitle[8].Name=UNUSED SLOT
"""

STATES = """list.info[0].channel=0
list.info[0].connectionState=Connected
list.info[1].channel=7
list.info[1].connectionState=Unconnected
list.info[2].channel=3
list.info[2].connectionState=Sleeping
"""


class KvParseTest(unittest.TestCase):

    def test_a_channel_row_needs_an_address_or_it_is_an_empty_slot(self):
        rows = nvr.parse_channels(CHANNELS)
        self.assertEqual([r["channel_no"] for r in rows], [0, 3, 7])

    def test_the_row_carries_the_camera_facts(self):
        row = nvr.parse_channels(CHANNELS)[0]
        self.assertEqual(row["ip_address"], "172.168.103.201")
        self.assertEqual(row["port"], 37777)
        self.assertEqual(row["name"], "GATE-EAST")
        self.assertEqual(row["camera_kind"], "IPC")
        self.assertTrue(row["enabled"])

    def test_a_disabled_channel_is_kept_and_marked(self):
        rows = {r["channel_no"]: r for r in nvr.parse_channels(CHANNELS)}
        self.assertFalse(rows[3]["enabled"])

    def test_channel_titles_join_by_slot(self):
        names = nvr.parse_names(NAMES)
        self.assertEqual(names[0], "MAIN GATE")
        self.assertNotIn(1, names)

    def test_states_join_on_the_CHANNEL_FIELD_not_the_list_position(self):
        prof = nvr_profiles.builtin("cpplus")
        states = nvr.parse_states(STATES, prof)
        self.assertEqual(states[7], "offline")
        self.assertEqual(states[0], "online")

    def test_a_state_word_outside_the_map_reads_unknown_never_offline(self):
        prof = nvr_profiles.builtin("cpplus")
        states = nvr.parse_states(STATES, prof)
        self.assertEqual(states[3], "unknown")

    def test_NO_EVENTS_is_an_empty_loss_set_not_a_failure(self):
        self.assertEqual(nvr.parse_event_indexes("Error: No Events"), set())
        self.assertEqual(nvr.parse_event_indexes("found=0\n"), set())

    def test_event_indexes_name_the_lost_channels(self):
        self.assertEqual(
            nvr.parse_event_indexes("events[0]=3\nevents[1]=5\n"), {3, 5})

    def test_a_DIFFERENT_error_is_a_failure_never_all_online(self):
        self.assertIsNone(nvr.parse_event_indexes("Error\nBad Request!"))
        self.assertIsNone(nvr.parse_event_indexes(""))
        self.assertIsNone(nvr.parse_event_indexes("<html>login</html>"))

    def test_junk_lines_are_ignored(self):
        rows = nvr.parse_channels("garbage\nno equals here\n=bare\n")
        self.assertEqual(rows, [])

    def test_the_UUID_KEYED_build_parses_too(self):
        text = """table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Name=IPG-7930PHS
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Enable=true
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Address=172.168.103.243
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Port=37777
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.ProtocolType=Onvif
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.DeviceType=
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.VideoInputs[0].MainStreamUrl=
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_15.Name=
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_15.Enable=false
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_15.Address=192.168.0.0
table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_15.ProtocolType=CPPLUS
"""
        rows = nvr.parse_channels(text)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["channel_no"], 0)
        self.assertEqual(row["ip_address"], "172.168.103.243")
        self.assertEqual(row["name"], "IPG-7930PHS")
        self.assertEqual(row["camera_kind"], "Onvif")
        self.assertEqual(row["port"], 37777)

    def test_an_EMPTY_SLOT_is_not_a_camera(self):
        text = ("table.RemoteDevice[8].Address=192.168.0.0\n"
                "table.RemoteDevice[8].Enable=false\n")
        self.assertEqual(nvr.parse_channels(text), [])

    def test_a_DISABLED_camera_with_a_real_address_is_kept(self):
        text = ("table.RemoteDevice[3].Address=172.168.103.244\n"
                "table.RemoteDevice[3].Enable=false\n")
        rows = nvr.parse_channels(text)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["enabled"])


class DigestTest(unittest.TestCase):

    def test_the_PUBLISHED_rfc2617_vector(self):
        ch = nvr.parse_challenge(
            'Digest realm="testrealm@host.com", qop="auth,auth-int", '
            'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", '
            'opaque="5ccc069c403ebaf9f0171e9517f40e41"')
        hdr = nvr.digest_header("GET", "/dir/index.html", "Mufasa",
                                "Circle Of Life", ch, nc=1, cnonce="0a4f113b")
        self.assertIn('response="6629fae49393a05397450978507c4ef1"', hdr)
        self.assertIn("qop=auth", hdr)
        self.assertIn('opaque="5ccc069c403ebaf9f0171e9517f40e41"', hdr)

    def test_a_challenge_without_qop_uses_the_rfc2069_form(self):
        ch = nvr.parse_challenge('Digest realm="r", nonce="n1"')
        hdr = nvr.digest_header("GET", "/x", "u", "p", ch)
        self.assertNotIn("qop=", hdr)
        self.assertIn('uri="/x"', hdr)

    def test_an_algorithm_we_do_not_speak_sends_NOTHING(self):
        ch = nvr.parse_challenge(
            'Digest realm="r", nonce="n1", algorithm=SHA-256')
        self.assertIsNone(nvr.digest_header("GET", "/x", "u", "p", ch))

    def test_a_basic_challenge_is_not_a_digest_challenge(self):
        self.assertIsNone(nvr.parse_challenge('Basic realm="r"'))

    def test_the_uri_keeps_its_query_string(self):
        ch = nvr.parse_challenge('Digest realm="r", nonce="n1"')
        hdr = nvr.digest_header(
            "GET", "/cgi-bin/configManager.cgi?action=getConfig", "u", "p", ch)
        self.assertIn('uri="/cgi-bin/configManager.cgi?action=getConfig"', hdr)


class TransitionTest(unittest.TestCase):

    def _row(self, no, state, enabled=True, name=None):
        return {"channel_no": no, "state": state, "enabled": enabled,
                "name": name}

    def test_online_to_offline_is_dark(self):
        t = nvr.transitions({0: "online"}, [self._row(0, "offline")])
        self.assertEqual(len(t["dark"]), 1)
        self.assertEqual(t["restored"], [])

    def test_offline_to_online_is_restored(self):
        t = nvr.transitions({0: "offline"}, [self._row(0, "online")])
        self.assertEqual(len(t["restored"]), 1)

    def test_UNKNOWN_pages_in_neither_direction(self):
        t = nvr.transitions({0: "online", 1: "unknown"},
                            [self._row(0, "unknown"), self._row(1, "offline")])
        self.assertEqual(t["dark"], [])
        self.assertEqual(t["restored"], [])

    def test_a_channel_seen_for_the_first_time_pages_nothing(self):
        t = nvr.transitions({}, [self._row(0, "offline")])
        self.assertEqual(t["dark"], [])

    def test_a_disabled_camera_never_pages(self):
        t = nvr.transitions({0: "online"},
                            [self._row(0, "offline", enabled=False)])
        self.assertEqual(t["dark"], [])

    def test_an_UNWATCHED_camera_pages_in_neither_direction(self):
        t = nvr.transitions({0: "online", 1: "offline"},
                            [self._row(0, "offline"), self._row(1, "online")],
                            unwatched={0, 1})
        self.assertEqual(t["dark"], [])
        self.assertEqual(t["restored"], [])

    def test_the_batch_detail_names_the_first_few_and_counts_the_rest(self):
        rows = [self._row(i, "offline", name=f"CAM{i}") for i in range(6)]
        detail = nvr.batch_detail(rows)
        self.assertIn("CH1 CAM0", detail)
        self.assertIn("+2", detail)

    def test_a_channel_label_is_one_based_like_the_nvr_ui(self):
        self.assertEqual(nvr.channel_label({"channel_no": 0}), "CH1")
        self.assertEqual(nvr.channel_label({"channel_no": 3, "name": "GATE"}),
                         "CH4 GATE")


class _FakeHttp:
    timeout_s = 30.0

    def __init__(self, replies: dict) -> None:
        self.replies = replies
        self.asked: list[str] = []

    def get(self, path, **kw):
        self.asked.append(path)
        r = self.replies.get(path)
        if isinstance(r, list):
            return r.pop(0) if len(r) > 1 else r[0]
        return r if r is not None else nvr.Response(404, [], b"")


class SnapshotFetchTest(unittest.TestCase):

    def setUp(self):
        self._pause = nvr.SNAPSHOT_RETRY_PAUSE_S
        nvr.SNAPSHOT_RETRY_PAUSE_S = 0.0

    def tearDown(self):
        nvr.SNAPSHOT_RETRY_PAUSE_S = self._pause

    def _fetch(self, resp):
        prof = nvr_profiles.builtin("cpplus")
        http = _FakeHttp({"/cgi-bin/snapshot.cgi?channel=4": resp})
        return (*nvr.fetch_snapshot(http, "u", "p", 3, prof), http)

    def test_a_jpeg_comes_back_as_bytes(self):
        frame, err, _ = self._fetch(
            nvr.Response(200, [("Content-Type", "image/jpeg")],
                         b"\xff\xd8\xff\xe0" + b"0" * 50))
        self.assertIsNone(err)
        self.assertTrue(frame.startswith(b"\xff\xd8"))

    def test_a_400_is_the_dark_channel_refusal(self):
        frame, err, _ = self._fetch(nvr.Response(400, [], b"Error"))
        self.assertIsNone(frame)
        self.assertIn("dark or empty", err)

    def test_a_persistent_500_is_RETRIED_then_named_honestly(self):
        frame, err, http = self._fetch(nvr.Response(500, [], b""))
        self.assertIsNone(frame)
        self.assertIn("camera did not answer", err)
        self.assertIn("3 tries", err)
        self.assertEqual(len(http.asked), 3)

    def test_a_flaky_grab_is_rescued_on_the_second_try(self):
        frame, err, http = self._fetch([
            nvr.Response(500, [], b""),
            nvr.Response(200, [("Content-Type", "image/jpeg")],
                         b"\xff\xd8\xff\xe0" + b"0" * 50)])
        self.assertIsNone(err)
        self.assertTrue(frame.startswith(b"\xff\xd8"))
        self.assertEqual(len(http.asked), 2)

    def test_a_TIMEOUT_is_not_retried_the_wait_was_already_paid(self):
        frame, err, http = self._fetch(
            nvr.Response(0, [], b"", error="tunnel timeout"))
        self.assertIsNone(frame)
        self.assertIn("within 30s", err)
        self.assertEqual(len(http.asked), 1)

    def test_a_reply_that_is_not_an_image_is_REFUSED(self):
        frame, err, _ = self._fetch(
            nvr.Response(200, [], b"<html>login</html>"))
        self.assertIsNone(frame)
        self.assertIn("not an image", err)


class ProfileTest(unittest.TestCase):

    def test_the_builtin_resolves(self):
        prof = nvr_profiles.builtin("cpplus")
        self.assertEqual(prof.login_flow, "digest")
        self.assertTrue(prof.channels_path.startswith("/cgi-bin/"))

    def test_a_url_in_a_path_is_refused_outright(self):
        with self.assertRaises(InventoryError):
            nvr_profiles.clean_nvr_profile_payload({
                "name": "x", "channels_path": "http://evil/cgi"})

    def test_an_unknown_login_flow_rejects_the_whole_profile(self):
        with self.assertRaises(InventoryError):
            nvr_profiles.clean_nvr_profile_payload({
                "name": "x", "channels_path": "/c", "login_flow": "form"})

    def test_a_state_map_value_outside_the_vocabulary_is_refused(self):
        with self.assertRaises(InventoryError):
            nvr_profiles.clean_nvr_profile_payload({
                "name": "x", "channels_path": "/c",
                "state_map": {"connected": "dark"}})

    def test_an_unknown_state_format_rejects_the_whole_profile(self):
        with self.assertRaises(InventoryError):
            nvr_profiles.clean_nvr_profile_payload({
                "name": "x", "channels_path": "/c", "state_format": "json"})

    def test_the_builtin_reads_states_over_RPC2_like_the_nvr_ui_does(self):
        prof = nvr_profiles.builtin("cpplus")
        self.assertEqual(prof.state_format, "rpc2-camerastate")

    def test_UNCONNECT_the_cp_unr_word_for_a_dead_camera_reads_offline(self):
        prof = nvr_profiles.builtin("cpplus")
        self.assertEqual(prof.state_of("Unconnect"), "offline")
        self.assertEqual(prof.state_of("Unconnected"), "offline")

    def test_the_rpc2_password_chain_is_md5_upper_of_md5_upper(self):
        import hashlib
        ha = hashlib.md5(b"admin:Realm:pw").hexdigest().upper()
        want = hashlib.md5(f"admin:RND:{ha}".encode()).hexdigest().upper()
        self.assertEqual(
            nvr.rpc2_password_hash("admin", "pw", "Realm", "RND"), want)

    def test_rpc2_states_parse_and_refuse(self):
        prof = nvr_profiles.builtin("cpplus")
        good = {"result": True, "params": {"states": [
            {"channel": 0, "connectionState": "Connected"},
            {"channel": 1, "connectionState": "Unconnected"},
            {"channel": 2, "connectionState": "Weird"}]}}
        states, unmapped = nvr.parse_rpc2_states(good, prof)
        self.assertEqual(states, {0: "online", 1: "offline", 2: "unknown"})
        self.assertEqual(unmapped, {"Weird"})
        self.assertIsNone(nvr.parse_rpc2_states({"result": True}, prof)[0])
        self.assertIsNone(nvr.parse_rpc2_states(
            {"params": {"states": "nope"}}, prof)[0])

    def test_a_disabled_row_is_a_tombstone_that_hides_the_builtin(self):
        pset = nvr_profiles.ProfileSet.build([
            {"name": "cpplus", "org_id": None, "enabled": False, "spec": {}}])
        self.assertIsNone(pset.resolve("o1", "cpplus"))
        self.assertNotIn("cpplus", pset.names())

    def test_a_same_named_org_row_shadows_the_builtin(self):
        pset = nvr_profiles.ProfileSet.build([
            {"name": "cpplus", "org_id": "o1", "enabled": True,
             "spec": {"channels_path": "/custom.cgi"}}])
        self.assertEqual(pset.resolve("o1", "cpplus").channels_path,
                         "/custom.cgi")
        self.assertNotEqual(
            pset.resolve("other", "cpplus").channels_path, "/custom.cgi")

    def test_the_snapshot_channel_is_ONE_based_on_the_builtin(self):
        prof = nvr_profiles.builtin("cpplus")
        self.assertEqual(prof.snapshot_url(3),
                         "/cgi-bin/snapshot.cgi?channel=4")

    def test_a_snapshot_path_without_the_placeholder_is_refused(self):
        with self.assertRaises(InventoryError):
            nvr_profiles.clean_nvr_profile_payload({
                "name": "x", "channels_path": "/c",
                "snapshot_path": "/cgi-bin/snapshot.cgi"})

    def test_a_snapshot_base_outside_0_or_1_is_refused(self):
        with self.assertRaises(InventoryError):
            nvr_profiles.clean_nvr_profile_payload({
                "name": "x", "channels_path": "/c",
                "snapshot_path": "/s?c={channel}",
                "snapshot_channel_base": 2})

    def test_a_broken_stored_spec_falls_back_to_the_builtin(self):
        pset = nvr_profiles.ProfileSet.build([
            {"name": "cpplus", "org_id": None, "enabled": True,
             "spec": "{not json"}])
        self.assertIsNotNone(pset.resolve("o1", "cpplus"))


if __name__ == "__main__":
    unittest.main()
