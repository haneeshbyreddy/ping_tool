import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.config import Config
from wisp.central import engine as central_engine
from wisp.central.dispatch import CentralAlertDispatcher
from wisp.central.notify_policy import AlertRouter, flush_digests
from wisp.central.store import CentralStore
from wisp.core.state_machine import DOWN, OutageOpened, OutageResolved
from wisp.central.whatsapp_bot import WhatsAppBot
from support import RecordingNotifier

T0 = "2026-01-01T00:00:00+00:00"


class OrgRoleWhatsappTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")
        self.store.set_org("ispA")

    def tearDown(self):
        self.tmp.cleanup()

    def test_numbers_by_role_active_only(self):
        o1 = self.store.add_user("ispA", "own1", "h", "s", "owner")
        o2 = self.store.add_user("ispA", "own2", "h", "s", "owner")
        w1 = self.store.add_user("ispA", "wkr1", "h", "s", "worker")
        no_num = self.store.add_user("ispA", "own3", "h", "s", "owner")  # noqa: F841
        off = self.store.add_user("ispA", "own4", "h", "s", "owner")
        self.store.set_user_whatsapp(o1, "919000000001")
        self.store.set_user_whatsapp(o2, "919000000002")
        self.store.set_user_whatsapp(w1, "919000000009")
        self.store.set_user_whatsapp(off, "919000000000")
        self.store.set_user_active(off, False)

        self.assertEqual(self.store.org_role_whatsapp("ispA", "owner"),
                         ["919000000001", "919000000002"])
        self.assertEqual(self.store.org_role_whatsapp("ispA", "worker"),
                         ["919000000009"])

    def test_empty_when_none_set(self):
        self.store.add_user("ispA", "own1", "h", "s", "owner")
        self.assertEqual(self.store.org_role_whatsapp("ispA", "owner"), [])

    def test_clearing_a_number_removes_it(self):
        u = self.store.add_user("ispA", "own1", "h", "s", "owner")
        self.store.set_user_whatsapp(u, "919000000001")
        self.assertEqual(self.store.org_role_whatsapp("ispA", "owner"),
                         ["919000000001"])
        self.store.set_user_whatsapp(u, None)
        self.assertEqual(self.store.org_role_whatsapp("ispA", "owner"), [])

    def test_scoped_to_org(self):
        self.store.set_org("ispB")
        a = self.store.add_user("ispA", "a1", "h", "s", "owner")
        b = self.store.add_user("ispB", "b1", "h", "s", "owner")
        self.store.set_user_whatsapp(a, "919000000001")
        self.store.set_user_whatsapp(b, "919000000002")
        self.assertEqual(self.store.org_role_whatsapp("ispA", "owner"),
                         ["919000000001"])


class WhatsappSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_only_whatsapp_prefixed_keys_stripped(self):
        self.store.set_setting("whatsapp_enabled", "1")
        self.store.set_setting("whatsapp_token", "TOK")
        self.store.set_setting("google_maps_key", "AIza")
        self.assertEqual(self.store.whatsapp_settings(),
                         {"enabled": "1", "token": "TOK"})


class DispatchFanoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db")
        self.store = CentralStore(self.cfg.central_db)
        self.dev = self.store.create_org_device("ispA", {
            "name": "Tower", "ip_address": "10.0.0.1", "device_type": None,
            "region": "Rampur", "parent_device_id": None})
        self.store.set_org("ispA", ntfy_topic_owner="a-owner",
                           ntfy_topic_worker="a-worker")
        o = self.store.add_user("ispA", "own1", "h", "s", "owner")
        w = self.store.add_user("ispA", "wkr1", "h", "s", "worker")
        self.store.set_user_whatsapp(o, "919000000001")
        self.store.set_user_whatsapp(w, "919000000009")
        self.engine = central_engine.build_engine(self.store, "ispA", self.cfg)
        self.notifier = RecordingNotifier()
        self.disp = CentralAlertDispatcher(self.store, "ispA", self.engine,
                                           self.notifier, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_device_down_fans_the_whole_audience(self):
        self.store.open_outage_if_absent("ispA", self.dev, T0, DOWN)
        self.disp.dispatch([OutageOpened(self.dev, DOWN)], T0)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(sorted(self.notifier.sent[0]["whatsapp"]),
                         ["919000000001", "919000000009"])
        self.assertEqual(self.notifier.sent[0]["facts"].status, "DOWN")

    def test_resolve_broadcast_fans_the_audience(self):
        self.store.open_outage_if_absent("ispA", self.dev, T0, DOWN)
        self.disp.dispatch([OutageOpened(self.dev, DOWN)], T0)
        self.store.resolve_outage("ispA", self.dev, T0)
        self.notifier.sent.clear()
        self.disp.dispatch([OutageResolved(self.dev)], T0)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(sorted(self.notifier.sent[0]["whatsapp"]),
                         ["919000000001", "919000000009"])
        self.assertEqual(self.notifier.sent[0]["facts"].status, "UP")

    def test_no_numbers_no_page(self):
        self.store.set_user_whatsapp(
            self.store.get_user_by_username("own1")["id"], None)
        self.store.set_user_whatsapp(
            self.store.get_user_by_username("wkr1")["id"], None)
        self.store.open_outage_if_absent("ispA", self.dev, T0, DOWN)
        self.disp.dispatch([OutageOpened(self.dev, DOWN)], T0)
        self.assertEqual(self.notifier.sent, [])


class RouterPushFanoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db")
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org("ispA")
        w = self.store.add_user("ispA", "wkr1", "h", "s", "worker")
        self.store.set_user_whatsapp(w, "919000000009")
        self.notifier = RecordingNotifier()
        self.router = AlertRouter(self.store, "ispA", self.notifier, self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_push_resolves_org_numbers(self):
        self.router.emit("PORT_DOWN", title="port down",
                         body="if 3", priority=3, ts=T0, device_id=7, cooldown_min=0)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0]["whatsapp"], ["919000000009"])

    def test_digest_flush_fans_org_numbers(self):
        self.store.queue_digest("ispA", 7, "PON_FAULT", "cut", "x", T0)
        self.assertEqual(self.notifier.sent, [])
        flush_digests(self.store, "ispA", self.notifier, self.cfg,
                      "2026-01-01T02:00:00+00:00")
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0]["whatsapp"], ["919000000009"])


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _text(frm, body):
    return {"entry": [{"changes": [{"value": {"messages": [
        {"from": frm, "type": "text", "text": {"body": body}}]}}]}]}


def _button(frm, bid):
    return {"entry": [{"changes": [{"value": {"messages": [
        {"from": frm, "type": "interactive", "interactive": {
            "type": "button_reply",
            "button_reply": {"id": bid, "title": "x"}}}]}}]}]}


def _status_only():
    return {"entry": [{"changes": [{"value": {"statuses": [
        {"id": "wamid", "status": "delivered"}]}}]}]}


class _RecBot:
    def __init__(self):
        self.texts = []
        self.buttons = []
        self.sent = []

    def send_text(self, to, body):
        self.texts.append((to, body))

    def send_buttons(self, to, body, buttons):
        self.buttons.append((to, body, list(buttons)))

    def send(self, title, body, priority=3, *, whatsapp=(), facts=None):
        self.sent.append((list(whatsapp), title, body))


class _FakeSweeper:
    def __init__(self, eligible=(), busy=()):
        self.eligible = set(eligible)
        self.busy_ids = set(busy)
        self.scraped = []
        self.done = threading.Event()

    def busy(self, did):
        return did in self.busy_ids

    def target(self, org, did):
        return {"id": did} if did in self.eligible else None

    def scrape_device(self, dev):
        self.scraped.append(dev)
        self.done.set()


class WhatsappResolverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")
        self.store.set_org("ispA")

    def tearDown(self):
        self.tmp.cleanup()

    def test_number_resolves_to_its_single_account(self):
        uid = self.store.add_user("ispA", "own1", "h", "s", "owner")
        self.store.set_user_whatsapp(uid, "919000000001")
        u = self.store.whatsapp_user("+91 90000-00001")
        self.assertIsNotNone(u)
        self.assertEqual((u["org_id"], u["role"]), ("ispA", "owner"))

    def test_unknown_number_is_none(self):
        self.assertIsNone(self.store.whatsapp_user("910000000000"))

    def test_deactivated_account_is_none(self):
        uid = self.store.add_user("ispA", "own1", "h", "s", "owner")
        self.store.set_user_whatsapp(uid, "919000000001")
        self.store.set_user_active(uid, False)
        self.assertIsNone(self.store.whatsapp_user("919000000001"))

    def test_number_shared_by_two_accounts_is_ambiguous_none(self):
        a = self.store.add_user("ispA", "own1", "h", "s", "owner")
        b = self.store.add_user("ispA", "wkr1", "h", "s", "worker")
        self.store.set_user_whatsapp(a, "919000000001")
        self.store.set_user_whatsapp(b, "919000000001")
        self.assertIsNone(self.store.whatsapp_user("919000000001"))


class WhatsappBotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "central.db")
        self.now = datetime.now(timezone.utc)
        self.owner = self.store.add_user("ispA", "own1", "h", "s", "owner")
        self.worker = self.store.add_user("ispA", "wkr1", "h", "s", "worker")
        self.store.set_user_whatsapp(self.owner, "919000000001")
        self.store.set_user_whatsapp(self.worker, "919000000009")
        bowner = self.store.add_user("ispB", "bown", "h", "s", "owner")
        self.store.set_user_whatsapp(bowner, "918888888888")

        self.olt = self.store.create_org_device("ispA", {
            "name": "HILL-OLT-1", "ip_address": "10.0.0.1", "device_type": "OLT",
            "region": None, "parent_device_id": None,
            "assigned_node_id": "probe1"})
        self.olt_b = self.store.create_org_device("ispB", {
            "name": "B-OLT", "ip_address": "10.9.9.9", "device_type": "OLT",
            "region": None, "parent_device_id": None,
            "assigned_node_id": "probe1"})

    def tearDown(self):
        self.tmp.cleanup()

    def _onu(self, org, did, key, serial, *, rx=-21.0, state="online",
             severity="ok", name=None, age_s=0):
        self.store.upsert_onu_optics(
            org, did, key, pon_port="EPON0/1", onu_id=1, name=name, serial=serial,
            state=state, rx_dbm=rx, tx_dbm=2.0, olt_rx_dbm=-20.0, distance_m=1200,
            rx_ref_dbm=None, rx_ref_at=None, severity=severity,
            ts=_iso(self.now - timedelta(seconds=age_s)))

    def _bot(self, notifier=None, sweeper=None):
        return WhatsAppBot(self.store, notifier or _RecBot(), sweeper,
                           base_url="https://hansanet.in")


    def test_unknown_number_gets_no_reply(self):
        rec = _RecBot()
        self._bot(rec).handle(_text("910000000000", "a4f2"))
        self.assertEqual((rec.texts, rec.buttons), ([], []))

    def test_status_callback_is_ignored(self):
        rec = _RecBot()
        self._bot(rec).handle(_status_only())
        self.assertEqual((rec.texts, rec.buttons), ([], []))


    def test_owner_lookup_returns_card_with_three_buttons(self):
        self._onu("ispA", self.olt, "onu-1", "a4:f2:1b:00:11:22", name="hc_kiran")
        rec = _RecBot()
        self._bot(rec).handle(_text("919000000001", "a4f2"))
        self.assertEqual(len(rec.buttons), 1)
        to, body, btns = rec.buttons[0]
        self.assertEqual(to, "919000000001")
        self.assertIn("HILL-OLT-1", body)
        self.assertIn("-21.00 dBm", body)
        ids = [b[0] for b in btns]
        self.assertEqual(ids, [f"refresh:{self.olt}", f"map:{self.olt}:1",
                               f"recent:{self.olt}"])

    def test_worker_lookup_omits_refresh_button(self):
        self._onu("ispA", self.olt, "onu-1", "a4:f2:1b:00:11:22")
        rec = _RecBot()
        self._bot(rec).handle(_text("919000000009", "a4f2"))
        _, _, btns = rec.buttons[0]
        ids = [b[0] for b in btns]
        self.assertNotIn(f"refresh:{self.olt}", ids)
        self.assertEqual(ids, [f"map:{self.olt}:1", f"recent:{self.olt}"])

    def test_null_rx_says_no_dbm_never_zero(self):
        self._onu("ispA", self.olt, "onu-1", "a4:f2:1b:00:11:22", rx=None)
        rec = _RecBot()
        self._bot(rec).handle(_text("919000000001", "a4f2"))
        body = rec.buttons[0][1]
        self.assertIn("no dBm reported for this OLT/vendor", body)
        self.assertNotIn("0.00 dBm", body)

    def test_down_olt_readings_are_frozen(self):
        self._onu("ispA", self.olt, "onu-1", "a4:f2:1b:00:11:22")
        self.store.write_device_states(
            "ispA", [(self.olt, "DOWN", None, 100.0, None)], _iso(self.now))
        rec = _RecBot()
        self._bot(rec).handle(_text("919000000001", "a4f2"))
        self.assertIn("frozen", rec.buttons[0][1])

    def test_no_match_says_so_and_offers_the_menu(self):
        rec = _RecBot()
        self._bot(rec).handle(_text("919000000001", "zzzznope"))
        self.assertEqual(rec.texts, [])
        _, body, btns = rec.buttons[0]
        self.assertIn("No ONU found", body)
        self.assertEqual([b[0] for b in btns], ["ask:mac", "ask:name"])


    def test_greeting_offers_the_two_search_options(self):
        rec = _RecBot()
        self._bot(rec).handle(_text("919000000001", "HI"))
        self.assertEqual(rec.texts, [])
        to, body, btns = rec.buttons[0]
        self.assertEqual(to, "919000000001")
        self.assertIn("own1", body)
        self.assertEqual(btns, [("ask:mac", "Search by MAC"),
                                ("ask:name", "Search by name")])

    def test_a_greeting_is_never_searched_for(self):
        for greeting in ("hello", "Hi there!", "good morning", "Menu", "?"):
            rec = _RecBot()
            self._bot(rec).handle(_text("919000000001", greeting))
            self.assertEqual(rec.texts, [], greeting)
            self.assertEqual(len(rec.buttons), 1, greeting)
            self.assertNotIn("No ONU found", rec.buttons[0][1], greeting)

    def test_search_by_mac_button_prints_the_format(self):
        rec = _RecBot()
        self._bot(rec).handle(_button("919000000001", "ask:mac"))
        self.assertEqual(rec.buttons, [])
        body = rec.texts[0][1]
        self.assertIn("a4:f2:1b:00:11:22", body)
        self.assertIn("3 characters", body)

    def test_search_by_name_button_prints_the_format(self):
        rec = _RecBot()
        self._bot(rec).handle(_button("919000000009", "ask:name"))
        body = rec.texts[0][1]
        self.assertIn("name", body.lower())
        self.assertIn("3 characters", body)

    def test_asking_the_format_needs_no_state_a_lookup_follows_directly(self):
        self._onu("ispA", self.olt, "onu-1", "a4:f2:1b:00:11:22", name="hc_kiran")
        rec = _RecBot()
        self._bot(rec).handle(_button("919000000001", "ask:mac"))
        self._bot(rec).handle(_text("919000000001", "a4f2"))
        self.assertIn("HILL-OLT-1", rec.buttons[0][1])

    def test_an_unhandled_message_type_offers_the_menu(self):
        rec = _RecBot()
        self._bot(rec).handle({"entry": [{"changes": [{"value": {"messages": [
            {"from": "919000000001", "type": "image", "image": {"id": "x"}}]}}]}]})
        self.assertEqual([b[0] for b in rec.buttons[0][2]], ["ask:mac", "ask:name"])

    def test_an_unknown_button_id_offers_the_menu(self):
        rec = _RecBot()
        self._bot(rec).handle(_button("919000000001", "bogus:7"))
        self.assertEqual(rec.texts, [])
        self.assertEqual([b[0] for b in rec.buttons[0][2]], ["ask:mac", "ask:name"])

    def test_a_greeting_from_an_unknown_number_stays_silent(self):
        rec = _RecBot()
        self._bot(rec).handle(_text("910000000000", "hi"))
        self.assertEqual((rec.texts, rec.buttons), ([], []))

    def test_lookup_is_scoped_to_the_senders_org(self):
        self._onu("ispB", self.olt_b, "b-onu", "de:ad:be:ef:00:01")
        rec = _RecBot()
        self._bot(rec).handle(_text("919000000001", "deadbeef"))
        self.assertEqual(rec.texts, [])
        body, btns = rec.buttons[0][1], rec.buttons[0][2]
        self.assertIn("No ONU found", body)
        self.assertEqual([b[0] for b in btns], ["ask:mac", "ask:name"])


    def test_owner_refresh_drives_the_scrape(self):
        sw = _FakeSweeper(eligible=[self.olt])
        rec = _RecBot()
        self._bot(rec, sw).handle(_button("919000000001", f"refresh:{self.olt}"))
        self.assertTrue(sw.done.wait(timeout=3))
        self.assertEqual([d["id"] for d in sw.scraped], [self.olt])
        self.assertIn("Reading", rec.texts[0][1])

    def test_worker_refresh_is_refused_no_scrape(self):
        sw = _FakeSweeper(eligible=[self.olt])
        rec = _RecBot()
        self._bot(rec, sw).handle(_button("919000000009", f"refresh:{self.olt}"))
        self.assertFalse(sw.done.wait(timeout=0.5))
        self.assertEqual(sw.scraped, [])
        self.assertIn("owner-only", rec.texts[0][1])

    def test_refresh_cross_org_id_is_refused(self):
        sw = _FakeSweeper(eligible=[self.olt_b])
        rec = _RecBot()
        self._bot(rec, sw).handle(_button("919000000001", f"refresh:{self.olt_b}"))
        self.assertEqual(sw.scraped, [])
        self.assertIn("isn't in your network", rec.texts[0][1])

    def test_map_button_returns_pin_when_placed(self):
        self.store.set_org_device_location("ispA", self.olt, 12.34, 56.78)
        rec = _RecBot()
        self._bot(rec).handle(_button("919000000001", f"map:{self.olt}"))
        body = rec.texts[0][1]
        self.assertIn("12.34,56.78", body)

    def test_recent_button_lists_outages(self):
        self.store.open_outage_if_absent("ispA", self.olt, _iso(self.now), "DOWN")
        rec = _RecBot()
        self._bot(rec).handle(_button("919000000001", f"recent:{self.olt}"))
        self.assertIn("Recent outages", rec.texts[0][1])


    def _assigned_outage(self, to=("wkr1",), org="ispA", device=None):
        device = self.olt if device is None else device
        self.store.open_outage_if_absent(org, device, _iso(self.now), "DOWN")
        oid = next(o["id"] for o in self.store.triage_outages(org)
                   if o["device_id"] == device)
        self.store.assign_outage(org, oid, list(to), "own1")
        return oid

    def test_accept_button_marks_the_worker_as_on_the_way(self):
        oid = self._assigned_outage()
        rec = _RecBot()
        self._bot(rec).handle(_button("919000000009", f"acc:{oid}"))
        row = next(o for o in self.store.triage_outages("ispA") if o["id"] == oid)
        self.assertEqual(row["accepted_by"], ["wkr1"])
        self.assertEqual(row["status"], "in_progress")
        self.assertIn("HILL-OLT-1", rec.texts[0][1])

    def test_accept_tells_whoever_assigned_it(self):
        oid = self._assigned_outage()
        rec = _RecBot()
        self._bot(rec).handle(_button("919000000009", f"acc:{oid}"))
        told = [t for t in rec.texts if t[0] == "919000000001"]
        self.assertTrue(told)
        self.assertIn("wkr1", told[0][1])

    def test_accepting_a_job_that_is_not_yours_changes_nothing(self):
        oid = self._assigned_outage(to=("wkr1",))
        rec = _RecBot()
        self._bot(rec).handle(_button("919000000001", f"acc:{oid}"))
        row = next(o for o in self.store.triage_outages("ispA") if o["id"] == oid)
        self.assertEqual(row["accepted_by"], [])
        self.assertIn("isn't assigned to you", rec.texts[0][1])

    def test_a_cross_org_outage_id_is_not_acceptable(self):
        oid = self._assigned_outage(to=("bown",), org="ispB", device=self.olt_b)
        rec = _RecBot()
        self._bot(rec).handle(_button("919000000009", f"acc:{oid}"))
        row = next(o for o in self.store.triage_outages("ispB") if o["id"] == oid)
        self.assertEqual(row["accepted_by"], [])
        self.assertNotIn("B-OLT", rec.texts[0][1])

    def test_a_second_tap_is_not_an_error(self):
        oid = self._assigned_outage()
        rec = _RecBot()
        bot = self._bot(rec)
        bot.handle(_button("919000000009", f"acc:{oid}"))
        self._bot(rec).handle(_button("919000000009", f"acc:{oid}"))
        self.assertIn("already accepted", rec.texts[-1][1])


if __name__ == "__main__":
    unittest.main()
