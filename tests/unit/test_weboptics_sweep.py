import base64
import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wisp.central.secretbox import DecryptError
from wisp.central.weboptics import DEFAULT_PONS
from wisp.central.weboptics_sweep import WebOpticsSweeper, _pons_for, endpoint
from wisp.config import Config


class FakeProxy:
    def __init__(self, polling=True, sessions=(), replies=None, expired=()):
        self.polling = polling
        self.sessions = list(sessions)
        self.replies = list(replies or [])
        self.expired = list(expired)
        self.submits = []
        self.idle_windows = []

    def polled_recently(self, org_id, node_id, within_s):
        return self.polling

    def active_sessions_for(self, org_id, node_id, idle_s=None):
        self.idle_windows.append(idle_s)
        return list(self.sessions)

    def reap_expired(self):
        gone, self.expired = list(self.expired), []
        return gone

    def submit(self, sess, *, method, path, headers, body, timeout, extra=None):
        self.submits.append({"path": path, "body": body, "sid": sess.sid,
                             "ip": sess.device_ip, "port": sess.device_port,
                             "scheme": sess.scheme, "extra": extra})
        return self.replies.pop(0) if self.replies else None


class FakeStore:
    def __init__(self, targets=(), creds=None, profiles=(), mac_profiles=()):
        self._targets = list(targets)
        self._creds = creds
        self._profiles = list(profiles)
        self._mac_profiles = list(mac_profiles)
        self.stored = []
        self.status = []
        self.retired = []
        self.macs = []
        self.mac_status = []

    def list_web_optics_profiles(self, org_id):
        return list(self._profiles)

    def list_web_mac_profiles(self, org_id):
        return list(self._mac_profiles)

    def user_mac_targets(self, vendors=("dbc",), device_id=None):
        return []

    def upsert_user_macs(self, org_id, device_id, rows, ts):
        self.macs.append((org_id, device_id, list(rows), ts))
        return len(rows)

    def set_web_mac_status(self, org_id, device_id, profile, state, detail,
                           rows, declared=None):
        self.mac_status.append({"device_id": device_id, "profile": profile,
                                "state": state, "detail": detail, "rows": rows,
                                "declared": declared})

    def web_optics_targets(self, vendors=("dbc",), device_id=None):
        self.asked_vendors = set(vendors)
        rows = [t for t in self._targets
                if str(t.get("vendor") or "dbc") in self.asked_vendors]
        if device_id is not None:
            rows = [t for t in rows if t.get("id") == device_id]
        return rows

    def close_proxy_session(self, sid, status):
        self.retired.append((sid, status))
        return True

    def get_device_webui_credentials(self, org_id, device_id):
        return self._creds

    def upsert_web_optics(self, org_id, device_id, rows, ts):
        self.stored.append((org_id, device_id, list(rows), ts))
        return len(rows)

    def set_web_optics_status(self, org_id, device_id, profile, state, detail, rows):
        self.status.append({"device_id": device_id, "profile": profile,
                            "state": state, "detail": detail, "rows": rows})


class FakeBox:
    def __init__(self, plaintext="pw", raises=False):
        self.plaintext = plaintext
        self.raises = raises

    def decrypt(self, token):
        if self.raises:
            raise DecryptError("key rotated")
        return self.plaintext


def target(**kw):
    dev = {"id": 8, "org_id": "byreddy", "name": "PYLON-OLT",
           "ip_address": "172.168.107.242", "assigned_node_id": "edge-1",
           "web_ip": None, "web_port": None, "web_scheme": None,
           "username": "admin", "password_enc": "enc:xxx"}
    dev.update(kw)
    return dev


CREDS = {"device_id": 8, "username": "admin", "password_enc": "enc:xxx",
         "auth_mode": "form"}


def sweeper(store=None, proxy=None, box=None, **cfgkw):
    return WebOpticsSweeper(store or FakeStore(), proxy or FakeProxy(),
                            box or FakeBox(), Config(**cfgkw))


class EndpointTest(unittest.TestCase):

    def test_plain_device_defaults_to_http_on_80(self):
        self.assertEqual(endpoint(target()), ("172.168.107.242", 80, "http"))

    def test_owner_override_wins(self):
        dev = target(web_ip="10.9.9.9", web_port=8443, web_scheme="https")
        self.assertEqual(endpoint(dev), ("10.9.9.9", 8443, "https"))

    def test_declared_port_picks_its_usual_scheme(self):
        self.assertEqual(endpoint(target(web_port=443))[2], "https")
        self.assertEqual(endpoint(target(web_port=8080))[2], "http")

    def test_no_address_is_not_scrapable(self):
        self.assertIsNone(endpoint(target(ip_address="", web_ip=None)))

    def test_a_nonsense_port_is_refused_rather_than_clamped(self):
        self.assertIsNone(endpoint(target(web_port=99999)))


class PonsForTest(unittest.TestCase):

    def test_the_roster_decides(self):
        self.assertEqual(
            _pons_for(target(pon_ports="EPON0/1,EPON0/3,EPON0/8")), (1, 3, 8))

    def test_an_olt_with_no_roster_labels_falls_back_to_the_common_four(self):
        self.assertEqual(_pons_for(target(pon_ports=None)), DEFAULT_PONS)
        self.assertEqual(_pons_for(target(pon_ports="")), DEFAULT_PONS)
        self.assertEqual(_pons_for(target(pon_ports="?,??")), DEFAULT_PONS)


class SweeperGateTest(unittest.TestCase):
    def test_a_dormant_tunnel_is_skipped(self):
        proxy = FakeProxy(polling=False)
        self.assertIsNone(sweeper(FakeStore(creds=CREDS), proxy).scrape_device(target()))
        self.assertEqual(proxy.submits, [])

    def test_an_olt_someone_is_browsing_is_left_alone(self):
        proxy = FakeProxy(sessions=[{"sid": "abc", "ttl_s": 300}])
        self.assertIsNone(sweeper(FakeStore(creds=CREDS), proxy).scrape_device(target()))
        self.assertEqual(proxy.submits, [])

    def test_the_browse_gate_asks_about_USE_not_existence(self):
        proxy = FakeProxy()
        s = sweeper(FakeStore(creds=CREDS), proxy, web_optics_browse_idle_s=180)
        with self.assertLogs("wisp.central.weboptics", level="WARNING"):
            s.scrape_device(target())
        self.assertEqual(proxy.idle_windows, [180])
        self.assertLess(proxy.idle_windows[0], Config().proxy_session_ttl_s)

    def test_a_device_with_no_stored_login_is_skipped(self):
        proxy = FakeProxy()
        self.assertIsNone(sweeper(FakeStore(creds=None), proxy).scrape_device(target()))
        self.assertEqual(proxy.submits, [])

    def test_a_password_that_will_not_decrypt_skips_the_olt(self):
        proxy = FakeProxy()
        s = sweeper(FakeStore(creds=CREDS), proxy, FakeBox(raises=True))
        with self.assertLogs("wisp.central.weboptics", level="WARNING") as logs:
            self.assertIsNone(s.scrape_device(target()))
        self.assertIn("will not decrypt", logs.output[0])
        self.assertEqual(proxy.submits, [])

    def test_a_device_with_no_web_address_is_skipped(self):
        proxy = FakeProxy()
        s = sweeper(FakeStore(creds=CREDS), proxy)
        self.assertIsNone(s.scrape_device(target(ip_address="")))
        self.assertEqual(proxy.submits, [])

    def test_a_scrape_already_running_on_this_olt_is_not_joined(self):
        proxy = FakeProxy()
        s = sweeper(FakeStore(creds=CREDS), proxy)
        s._lock_for(8).acquire()
        self.assertIsNone(s.scrape_device(target()))
        self.assertEqual(proxy.submits, [])


def preflight_reply(results):
    doc = json.dumps({"preflight": True, "results": results}).encode()
    return {"status": 200, "headers": [],
            "body_b64": base64.b64encode(doc).decode()}


class PreflightTest(unittest.TestCase):

    def test_the_scrape_follows_the_endpoint_the_edge_confirms(self):
        proxy = FakeProxy(replies=[
            preflight_reply([["172.168.107.242", 443, "https", True],
                             ["172.168.107.242", 80, "http", False]]),
        ])
        s = sweeper(FakeStore(creds=CREDS), proxy)
        with self.assertLogs("wisp.central.weboptics", level="WARNING"):
            s.scrape_device(target())
        probe, login = proxy.submits[0], proxy.submits[1]
        self.assertEqual(probe["sid"], "preflight")
        self.assertEqual(probe["extra"]["kind"], "preflight")
        self.assertEqual((login["ip"], login["port"], login["scheme"]),
                         ("172.168.107.242", 443, "https"))

    def test_no_credentials_are_sent_when_nothing_answers(self):
        proxy = FakeProxy(replies=[
            preflight_reply([["172.168.107.242", 443, "https", False],
                             ["172.168.107.242", 80, "http", False]]),
        ])
        s = sweeper(FakeStore(creds=CREDS), proxy)
        with self.assertLogs("wisp.central.weboptics", level="WARNING"):
            device_id, count, err = s.scrape_device(target())
        self.assertEqual((device_id, count), (8, 0))
        self.assertIn("unreachable", err)
        self.assertEqual([x["sid"] for x in proxy.submits], ["preflight"])

    def test_an_inconclusive_probe_keeps_the_heuristic(self):
        proxy = FakeProxy(replies=[{"status": 200, "headers": [],
                                    "body_b64": ""}])
        s = sweeper(FakeStore(creds=CREDS), proxy)
        with self.assertLogs("wisp.central.weboptics", level="WARNING"):
            s.scrape_device(target())
        login = proxy.submits[1]
        self.assertEqual((login["port"], login["scheme"]), (80, "http"))

    def test_the_preflight_costs_nothing_for_a_device_we_cannot_scrape(self):
        proxy = FakeProxy()
        sweeper(FakeStore(creds=None), proxy).scrape_device(target())
        self.assertEqual(proxy.submits, [])


class SweeperRunTest(unittest.TestCase):

    def test_a_failed_scrape_logs_and_returns_the_error(self):
        proxy = FakeProxy(replies=[])
        s = sweeper(FakeStore(creds=CREDS), proxy)
        with self.assertLogs("wisp.central.weboptics", level="WARNING"):
            res = s.scrape_device(target())
        device_id, count, err = res
        self.assertEqual((device_id, count), (8, 0))
        self.assertIn("credentials NOT sent", err)
        self.assertNotIn(b"pass=pw", b"".join(
            s["body"] or b"" for s in proxy.submits))

    def test_the_lock_is_released_even_when_the_scrape_fails(self):
        proxy = FakeProxy(replies=[])
        s = sweeper(FakeStore(creds=CREDS), proxy)
        with self.assertLogs("wisp.central.weboptics", level="WARNING"):
            s.scrape_device(target())
        self.assertTrue(s._lock_for(8).acquire(blocking=False))

    def test_one_bad_olt_does_not_end_the_sweep(self):
        store = FakeStore(targets=[target(id=8), target(id=9, name="HILL-OLT")],
                          creds=CREDS)
        s = sweeper(store, FakeProxy(replies=[]))
        with self.assertLogs("wisp.central.weboptics", level="WARNING"):
            out = s.sweep_once()
        self.assertEqual([r[0] for r in out], [8, 9])

    def test_a_store_that_cannot_list_targets_is_not_fatal(self):
        class Broken(FakeStore):
            def web_optics_targets(self):
                raise RuntimeError("db is gone")

        with self.assertLogs("wisp.central.weboptics", level="ERROR"):
            self.assertEqual(sweeper(Broken()).sweep_once(), [])

    def test_locks_are_per_device_not_shared(self):
        s = sweeper()
        self.assertIsNot(s._lock_for(8), s._lock_for(9))
        self.assertIs(s._lock_for(8), s._lock_for(8))

    def test_lock_creation_is_threadsafe(self):
        s = sweeper()
        seen, barrier = [], threading.Barrier(8)

        def grab():
            barrier.wait()
            seen.append(s._lock_for(42))

        threads = [threading.Thread(target=grab) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len({id(x) for x in seen}), 1)


class SessionReapTest(unittest.TestCase):

    def test_the_sweep_retires_timed_out_sessions_in_both_places(self):
        store = FakeStore()
        proxy = FakeProxy(expired=["sid-one", "sid-two"])
        with self.assertLogs("wisp.central.weboptics", level="INFO"):
            sweeper(store, proxy).sweep_once()
        self.assertEqual(store.retired,
                         [("sid-one", "expired"), ("sid-two", "expired")])

    def test_a_reap_that_fails_does_not_cost_the_sweep(self):
        class Broken(FakeProxy):
            def reap_expired(self):
                raise RuntimeError("hub is wedged")

        store = FakeStore(targets=[target()], creds=CREDS)
        s = sweeper(store, Broken(replies=[]))
        with self.assertLogs("wisp.central.weboptics", level="ERROR"):
            out = s.sweep_once()
        self.assertEqual([r[0] for r in out], [8])


class ScrapeOneTest(unittest.TestCase):

    def test_it_scrapes_the_named_olt_only(self):
        store = FakeStore(targets=[target(id=8), target(id=9, name="HILL-OLT")],
                          creds=CREDS)
        s = sweeper(store, FakeProxy(replies=[]))
        with self.assertLogs("wisp.central.weboptics", level="WARNING"):
            res = s.scrape_one("byreddy", 9)
        self.assertEqual(res[0], 9)

    def test_an_ineligible_device_records_NOTHING(self):
        store = FakeStore(targets=[target(id=8)], creds=CREDS)
        self.assertIsNone(sweeper(store, FakeProxy()).scrape_one("byreddy", 99))
        self.assertEqual(store.status, [])

    def test_another_orgs_device_id_is_not_scrapable(self):
        store = FakeStore(targets=[target(id=8, org_id="byreddy")], creds=CREDS)
        s = sweeper(store, FakeProxy())
        self.assertIsNone(s.scrape_one("someone-else", 8))

    def test_busy_reports_a_running_scrape(self):
        s = sweeper()
        self.assertFalse(s.busy(8))
        s._lock_for(8).acquire()
        self.assertTrue(s.busy(8))
        self.assertTrue(s.busy(8))


if __name__ == "__main__":
    unittest.main()
