"""The pure metering math (billing v2). No store, no clock, no I/O.

metering.py is to billing what core/state_machine is to monitoring: counts and
rates in, an accrual row out. Everything that decides how much money a day
costs lives here, so it is testable without a database, and it is where the
rounding rule and the source ladder are pinned.
"""

import os
import sys
import unittest
from datetime import date

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.central import metering
from wisp.central.metering import Source

CONN = metering.DEFAULT_CONN_PAISE      # 300 = Rs 3 per ONU per month
FLOOR = metering.DEFAULT_FLOOR_PAISE    # 10000 = Rs 100 per device per month


class MonthMathTest(unittest.TestCase):
    def test_next_and_prev_roll_the_year(self):
        self.assertEqual(metering.next_month("2026-12"), "2027-01")
        self.assertEqual(metering.prev_month("2026-01"), "2025-12")
        self.assertEqual(metering.next_month("2026-07"), "2026-08")
        self.assertEqual(metering.prev_month("2026-08"), "2026-07")

    def test_days_in_month_knows_february(self):
        self.assertEqual(metering.days_in_month("2026-02"), 28)
        self.assertEqual(metering.days_in_month("2028-02"), 29)  # leap
        self.assertEqual(metering.days_in_month("2026-04"), 30)
        self.assertEqual(metering.days_in_month("2026-08"), 31)

    def test_month_of_day_and_labels(self):
        self.assertEqual(metering.month_of_day("2026-08-17"), "2026-08")
        self.assertEqual(metering.month_label("2026-08"), "August 2026")
        self.assertEqual(metering.month_key(date(2026, 8, 17)), "2026-08")


class DailyMoneyTest(unittest.TestCase):
    def test_the_connection_side_wins_when_it_is_larger(self):
        # 500 conns x Rs 3 = Rs 1500; 4 devices x Rs 100 = Rs 400.
        paise, side = metering.daily_paise(500, 4, CONN, FLOOR, 30)
        self.assertEqual(side, "conn")
        self.assertEqual(paise, round(500 * CONN / 30))

    def test_the_device_floor_wins_for_a_small_subscriber_base(self):
        # 10 conns x Rs 3 = Rs 30; 12 devices x Rs 100 = Rs 1200.
        paise, side = metering.daily_paise(10, 12, CONN, FLOOR, 30)
        self.assertEqual(side, "floor")
        self.assertEqual(paise, round(12 * FLOOR / 30))

    def test_a_tie_goes_to_the_connection_side(self):
        # Billed per connection is the headline story; the floor is a backstop,
        # so an exact tie must not report the floor as the reason.
        _, side = metering.daily_paise(1000, 3, 300, 100000, 30)
        self.assertEqual(side, "conn")

    def test_rounding_is_half_up_and_happens_once(self):
        # 1 conn x 300 paise over 31 days = 9.677..., which must land on 10.
        paise, _ = metering.daily_paise(1, 0, CONN, FLOOR, 31)
        self.assertEqual(paise, 10)
        # 1 x 300 over 8 days = 37.5 exactly: half goes UP.
        self.assertEqual(metering.daily_paise(1, 0, 300, 0, 8)[0], 38)

    def test_the_same_fleet_costs_the_same_every_month(self):
        """A per-connection month is a MONTH, not 30 days: February must not
        cost less than March for the same subscriber base. The division by
        days_in_month is what makes the monthly total land on the rate."""
        for month in ("2026-02", "2026-03", "2026-04", "2028-02"):
            days = metering.days_in_month(month)
            total = sum(metering.daily_paise(400, 2, CONN, FLOOR, days)[0]
                        for _ in range(days))
            # 400 connections x Rs 3 = Rs 1200 a month, within a rounding
            # paise per day.
            self.assertAlmostEqual(total, 400 * CONN, delta=days,
                                   msg=f"{month} drifted")

    def test_zero_everything_is_free_not_an_error(self):
        paise, side = metering.daily_paise(0, 0, CONN, FLOOR, 31)
        self.assertEqual(paise, 0)
        self.assertEqual(side, "conn")

    def test_negative_inputs_cannot_produce_a_negative_charge(self):
        paise, _ = metering.daily_paise(-5, -2, CONN, FLOOR, 31)
        self.assertEqual(paise, 0)

    def test_rate_overrides_are_honoured(self):
        # An org on a negotiated Rs 2 per connection with no device floor.
        paise, side = metering.daily_paise(600, 50, 200, 0, 30)
        self.assertEqual(side, "conn")
        self.assertEqual(paise, round(600 * 200 / 30))


class SourceLadderTest(unittest.TestCase):
    """The bill is PER ONU (2026-08-17). One measuring rung, and the latch on
    it is what stands between a broken walk and a moved bill."""

    def test_the_roster_is_the_count(self):
        count, source, flags, eff = metering.resolve_count(
            Source(True, 480, 0.1), None)
        self.assertEqual((count, source, eff), (480, "onu", "onu"))
        self.assertEqual(flags, {})

    def test_no_roster_at_all_is_an_honest_zero(self):
        count, source, _, eff = metering.resolve_count(None, None)
        self.assertEqual((count, source, eff), (0, "none", "none"))
        count, source, _, _ = metering.resolve_count(Source(False, 0, None), None)
        self.assertEqual((count, source), (0, "none"))

    def test_a_stale_roster_is_HELD_not_dropped(self):
        """An OLT fleet that stopped answering keeps its last good count for
        HOLD_DAYS. A walk that breaks must never silently move a bill — and
        with one rung there is nothing underneath to catch it."""
        count, source, flags, eff = metering.resolve_count(
            Source(True, 500, 3.0), None)
        self.assertEqual(count, 500)
        self.assertEqual(source, "held")
        self.assertEqual(flags["held"], "onu")
        # 'held' is a CONDITION of a rung, not a rung: the effective source is
        # still onu, so tomorrow's unchanged reading is not a "change".
        self.assertEqual(eff, "onu")

    def test_the_hold_expires_and_the_ladder_falls_through_FLAGGED(self):
        count, source, flags, eff = metering.resolve_count(
            Source(True, 500, metering.HOLD_DAYS + 1), "onu")
        self.assertEqual((count, source, eff), (0, "none", "none"))
        self.assertEqual(flags["downgraded"], {"from": "onu", "to": "none"})
        self.assertEqual(flags["source_changed"], {"from": "onu", "to": "none"})

    def test_a_roster_that_never_answered_is_skipped_not_believed_at_zero(self):
        """"We have not measured this org" is not "this org has no
        subscribers": the row must not claim a measured zero."""
        count, source, _, _ = metering.resolve_count(Source(True, 999, None), None)
        self.assertEqual((count, source), (0, "none"))

    def test_climbing_back_up_is_a_change_but_not_a_downgrade(self):
        _, _, flags, eff = metering.resolve_count(Source(True, 480, 0.1), "none")
        self.assertEqual(eff, "onu")
        self.assertIn("source_changed", flags)
        self.assertNotIn("downgraded", flags)

    def test_an_unchanged_source_raises_no_flag(self):
        _, _, flags, _ = metering.resolve_count(Source(True, 512, 0.1), "onu")
        self.assertEqual(flags, {})

    def test_the_RETIRED_basis_reads_as_a_change_never_a_downgrade(self):
        """The cutover day: yesterday's row says 'radius', today's says 'onu'.
        The basis moved because somebody decided it should, so it is news and
        not a fault — a downgrade chip would send the operator hunting for a
        broken feed that does not exist."""
        for retired in metering.RETIRED_CONN_SOURCES:
            _, source, flags, eff = metering.resolve_count(
                Source(True, 480, 0.1), retired)
            self.assertEqual((source, eff), ("onu", "onu"))
            self.assertEqual(flags["source_changed"],
                             {"from": retired, "to": "onu"})
            self.assertNotIn("downgraded", flags)

    def test_effective_source_resolves_a_held_row_to_its_rung(self):
        self.assertEqual(metering.effective_source("held", {"held": "onu"}), "onu")
        self.assertEqual(metering.effective_source("onu", {}), "onu")
        self.assertEqual(metering.effective_source("held", {}), "none")
        # A row the retired ladder wrote still resolves to what it was, so the
        # cutover is detected as a change rather than read as a fresh start.
        self.assertEqual(
            metering.effective_source("held", {"held": "radius"}), "radius")


class AccrualRowTest(unittest.TestCase):
    def test_accrue_day_carries_the_rate_it_was_charged_at(self):
        row = metering.accrue_day("2026-08-17", 400, "onu", {}, 12,
                                  CONN, FLOOR)
        self.assertEqual(row.day, "2026-08-17")
        self.assertEqual(row.conn_rate_paise, CONN)
        self.assertEqual(row.floor_paise, FLOOR)
        self.assertEqual(row.winning_side, "conn")
        self.assertEqual(row.paise,
                         metering.daily_paise(400, 12, CONN, FLOOR, 31)[0])

    def test_a_dormant_org_accrues_zero_not_an_error(self):
        row = metering.accrue_day("2026-08-17", 0, "none", {}, 0, CONN, FLOOR)
        self.assertEqual(row.paise, 0)
        self.assertEqual(row.conn_count, 0)
        self.assertEqual(row.device_count, 0)

    def test_carry_forward_repeats_the_counts_AND_the_rates(self):
        """A day computed late must not ride a rate that changed during the
        outage: rate changes apply forward only."""
        prior = metering.accrue_day("2026-08-16", 400, "onu", {}, 12,
                                    CONN, FLOOR)
        row = metering.carry_forward(prior, "2026-08-17")
        self.assertEqual(row.conn_count, 400)
        self.assertEqual(row.device_count, 12)
        self.assertEqual(row.conn_rate_paise, CONN)
        self.assertTrue(row.flags["backfilled"])

    def test_carry_forward_recharges_against_the_TARGET_month(self):
        # 31 August carried into 1 September: the same monthly figure divided
        # by 30 days, not 31.
        prior = metering.accrue_day("2026-08-31", 300, "onu", {}, 1,
                                    CONN, FLOOR)
        row = metering.carry_forward(prior, "2026-09-01")
        self.assertEqual(row.paise,
                         metering.daily_paise(300, 1, CONN, FLOOR, 30)[0])
        self.assertNotEqual(row.paise, prior.paise)

    def test_carry_forward_keeps_a_held_source_named(self):
        prior = metering.accrue_day("2026-08-16", 400, "held",
                                    {"held": "onu"}, 12, CONN, FLOOR)
        row = metering.carry_forward(prior, "2026-08-17")
        self.assertEqual(row.conn_source, "held")
        self.assertEqual(row.flags["held"], "onu")


class LadderTest(unittest.TestCase):
    def test_the_invoice_issues_on_the_first_of_the_next_month(self):
        # July's invoice issues 1 August, and that day is overdue day 1.
        self.assertEqual(metering.days_overdue("2026-07", date(2026, 8, 1)), 1)
        self.assertEqual(metering.days_overdue("2026-07", date(2026, 8, 4)), 4)

    def test_a_month_not_yet_closed_is_not_overdue(self):
        self.assertEqual(metering.days_overdue("2026-08", date(2026, 8, 20)), 0)

    def test_a_new_org_with_no_invoice_is_NEVER_locked(self):
        """Postpaid means outstanding is nonzero from day one of usage. The
        ladder anchors to the INVOICE so a signup on the 20th is not locked
        on the 23rd."""
        st = metering.ladder_stage(None, date(2026, 8, 23))
        self.assertEqual(st["stage"], "clear")
        self.assertFalse(st["locked"])

    def test_days_one_to_three_banner_then_day_four_locks(self):
        for day, stage in ((1, "banner"), (2, "banner"), (3, "banner"),
                           (4, "locked"), (30, "locked")):
            st = metering.ladder_stage("2026-07", date(2026, 8, day))
            self.assertEqual(st["stage"], stage, f"day {day}")
            self.assertEqual(st["locked"], stage == "locked", f"day {day}")

    def test_sixty_days_puts_the_org_on_the_deactivation_LIST(self):
        st = metering.ladder_stage("2026-06", date(2026, 8, 30))
        self.assertEqual(st["days_overdue"], 61)
        self.assertTrue(st["deactivation_candidate"])
        # ...and it is a LIST, not an action: the stage is still just locked.
        self.assertEqual(st["stage"], "locked")

    def test_one_day_short_of_sixty_is_not_a_candidate(self):
        st = metering.ladder_stage("2026-07", date(2026, 8, 1))
        self.assertFalse(st["deactivation_candidate"])

    def test_an_exempt_org_never_locks(self):
        st = metering.ladder_stage("2026-06", date(2026, 8, 30), exempt=True)
        self.assertEqual(st["stage"], "exempt")
        self.assertFalse(st["locked"])
        self.assertFalse(st["deactivation_candidate"])

    def test_a_deactivated_org_reads_deactivated(self):
        st = metering.ladder_stage("2026-06", date(2026, 8, 30),
                                   deactivated=True)
        self.assertEqual(st["stage"], "deactivated")
        self.assertTrue(st["locked"])


class CreditTest(unittest.TestCase):
    def test_the_projection_counts_days_at_the_current_rate(self):
        # Rs 100 credit against Rs 10 a day is ten days.
        out = metering.credit_lasts_until(10000, 1000, date(2026, 8, 17))
        self.assertEqual(out, date(2026, 8, 27))

    def test_no_credit_projects_nothing(self):
        self.assertIsNone(metering.credit_lasts_until(0, 1000, date(2026, 8, 17)))
        self.assertIsNone(metering.credit_lasts_until(-500, 1000, date(2026, 8, 17)))

    def test_credit_against_a_zero_rate_prints_NO_date(self):
        """Nothing is accruing, so the credit lasts forever. Printing a date
        would be a lie with a decimal point on it."""
        self.assertIsNone(metering.credit_lasts_until(10000, 0, date(2026, 8, 17)))


class VocabularyTest(unittest.TestCase):
    def test_the_stored_vocabularies_stay_closed(self):
        """These strings are written to billing_accruals and read by the SPA.
        Adding one is a schema decision, not a typo, so the set is pinned."""
        self.assertEqual(set(metering.CONN_SOURCES), {"onu", "held", "none"})
        self.assertEqual(set(metering.WINNING_SIDES), {"conn", "floor"})
        self.assertEqual(metering.SOURCE_LADDER, ("onu", "none"))

    def test_the_retired_rungs_stay_OUT_of_the_ladder(self):
        """RADIUS and the hand-typed declaration are readable history, not
        rungs. Putting either back means a bill can move between two answers
        by itself, which is the failure the single rung exists to prevent."""
        self.assertEqual(set(metering.RETIRED_CONN_SOURCES),
                         {"radius", "declared"})
        for retired in metering.RETIRED_CONN_SOURCES:
            self.assertNotIn(retired, metering.SOURCE_LADDER)
            self.assertNotIn(retired, metering.CONN_SOURCES)

    def test_every_resolved_source_is_in_the_vocabulary(self):
        cases = (
            (Source(True, 5, 0.1), None),
            (Source(True, 5, 3.0), None),
            (Source(True, 5, metering.HOLD_DAYS + 1), "onu"),
            (Source(True, 5, None), None),
            (Source(False, 0, None), "radius"),
            (None, None),
        )
        for onu, prior in cases:
            _, source, _, eff = metering.resolve_count(onu, prior)
            self.assertIn(source, metering.CONN_SOURCES)
            self.assertIn(eff, metering.SOURCE_LADDER)


if __name__ == "__main__":
    unittest.main()
