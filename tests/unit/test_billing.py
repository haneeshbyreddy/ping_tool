import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.central import billing
from wisp.central.store import CentralStore
from support import RecordingNotifier


def _utc(y, m, d, hh=12):
    return datetime(y, m, d, hh, 0, tzinfo=timezone.utc)


class MonthMathTest(unittest.TestCase):
    def test_next_month_and_rollover(self):
        self.assertEqual(billing.next_month("2026-07"), "2026-08")
        self.assertEqual(billing.next_month("2026-12"), "2027-01")

    def test_month_label(self):
        self.assertEqual(billing.month_label("2026-07"), "July 2026")


class ComputeStatusTest(unittest.TestCase):
    def test_free_never_locks(self):
        st = billing.compute_status("free", set(), _utc(2026, 7, 15))
        self.assertEqual(st["status"], "free")
        self.assertFalse(st["locked"])
        self.assertIsNone(st["due_month"])

    def test_unknown_plan_treated_as_free(self):
        st = billing.compute_status("", set(), _utc(2026, 7, 15))
        self.assertFalse(st["locked"])

    def test_unpaid_current_month_locks(self):
        st = billing.compute_status("pro", {"2026-06"}, _utc(2026, 7, 15))
        self.assertEqual(st["status"], "locked")
        self.assertTrue(st["locked"])
        self.assertEqual(st["due_month"], "2026-07")
        self.assertEqual(st["days_left"], 0)

    def test_active_mid_month(self):
        st = billing.compute_status("pro", {"2026-07"}, _utc(2026, 7, 15))
        self.assertEqual(st["status"], "active")
        self.assertEqual(st["paid_through"], "2026-07")
        self.assertEqual(st["due_month"], "2026-08")
        self.assertEqual(st["days_left"], 17)

    def test_due_soon_inside_three_days(self):
        # July 29/30/31 are the 3 days before an unpaid August 1st
        for day in (29, 30, 31):
            st = billing.compute_status("vip", {"2026-07"}, _utc(2026, 7, day))
            self.assertEqual(st["status"], "due_soon", day)
            self.assertFalse(st["locked"])
        st = billing.compute_status("vip", {"2026-07"}, _utc(2026, 7, 28))
        self.assertEqual(st["status"], "active")

    def test_prepaid_months_extend_the_runway(self):
        # Admin pre-marked Aug+Sep: no warning until Sep runs short (the
        # "no reminder this cycle" mechanism is just future paid months).
        st = billing.compute_status("pro", {"2026-07", "2026-08", "2026-09"},
                                    _utc(2026, 7, 30))
        self.assertEqual(st["status"], "active")
        self.assertEqual(st["paid_through"], "2026-09")
        self.assertEqual(st["due_month"], "2026-10")

    def test_gap_in_paid_months_ends_the_runway(self):
        # Paid July and September but not August: runway ends with July.
        st = billing.compute_status("pro", {"2026-07", "2026-09"}, _utc(2026, 7, 30))
        self.assertEqual(st["status"], "due_soon")
        self.assertEqual(st["due_month"], "2026-08")

    def test_year_rollover_runway(self):
        st = billing.compute_status("pro", {"2026-12", "2027-01"}, _utc(2026, 12, 30))
        self.assertEqual(st["status"], "active")
        self.assertEqual(st["paid_through"], "2027-01")


class MonthsToPayTest(unittest.TestCase):
    """What one Razorpay checkout buys: the next N unpaid months."""

    def test_locked_org_starts_at_current_month(self):
        now = _utc(2026, 7, 16)
        self.assertEqual(billing.months_to_pay("pro", set(), 3, now),
                         ["2026-07", "2026-08", "2026-09"])

    def test_active_org_extends_the_runway(self):
        now = _utc(2026, 7, 16)
        paid = {"2026-07", "2026-08"}
        self.assertEqual(billing.months_to_pay("pro", paid, 2, now),
                         ["2026-09", "2026-10"])

    def test_prepaid_island_is_skipped_not_double_billed(self):
        now = _utc(2026, 7, 16)
        paid = {"2026-07", "2026-09"}  # admin pre-marked September
        self.assertEqual(billing.months_to_pay("pro", paid, 2, now),
                         ["2026-08", "2026-10"])

    def test_year_rollover(self):
        now = _utc(2026, 11, 20)
        paid = {"2026-11", "2026-12"}
        self.assertEqual(billing.months_to_pay("vip", paid, 2, now),
                         ["2027-01", "2027-02"])


class RazorpaySignatureTest(unittest.TestCase):
    """The gateway's pure parts: HMAC verification + enabled gating. Order
    creation (the one network call) is exercised via a double in
    integration/test_central_billing."""

    def setUp(self):
        import hashlib as _hashlib
        import hmac as _hmac
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "c.db")
        from wisp.central.razorpay import RazorpayGateway
        self.gw = RazorpayGateway(self.store)
        self.sign = lambda msg, secret: _hmac.new(
            secret.encode(), msg.encode(), _hashlib.sha256).hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def test_disabled_until_both_keys_set(self):
        self.assertFalse(self.gw.enabled)
        self.store.set_setting("razorpay_key_id", "rzp_test_x")
        self.assertFalse(self.gw.enabled)
        self.store.set_setting("razorpay_key_secret", "sekrit")
        self.assertTrue(self.gw.enabled)

    def test_signature_roundtrip(self):
        self.store.set_setting("razorpay_key_secret", "sekrit")
        good = self.sign("order_1|pay_1", "sekrit")
        self.assertTrue(self.gw.verify_signature("order_1", "pay_1", good))
        self.assertFalse(self.gw.verify_signature("order_1", "pay_2", good))
        self.assertFalse(self.gw.verify_signature("order_1", "pay_1",
                                                  self.sign("order_1|pay_1", "wrong")))
        self.assertFalse(self.gw.verify_signature("order_1", "pay_1", ""))

    def test_no_secret_never_verifies(self):
        good = self.sign("order_1|pay_1", "sekrit")
        self.assertFalse(self.gw.verify_signature("order_1", "pay_1", good))


class SweeperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.tmp.name) / "c.db")
        self.store.set_org("ispA", name="Acme", ntfy_topic_owner="own-a")
        self.store.set_org_plan("ispA", "pro")
        self.notifier = RecordingNotifier()
        self.sweeper = billing.BillingSweeper(self.store, notifier=self.notifier)

    def tearDown(self):
        self.tmp.cleanup()

    def test_due_soon_pages_owner_once(self):
        self.store.set_billing_month("ispA", "2026-07", True)
        now = _utc(2026, 7, 30)
        self.assertEqual(self.sweeper.check(now),
                         [("ispA", "2026-08", "due_soon")])
        self.assertEqual(len(self.notifier.sent), 1)
        page = self.notifier.sent[0]
        self.assertEqual(page["recipient"], "own-a")
        self.assertIn("August 2026", page["body"])
        self.assertIn(billing.DEFAULT_GPAY_NUMBER, page["body"])
        # transition-only: a second sweep stays silent
        self.assertEqual(self.sweeper.check(now), [])
        self.assertEqual(len(self.notifier.sent), 1)

    def test_locked_pages_once_and_mentions_gpay(self):
        now = _utc(2026, 8, 2)
        self.assertEqual(self.sweeper.check(now), [("ispA", "2026-08", "locked")])
        self.assertIn("locked", self.notifier.sent[0]["title"])
        self.assertIn(billing.DEFAULT_GPAY_NUMBER, self.notifier.sent[0]["body"])
        self.assertEqual(self.sweeper.check(now), [])

    def test_failed_send_is_retried_next_sweep(self):
        failing = RecordingNotifier(ok=False)
        sweeper = billing.BillingSweeper(self.store, notifier=failing)
        now = _utc(2026, 8, 2)
        self.assertEqual(sweeper.check(now), [])
        self.assertEqual(len(failing.sent), 1)
        # still unpaid, send failed → retried (not stranded)
        sweeper.check(now)
        self.assertEqual(len(failing.sent), 2)

    def test_no_topic_is_skipped_not_retried(self):
        self.store.set_org("ispB", name="NoTopic")
        self.store.set_org_plan("ispB", "vip")
        now = _utc(2026, 8, 2)
        self.sweeper.check(now)
        recipients = [p["recipient"] for p in self.notifier.sent]
        self.assertNotIn(None, recipients)
        sent_before = len(self.notifier.sent)
        self.sweeper.check(now)
        self.assertEqual(len(self.notifier.sent), sent_before)

    def test_free_org_never_paged(self):
        self.store.set_org_plan("ispA", "free")
        self.assertEqual(self.sweeper.check(_utc(2026, 8, 2)), [])
        self.assertEqual(self.notifier.sent, [])

    def test_custom_gpay_number_rides_the_page(self):
        self.store.set_setting("billing_gpay_number", "9999999999")
        self.sweeper.check(_utc(2026, 8, 2))
        self.assertIn("9999999999", self.notifier.sent[0]["body"])


if __name__ == "__main__":
    unittest.main()
