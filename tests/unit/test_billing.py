"""Billing v2: the ledger engine, the 402 gate and the dunning ladder.

The pure math lives in tests/unit/test_metering.py. THIS file pins the glue:
what the sweeper writes, what org_locked answers, and what actually gets sent.

The load-bearing invariant, pinned twice below because it is the one a v1
reflex would break: the ladder anchors to the oldest OPEN INVOICE, never to
the outstanding balance. Postpaid means outstanding is nonzero from day one of
usage, so keying the lock on it would shut a brand new signup out on day 3.
"""

import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.central import billing, dunning, metering
from wisp.central.store import CentralStore
from wisp.config import Config
from support import RecordingNotifier

ORG = "ispA"


def _utc(y, m, d, hh=12):
    return datetime(y, m, d, hh, 0, tzinfo=timezone.utc)


class LedgerTestCase(unittest.TestCase):
    """A store with one org, one owner who has a WhatsApp number, and a
    recording notifier. Every subclass shares the same fixture shape."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # An EXPLICIT Config, never the global CONFIG: the ops number is read
        # from the environment there, and a developer with
        # WISP_WHATSAPP_ADMIN_NUMBER exported would give the superadmin digest
        # a recipient and turn every "pages exactly once" assertion red.
        self.cfg = Config(central_db=Path(self.tmp.name) / "c.db",
                          whatsapp_admin_number="")
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org(ORG, name="Acme")
        owner = self.store.add_user(ORG, "own1", "h", "s", "owner")
        self.store.set_user_whatsapp(owner, "919000000001")
        self.notifier = RecordingNotifier()
        self.sweeper = billing.BillingSweeper(self.store, self.cfg,
                                              notifier=self.notifier)

    def tearDown(self):
        self.tmp.cleanup()

    # -- fixture helpers ---------------------------------------------------

    def device(self, name="Tower A", device_type=None):
        return self.store.create_org_device(ORG, {
            "name": name, "ip_address": "10.0.0.1", "device_type": device_type,
            "region": "R", "parent_device_id": None})

    def accrue(self, day: str, paise: int, org: str = ORG, **kw):
        """Write one accrual row directly. Stamping money by hand keeps the
        ladder tests independent of what the counting feeds happen to do.

        The counts default to ZERO on purpose: a hand-stamped money row must
        not imply a fleet, or a later sweep's carry-forward would recompute
        the backfilled days off counts the test never meant to assert."""
        row = metering.AccrualRow(
            day=day, paise=paise, conn_count=kw.get("conn_count", 0),
            conn_source=kw.get("conn_source", "radius"),
            device_count=kw.get("device_count", 0),
            winning_side=kw.get("winning_side", "conn"),
            conn_rate_paise=metering.DEFAULT_CONN_PAISE,
            floor_paise=metering.DEFAULT_FLOOR_PAISE,
            flags=kw.get("flags", {}))
        return self.store.insert_accrual(org, row)

    def invoice(self, month: str, paise: int, org: str = ORG):
        """An invoice AND the accrual row behind it.

        The engine only ever issues an invoice from a month's summed accruals,
        so an invoice with no rows under it is a state production cannot
        reach. Seeding it bare once made a real balance read as zero and
        pushed a semantic change into the dunning gate; the fixture was the
        thing that was wrong."""
        self.accrue(f"{month}-15", paise, org)
        self.store.ensure_invoice(org, month, paise)


class GateTest(LedgerTestCase):
    """server.py's 402. Two indexed point reads on every /api request."""

    def test_a_brand_new_org_is_NEVER_locked(self):
        self.assertFalse(billing.org_locked(self.store, ORG, _utc(2026, 8, 23)))

    def test_an_org_that_OWES_MONEY_but_has_no_invoice_is_not_locked(self):
        """Sign up on the 20th: 11 days accrue before the month closes. The
        outstanding balance is real, the invoice does not exist yet, and the
        dashboard must stay open."""
        for d in range(20, 31):
            self.accrue(f"2026-08-{d:02d}", 5000)
        self.assertGreater(self.store.outstanding_paise(ORG), 0)
        self.assertFalse(billing.org_locked(self.store, ORG, _utc(2026, 8, 31)))

    def test_the_first_possible_lock_is_the_fourth(self):
        self.invoice("2026-07", 120000)
        for day, locked in ((1, False), (2, False), (3, False),
                            (4, True), (31, True)):
            self.assertEqual(
                billing.org_locked(self.store, ORG, _utc(2026, 8, day)),
                locked, f"August {day}")

    def test_paying_the_invoice_unlocks_immediately(self):
        self.invoice("2026-07", 120000)
        self.assertTrue(billing.org_locked(self.store, ORG, _utc(2026, 8, 10)))
        self.store.record_payment(ORG, 120000, "manual", note="bank transfer",
                                  recorded_by="admin")
        self.store.settle_invoices(ORG)
        self.assertFalse(billing.org_locked(self.store, ORG, _utc(2026, 8, 10)))

    def test_a_partial_payment_does_NOT_unlock(self):
        self.invoice("2026-07", 120000)
        self.store.record_payment(ORG, 50000, "manual", recorded_by="admin")
        self.store.settle_invoices(ORG)
        self.assertTrue(billing.org_locked(self.store, ORG, _utc(2026, 8, 10)))

    def test_an_exempt_org_never_locks(self):
        self.invoice("2026-06", 500000)
        self.store.set_org_billing_flags(ORG, exempt=True)
        self.assertFalse(billing.org_locked(self.store, ORG, _utc(2026, 9, 30)))

    def test_a_deactivated_org_is_locked(self):
        self.store.set_org_billing_flags(ORG, deactivated=True)
        self.assertTrue(billing.org_locked(self.store, ORG, _utc(2026, 8, 1)))

    def test_a_voided_invoice_stops_holding_the_lock(self):
        self.invoice("2026-07", 120000)
        self.assertTrue(billing.org_locked(self.store, ORG, _utc(2026, 8, 10)))
        self.store.set_invoice_status(ORG, "2026-07", "void")
        self.assertFalse(billing.org_locked(self.store, ORG, _utc(2026, 8, 10)))


class StatusTest(LedgerTestCase):
    """The one document the hero, the banner and the locked screen read."""

    def test_a_fresh_org_reads_clear_and_owes_nothing(self):
        st = billing.org_status(self.store, ORG, _utc(2026, 8, 17))
        self.assertEqual(st["stage"], "clear")
        self.assertFalse(st["locked"])
        self.assertEqual(st["outstanding_paise"], 0)
        self.assertEqual(st["credit_paise"], 0)
        self.assertIsNone(st["open_invoice"])
        self.assertIsNone(st["credit_lasts_until"])

    def test_the_stages_walk_the_ladder(self):
        self.invoice("2026-07", 120000)
        for day, stage in ((1, "banner"), (3, "banner"), (4, "locked")):
            st = billing.org_status(self.store, ORG, _utc(2026, 8, day))
            self.assertEqual(st["stage"], stage, f"August {day}")
            self.assertEqual(st["days_overdue"], day)

    def test_sixty_days_flags_a_deactivation_candidate_but_deactivates_nothing(self):
        self.invoice("2026-06", 120000)
        st = billing.org_status(self.store, ORG, _utc(2026, 8, 30))
        self.assertTrue(st["deactivation_candidate"])
        self.assertFalse(st["deactivated"])
        self.assertEqual(self.store.org_billing(ORG)["deactivated"], False)

    def test_overpaying_reads_as_CREDIT_with_a_projection(self):
        self.accrue("2026-08-17", 1000)
        self.store.record_payment(ORG, 11000, "manual", recorded_by="admin")
        st = billing.org_status(self.store, ORG, _utc(2026, 8, 17))
        self.assertEqual(st["outstanding_paise"], -10000)
        self.assertEqual(st["credit_paise"], 10000)
        # Rs 100 credit against Rs 10 a day is ten more days.
        self.assertEqual(st["credit_lasts_until"], "2026-08-27")

    def test_credit_with_nothing_accruing_projects_NO_date(self):
        self.store.record_payment(ORG, 10000, "manual", recorded_by="admin")
        st = billing.org_status(self.store, ORG, _utc(2026, 8, 17))
        self.assertEqual(st["credit_paise"], 10000)
        self.assertIsNone(st["credit_lasts_until"])

    def test_the_rates_report_whether_they_are_overridden(self):
        st = billing.org_status(self.store, ORG, _utc(2026, 8, 17))
        self.assertEqual(st["rates"]["conn_paise"], metering.DEFAULT_CONN_PAISE)
        self.assertFalse(st["rates"]["conn_override"])
        self.store.set_org_billing_rates(ORG, conn_rate_paise=200,
                                         floor_paise=None)
        st = billing.org_status(self.store, ORG, _utc(2026, 8, 17))
        self.assertEqual(st["rates"]["conn_paise"], 200)
        self.assertTrue(st["rates"]["conn_override"])
        self.assertFalse(st["rates"]["floor_override"])

    def test_a_global_rate_change_needs_no_restart(self):
        self.store.set_setting("billing_conn_paise", "250")
        st = billing.org_status(self.store, ORG, _utc(2026, 8, 17))
        self.assertEqual(st["rates"]["conn_paise"], 250)


class AccrualSweepTest(LedgerTestCase):
    def test_one_row_per_day_and_re_running_writes_nothing(self):
        self.device()
        now = _utc(2026, 8, 17)
        first = self.sweeper.sweep(now)
        self.assertEqual(first["accrued"][ORG], ["2026-08-17"])
        again = self.sweeper.sweep(now)
        self.assertEqual(again["accrued"], {})
        self.assertEqual(len(self.store.accruals_for_month(ORG, "2026-08")), 1)

    def test_a_dormant_org_accrues_a_zero_row_not_nothing(self):
        """Zero devices and no declaration is a real answer, and the row is
        what proves the sweep ran that day."""
        row = self.sweeper.sweep(_utc(2026, 8, 17))
        self.assertEqual(row["accrued"][ORG], ["2026-08-17"])
        today = self.store.accrual_on(ORG, "2026-08-17")
        self.assertEqual(today["paise"], 0)
        self.assertEqual(today["conn_source"], "none")

    def test_the_device_floor_applies_with_no_subscribers(self):
        for i in range(4):
            self.device(f"Tower {i}")
        self.sweeper.sweep(_utc(2026, 8, 17))
        today = self.store.accrual_on(ORG, "2026-08-17")
        self.assertEqual(today["device_count"], 4)
        self.assertEqual(today["winning_side"], "floor")
        self.assertEqual(today["paise"],
                         metering.daily_paise(0, 4, 300, 10000, 31)[0])

    def test_passives_never_count_as_monitored_devices(self):
        self.device("Tower A")
        self.device("Splitter 1", device_type="splitter")
        self.sweeper.sweep(_utc(2026, 8, 17))
        self.assertEqual(
            self.store.accrual_on(ORG, "2026-08-17")["device_count"], 1)

    def test_a_gap_is_BACKFILLED_and_flagged(self):
        self.device()
        self.sweeper.sweep(_utc(2026, 8, 14))
        # Central was down for three days.
        out = self.sweeper.sweep(_utc(2026, 8, 17))
        self.assertEqual(out["accrued"][ORG],
                         ["2026-08-14", "2026-08-15", "2026-08-16",
                          "2026-08-17"][1:])
        for day in ("2026-08-15", "2026-08-16"):
            row = self.store.accrual_on(ORG, day)
            self.assertTrue(row["flags"]["backfilled"], day)
            self.assertEqual(row["device_count"], 1)

    def test_an_exempt_org_accrues_NOTHING(self):
        self.device()
        self.store.set_org_billing_flags(ORG, exempt=True)
        out = self.sweeper.sweep(_utc(2026, 8, 17))
        self.assertEqual(out["accrued"], {})
        self.assertIsNone(self.store.accrual_on(ORG, "2026-08-17"))

    def test_a_deactivated_org_accrues_NOTHING(self):
        self.device()
        self.store.set_org_billing_flags(ORG, deactivated=True)
        self.sweeper.sweep(_utc(2026, 8, 17))
        self.assertIsNone(self.store.accrual_on(ORG, "2026-08-17"))

    def test_clearing_exempt_RE_ANCHORS_instead_of_backfilling_the_hole(self):
        """The days an org spent exempt are a deliberate hole in the ledger.
        The backfill pass must not charge across them as if central had merely
        been down."""
        self.device()
        self.sweeper.sweep(_utc(2026, 8, 1))
        self.store.set_org_billing_flags(ORG, exempt=True)
        self.store.set_org_billing_flags(ORG, exempt=False,
                                         resume_day="2026-08-17")
        self.sweeper.sweep(_utc(2026, 8, 17))
        for day in range(2, 17):
            self.assertIsNone(self.store.accrual_on(ORG, f"2026-08-{day:02d}"),
                              f"August {day} should stay a hole")
        self.assertIsNotNone(self.store.accrual_on(ORG, "2026-08-17"))

    def test_a_bad_org_cannot_stop_the_others(self):
        self.store.set_org("ispB", name="Beta")
        self.device()
        real = self.store.org_monitored_device_count

        def explode(org_id, passives):
            if org_id == ORG:
                raise RuntimeError("boom")
            return real(org_id, passives)

        self.store.org_monitored_device_count = explode
        out = self.sweeper.sweep(_utc(2026, 8, 17))
        self.assertNotIn(ORG, out["accrued"])
        self.assertEqual(out["accrued"]["ispB"], ["2026-08-17"])


class InvoiceTest(LedgerTestCase):
    def test_a_finished_month_closes_into_ONE_invoice(self):
        for d in (15, 16, 17):
            self.accrue(f"2026-07-{d}", 4000)
        out = self.sweeper.sweep(_utc(2026, 8, 2))
        self.assertEqual(out["invoiced"][ORG], ["2026-07"])
        inv = self.store.org_invoice(ORG, "2026-07")
        self.assertEqual(inv["paise"], 12000)
        self.assertEqual(inv["status"], "open")

    def test_the_invoice_is_the_SUM_of_its_rows_and_is_never_recomputed(self):
        for d in (15, 16, 17):
            self.accrue(f"2026-07-{d}", 3333)
        self.sweeper.sweep(_utc(2026, 8, 2))
        rows = self.store.accruals_for_month(ORG, "2026-07")
        self.assertEqual(self.store.org_invoice(ORG, "2026-07")["paise"],
                         sum(r["paise"] for r in rows))

    def test_closing_twice_does_not_double_invoice(self):
        self.accrue("2026-07-15", 4000)
        self.sweeper.sweep(_utc(2026, 8, 2))
        out = self.sweeper.sweep(_utc(2026, 8, 3))
        self.assertEqual(out["invoiced"], {})
        self.assertEqual(len(self.store.org_invoices(ORG)), 1)

    def test_the_CURRENT_month_is_never_invoiced(self):
        self.accrue("2026-08-01", 4000)
        self.sweeper.sweep(_utc(2026, 8, 17))
        self.assertIsNone(self.store.org_invoice(ORG, "2026-08"))

    def test_a_zero_month_gets_no_invoice_and_no_ladder(self):
        self.accrue("2026-07-15", 0)
        self.sweeper.sweep(_utc(2026, 8, 10))
        self.assertEqual(self.store.org_invoices(ORG), [])
        self.assertFalse(billing.org_locked(self.store, ORG, _utc(2026, 8, 10)))

    def test_several_missed_months_all_close(self):
        self.accrue("2026-05-15", 4000)
        self.accrue("2026-06-15", 5000)
        self.accrue("2026-07-15", 6000)
        out = self.sweeper.sweep(_utc(2026, 8, 2))
        self.assertEqual(out["invoiced"][ORG], ["2026-05", "2026-06", "2026-07"])

    def test_payments_settle_invoices_OLDEST_first(self):
        self.invoice("2026-06", 10000)
        self.invoice("2026-07", 10000)
        self.store.record_payment(ORG, 10000, "manual", recorded_by="admin")
        self.store.settle_invoices(ORG)
        self.assertEqual(self.store.org_invoice(ORG, "2026-06")["status"], "paid")
        self.assertEqual(self.store.org_invoice(ORG, "2026-07")["status"], "open")
        self.assertEqual(self.store.oldest_open_invoice(ORG)["month"], "2026-07")

    def test_settling_is_idempotent(self):
        self.invoice("2026-06", 10000)
        self.store.record_payment(ORG, 10000, "manual", recorded_by="admin")
        for _ in range(3):
            self.store.settle_invoices(ORG)
        self.assertEqual(self.store.org_invoice(ORG, "2026-06")["status"], "paid")

    def test_an_exempt_org_still_closes_months_it_already_accrued(self):
        """Usage that happened before the flag flipped stays owed."""
        self.accrue("2026-07-15", 4000)
        self.store.set_org_billing_flags(ORG, exempt=True)
        out = self.sweeper.sweep(_utc(2026, 8, 2))
        self.assertEqual(out["invoiced"][ORG], ["2026-07"])


class PaymentLedgerTest(LedgerTestCase):
    def test_a_replayed_gateway_payment_is_a_no_op(self):
        first = self.store.record_payment(
            ORG, 50000, "gateway", provider="razorpay",
            provider_payment_id="pay_ABC")
        replay = self.store.record_payment(
            ORG, 50000, "gateway", provider="razorpay",
            provider_payment_id="pay_ABC")
        self.assertIsNotNone(first)
        self.assertIsNone(replay)
        self.assertEqual(self.store.sum_paid(ORG), 50000)

    def test_an_adjustment_may_be_negative_but_a_payment_may_not(self):
        self.store.record_payment(ORG, -2500, "adjustment", note="dispute",
                                  recorded_by="admin")
        self.assertEqual(self.store.sum_paid(ORG), -2500)
        with self.assertRaises(ValueError):
            self.store.record_payment(ORG, -100, "manual", recorded_by="admin")
        with self.assertRaises(ValueError):
            self.store.record_payment(ORG, 0, "adjustment", recorded_by="admin")

    def test_an_unknown_payment_kind_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.record_payment(ORG, 100, "cheque")

    def test_outstanding_is_accruals_minus_payments(self):
        self.accrue("2026-08-16", 4000)
        self.accrue("2026-08-17", 4000)
        self.store.record_payment(ORG, 5000, "manual", recorded_by="admin")
        self.assertEqual(self.store.outstanding_paise(ORG), 3000)


class DunningTest(LedgerTestCase):
    """What actually gets SENT. The kinds vocabulary is dunning's own, so
    these assert on behaviour (how many pages, to whom, and whether a second
    sweep repeats itself) rather than on the kind strings."""

    def pages(self, now):
        return dunning.sweep(self.store, self.sweeper.cfg, self.notifier, now)

    def test_a_fresh_org_with_no_invoice_is_never_paged(self):
        self.assertEqual(self.pages(_utc(2026, 8, 23)), [])
        self.assertEqual(self.notifier.sent, [])

    def test_an_org_that_owes_money_with_no_invoice_is_never_paged(self):
        for d in range(20, 31):
            self.accrue(f"2026-08-{d:02d}", 5000)
        self.assertEqual(self.pages(_utc(2026, 8, 31)), [])

    def test_an_overdue_invoice_pages_the_owner_once(self):
        self.invoice("2026-07", 120000)
        sent = self.pages(_utc(2026, 8, 1))
        self.assertTrue(sent, "an overdue invoice must page somebody")
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0]["whatsapp"], ["919000000001"])
        # ...and the same rung never pages twice.
        self.assertEqual(self.pages(_utc(2026, 8, 1)), [])
        self.assertEqual(len(self.notifier.sent), 1)

    def test_a_failed_send_is_retried_next_sweep(self):
        failing = RecordingNotifier(ok=False)
        self.invoice("2026-07", 120000)
        now = _utc(2026, 8, 1)
        dunning.sweep(self.store, self.sweeper.cfg, failing, now)
        self.assertEqual(len(failing.sent), 1)
        dunning.sweep(self.store, self.sweeper.cfg, failing, now)
        self.assertEqual(len(failing.sent), 2)

    def test_an_org_in_credit_is_never_paged(self):
        """The projection IS the no-reminder switch."""
        self.invoice("2026-07", 10000)
        self.store.record_payment(ORG, 50000, "manual", recorded_by="admin")
        self.store.settle_invoices(ORG)
        self.assertEqual(self.pages(_utc(2026, 8, 5)), [])
        self.assertEqual(self.notifier.sent, [])

    def test_exempt_and_deactivated_orgs_are_never_paged(self):
        self.invoice("2026-06", 120000)
        self.store.set_org_billing_flags(ORG, exempt=True)
        self.assertEqual(self.pages(_utc(2026, 8, 20)), [])
        self.store.set_org_billing_flags(ORG, exempt=False, deactivated=True)
        self.assertEqual(self.pages(_utc(2026, 8, 20)), [])
        self.assertEqual(self.notifier.sent, [])

    def test_the_amount_and_the_month_ride_the_page(self):
        self.invoice("2026-07", 184700)
        self.pages(_utc(2026, 8, 1))
        text = " ".join(str(v) for v in self.notifier.sent[0].values())
        self.assertIn("1,847", text)
        self.assertIn("July 2026", text)

    def test_no_page_carries_a_prose_em_dash(self):
        """The house copy rule: periods, colons and the middle dot."""
        self.invoice("2026-07", 120000)
        self.pages(_utc(2026, 8, 1))
        for page in self.notifier.sent:
            self.assertNotIn("—", f"{page['title']} {page['body']}")

    def test_a_send_can_never_crash_the_sweep(self):
        class Exploding:
            channel = "whatsapp"

            def send(self, *a, **kw):
                raise RuntimeError("meta is down")

        self.invoice("2026-07", 120000)
        # No assertion beyond "it returns": nothing may propagate.
        dunning.sweep(self.store, self.sweeper.cfg, Exploding(),
                      _utc(2026, 8, 1))

    def test_the_sweeper_survives_a_dunning_failure(self):
        self.device()
        original = dunning.sweep
        try:
            dunning.sweep = lambda *a, **kw: (_ for _ in ()).throw(
                RuntimeError("boom"))
            out = self.sweeper.sweep(_utc(2026, 8, 17))
        finally:
            dunning.sweep = original
        self.assertEqual(out["accrued"][ORG], ["2026-08-17"])


class StandDownTest(LedgerTestCase):
    """Deactivation is the ONE place billing touches the monitoring path, and
    the line between it and the overdue ladder is the whole safety argument.

    `/edge/devices` is the choke point: an empty topology means the edge polls
    nothing and pages nobody, while the node keeps heartbeating so it stays
    live and updatable."""

    def topology_for(self, org=ORG):
        return self.store.org_device_topology(org)

    def test_a_LOCKED_org_is_still_fully_monitored(self):
        """A lapsed bill must not silence an alarm. However overdue an account
        gets, the devices keep being handed to the probe."""
        self.device("Tower A")
        self.invoice("2026-06", 500000)
        self.assertTrue(billing.org_locked(self.store, ORG, _utc(2026, 8, 30)))
        self.assertEqual(len(self.topology_for()), 1)
        self.assertFalse(self.store.org_billing(ORG)["deactivated"])

    def test_deactivation_is_the_only_thing_that_stands_probes_down(self):
        self.device("Tower A")
        self.store.set_org_billing_flags(ORG, deactivated=True)
        # The topology itself is untouched: nothing is deleted, and turning the
        # org back on restores monitoring with no re-entry of the network.
        self.assertEqual(len(self.topology_for()), 1)
        # ...the edge route is what withholds it. Pinned in
        # integration/test_central_billing at the HTTP layer.
        self.assertTrue(self.store.org_billing(ORG)["deactivated"])

    def test_reactivating_restores_both_the_meter_and_the_probe(self):
        self.device("Tower A")
        self.store.set_org_billing_flags(ORG, deactivated=True)
        self.sweeper.sweep(_utc(2026, 8, 17))
        self.assertIsNone(self.store.accrual_on(ORG, "2026-08-17"))
        self.store.set_org_billing_flags(ORG, deactivated=False,
                                         resume_day="2026-08-18")
        self.sweeper.sweep(_utc(2026, 8, 18))
        self.assertIsNotNone(self.store.accrual_on(ORG, "2026-08-18"))
        self.assertFalse(self.store.org_billing(ORG)["deactivated"])


class CutoverBackfillTest(LedgerTestCase):
    """tools/billing_backfill_month.py: the one-shot that charges the current
    month from its 1st at go-live, because the ledger starts empty and the
    first sweep would otherwise bill only from the day central restarted.

    It stays a TOOL and never becomes engine behaviour: `accrue_org` must keep
    starting a new org on the day it appears, or a signup on the 20th would be
    charged for the 19 days before it existed."""

    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        import billing_backfill_month
        self.tool = billing_backfill_month
        self.now = _utc(2026, 8, 17)
        self.today = billing.operator_today(self.cfg, self.now)

    def plan(self, org_id=ORG, month="2026-08"):
        row = next(o for o in self.store.billing_org_rows()
                   if o["org_id"] == org_id)
        return self.tool.plan_org(self.sweeper, row, month, self.today, self.now)

    def test_it_fills_the_first_to_today_and_stops(self):
        self.device("Tower A")
        plan = self.plan()
        self.assertEqual(len(plan["rows"]), 17)
        self.assertEqual(plan["rows"][0].day, "2026-08-01")
        self.assertEqual(plan["rows"][-1].day, "2026-08-17")

    def test_it_never_bills_a_day_that_has_not_happened(self):
        self.device("Tower A")
        days = [r.day for r in self.plan()["rows"]]
        self.assertNotIn("2026-08-18", days)
        self.assertNotIn("2026-08-31", days)

    def test_it_never_rewrites_a_day_already_billed(self):
        """Re-running must be a no-op: an accrual once written is never
        rewritten, or the invoice would stop equalling its rows."""
        self.device("Tower A")
        self.accrue("2026-08-05", 4242)
        plan = self.plan()
        self.assertIn("2026-08-05", plan["skipped"])
        self.assertNotIn("2026-08-05", [r.day for r in plan["rows"]])
        for row in plan["rows"]:
            self.store.insert_accrual(ORG, row)
        self.assertEqual(self.store.accrual_on(ORG, "2026-08-05")["paise"], 4242)
        self.assertEqual(self.plan()["rows"], [])

    def test_every_backfilled_row_is_stamped_as_a_CUTOVER(self):
        """Distinguishable from ordinary after-downtime carry-forward, so the
        one month that was priced at a later day's rate is auditable."""
        self.device("Tower A")
        for row in self.plan()["rows"]:
            self.assertTrue(row.flags["backfilled"])
            self.assertEqual(row.flags["cutover"], "2026-08")

    def test_exempt_and_deactivated_orgs_are_skipped_entirely(self):
        self.device("Tower A")
        self.store.set_org_billing_flags(ORG, exempt=True)
        self.assertIsNone(self.plan())
        self.store.set_org_billing_flags(ORG, exempt=False, deactivated=True)
        self.assertIsNone(self.plan())

    def test_it_prices_the_month_the_way_tonight_will_price_it(self):
        """The backfill reads counts through the sweeper's own feeds, so a
        cutover day and the next real day cannot disagree on the rate."""
        for i in range(4):
            self.device(f"Tower {i}")
        plan = self.plan()
        self.sweeper.sweep(self.now)
        tonight = self.store.accrual_on(ORG, "2026-08-17")
        same_day = [r for r in plan["rows"] if r.day == "2026-08-17"][0]
        self.assertEqual(same_day.paise, tonight["paise"])
        self.assertEqual(same_day.device_count, tonight["device_count"])


class SpaAgreementTest(unittest.TestCase):
    """The SPA mirrors the ladder's day math and the source vocabulary so the
    banner can decide what to draw without a round trip. Nothing enforces the
    mirror at runtime, so it is pinned here: a banner that disagrees with the
    gate tells an owner they have days left on the morning they are locked
    out. Same discipline as fiber.ts and mapdetail."""

    def setUp(self):
        root = Path(__file__).resolve().parents[2] / "web" / "src"
        self.lock = (root / "components" / "billing-lock.tsx").read_text()
        self.lib = (root / "lib" / "billing.ts").read_text()

    def test_the_banner_window_matches(self):
        found = re.search(r"const BANNER_DAYS = (\d+)", self.lock)
        self.assertIsNotNone(found, "BANNER_DAYS not found in billing-lock.tsx")
        self.assertEqual(int(found.group(1)), metering.BANNER_DAYS)

    def test_every_stored_conn_source_has_SPA_copy(self):
        for source in metering.CONN_SOURCES:
            self.assertIn(f'case "{source}":', self.lib,
                          f"connSourceMeta has no case for {source!r}")

    def test_a_RETIRED_source_is_labelled_not_dropped(self):
        """A row the old RADIUS ladder wrote carries a real measured count.
        Letting it fall to the "No source" branch would turn "billed on a
        basis we no longer use" into "we could not count you", which are
        opposite findings that take opposite actions."""
        for retired in metering.RETIRED_CONN_SOURCES:
            self.assertIn(f'{retired}:', self.lib,
                          f"connSourceMeta cannot label a {retired!r} row")

    def test_every_ladder_stage_has_SPA_copy(self):
        for stage in ("deactivated", "locked", "banner", "exempt"):
            self.assertIn(f'case "{stage}":', self.lib,
                          f"stageMeta has no case for {stage!r}")


class OperatorDayTest(LedgerTestCase):
    def test_the_billing_day_is_the_OPERATOR_day_not_utc(self):
        """22:00 UTC is already tomorrow in Asia/Kolkata, and the ledger uses
        the operator's calendar (the worker-tracking precedent)."""
        late = datetime(2026, 8, 16, 22, 0, tzinfo=timezone.utc)
        self.assertEqual(billing.operator_today(self.sweeper.cfg, late),
                         (late + timedelta(hours=5, minutes=30)).date())


if __name__ == "__main__":
    unittest.main()
