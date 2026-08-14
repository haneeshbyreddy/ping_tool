import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(_TESTS_DIR), "src"))
sys.path.insert(0, _TESTS_DIR)

from wisp.central import history
from wisp.central.store import CentralStore
from wisp.central.store_proxy import PROXY_AUDIT_KEEP_DAYS
from wisp.config import Config

ORG = "ispA"
NODE = "node1"


def _iso(days_ago: float) -> str:
    # Stamped at CALL time, never at import: discovery imports every test file
    # up front, so an import-time "now" is stale by the time the file runs.
    return (datetime.now(timezone.utc)
            - timedelta(days=days_ago)).isoformat(timespec="seconds")


class RetentionTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(central_db=Path(self.tmp.name) / "central.db")
        self.store = CentralStore(self.cfg.central_db)
        self.store.set_org(ORG, "Isp A")
        self.dev = self.store.create_org_device(ORG, {
            "name": "OLT-1", "ip_address": "10.0.0.2", "device_type": "olt",
            "region": None, "parent_device_id": None})

    def tearDown(self):
        self.tmp.cleanup()

    def _rows(self, table, col="id"):
        with self.store._connect() as conn:
            return [r[0] for r in conn.execute(
                f"SELECT {col} FROM {table} ORDER BY id")]

    def _count(self, table):
        with self.store._connect() as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def _outage(self, resolved: bool, started="2026-01-01T00:00:00+00:00"):
        with self.store._write_lock, self.store._connect() as conn:
            cur = conn.execute(
                "INSERT INTO outages (org_id, device_id, started_at, resolved_at,"
                " final_state) VALUES (?,?,?,?, 'DOWN')",
                (ORG, self.dev, started,
                 "2026-01-02T00:00:00+00:00" if resolved else None))
            conn.commit()
            return cur.lastrowid


class ProxyAuditPruneTest(RetentionTestBase):
    def _audit(self, ts):
        with self.store._write_lock, self.store._connect() as conn:
            conn.execute(
                "INSERT INTO proxy_audit (sid, org_id, device_id, user_id,"
                " method, path, status, ts) VALUES ('s',?,?,NULL,'GET','/',200,?)",
                (ORG, self.dev, ts))
            conn.commit()

    def test_keep_days_cut_to_a_fortnight(self):
        self.assertEqual(PROXY_AUDIT_KEEP_DAYS, 14)

    def test_only_rows_past_the_cutoff_go(self):
        self._audit(_iso(20))
        self._audit(_iso(1))
        n = self.store.prune_proxy_audit(_iso(14))
        self.assertEqual(n, 1)
        self.assertEqual(self._count("proxy_audit"), 1)

    def test_creating_a_session_no_longer_prunes(self):
        # The whole point of the move: the prune must not depend on somebody
        # opening a tunnel.
        self._audit(_iso(400))
        self.store.create_proxy_session(
            "sid1", ORG, self.dev, NODE, None, _iso(-1))
        self.assertEqual(self._count("proxy_audit"), 1)

    def test_the_maintenance_loop_prunes_it(self):
        self._audit(_iso(400))
        self._audit(_iso(1))
        removed = history.prune_ledgers(self.store)
        self.assertEqual(removed.get("proxy_audit"), 1)
        self.assertEqual(self._count("proxy_audit"), 1)


class EventsPruneTest(RetentionTestBase):
    def _event(self, received_at, edge_id):
        with self.store._write_lock, self.store._connect() as conn:
            conn.execute(
                "INSERT INTO events (org_id, node_id, edge_id, type, payload,"
                " received_at) VALUES (?,?,?, 'DEVICE_DOWN', '{}', ?)",
                (ORG, NODE, edge_id, received_at))
            conn.commit()

    def test_old_events_go_and_fresh_ones_stay(self):
        self._event(_iso(120), 1)
        self._event(_iso(5), 2)
        removed = history.prune_ledgers(self.store)
        self.assertEqual(removed.get("events"), 1)
        self.assertEqual(self._rows("events", "edge_id"), [2])

    def test_edge_id_allocation_survives_a_prune(self):
        # edge_id is MAX(edge_id)+1, so ageing rows out must never reissue one
        # (the UNIQUE(org_id, node_id, edge_id) would reject the insert).
        self._event(_iso(120), 1)
        self._event(_iso(1), 2)
        history.prune_ledgers(self.store)
        with self.store._write_lock, self.store._connect() as conn:
            nxt = conn.execute(
                "SELECT COALESCE(MAX(edge_id), 0) + 1 FROM events"
                " WHERE org_id=? AND node_id=?", (ORG, NODE)).fetchone()[0]
        self.assertEqual(nxt, 3)


class AlertLogPruneTest(RetentionTestBase):
    def _log(self, ts, outage_id=None, payload="{}", kind="PORT_DOWN"):
        self.store.log_alert(ORG, outage_id, self.dev, "whatsapp", "+1",
                             "sent", payload, ts, kind)

    def test_old_rows_go_and_fresh_ones_stay(self):
        self._log(_iso(120))
        self._log(_iso(5))
        removed = history.prune_ledgers(self.store)
        self.assertEqual(removed.get("alert_log"), 1)
        self.assertEqual(self._count("alert_log"), 1)

    def test_an_OPEN_outages_pages_are_kept_however_old(self):
        # already_paged() is the per-outage dedupe; losing the row re-pages an
        # outage that is still open. Prod carries exactly this case.
        oid = self._outage(resolved=False)
        self._log(_iso(400), outage_id=oid)
        history.prune_ledgers(self.store)
        self.assertEqual(self._count("alert_log"), 1)
        self.assertTrue(self.store.already_paged(oid))

    def test_a_RESOLVED_outages_pages_do_age_out(self):
        oid = self._outage(resolved=True)
        self._log(_iso(400), outage_id=oid)
        history.prune_ledgers(self.store)
        self.assertEqual(self._count("alert_log"), 0)

    def test_the_newest_uplink_row_per_org_is_kept(self):
        # uplink_active() reads the newest UPLINK payload with no time bound of
        # its own, so pruning it would silently clear a live uplink alarm.
        self._log(_iso(400), payload='{"t":"UPLINK_DOWN"}')
        self.assertTrue(self.store.uplink_active(ORG))
        history.prune_ledgers(self.store)
        self.assertTrue(self.store.uplink_active(ORG))

    def test_older_uplink_rows_still_age_out(self):
        self._log(_iso(500), payload='{"t":"UPLINK_DOWN"}')
        self._log(_iso(400), payload='{"t":"UPLINK_UP"}')
        history.prune_ledgers(self.store)
        self.assertEqual(self._count("alert_log"), 1)
        self.assertFalse(self.store.uplink_active(ORG))

    def test_retention_clears_the_cooldown_window_by_orders_of_magnitude(self):
        self.assertGreater(history.RETENTION_DAYS["alert_log"] * 24 * 60,
                           self.cfg.alert_cooldown_min * 100)


class EscalationPruneTest(RetentionTestBase):
    def _esc(self, outage_id, due_at, kind="hourly", executed=None):
        with self.store._write_lock, self.store._connect() as conn:
            conn.execute(
                "INSERT INTO escalations (org_id, outage_id, kind, due_at,"
                " executed_at) VALUES (?,?,?,?,?)",
                (ORG, outage_id, kind, due_at, executed))
            conn.commit()

    def test_a_resolved_outages_escalation_ages_out(self):
        oid = self._outage(resolved=True)
        self._esc(oid, _iso(120), executed=_iso(120))
        removed = history.prune_ledgers(self.store)
        self.assertEqual(removed.get("escalations"), 1)
        self.assertEqual(self._count("escalations"), 0)

    def test_an_OPEN_outages_escalation_is_kept_however_old(self):
        # schedule_escalation is INSERT OR IGNORE on (outage_id, kind): delete
        # the row and the hourly ladder re-arms and pages again.
        oid = self._outage(resolved=False)
        self._esc(oid, _iso(400))
        history.prune_ledgers(self.store)
        self.assertEqual(self._count("escalations"), 1)
        self.assertEqual(len(self.store.due_escalations(ORG, _iso(0))), 1)

    def test_a_fresh_escalation_stays(self):
        oid = self._outage(resolved=True)
        self._esc(oid, _iso(2), executed=_iso(2))
        history.prune_ledgers(self.store)
        self.assertEqual(self._count("escalations"), 1)


class AlertDigestPruneTest(RetentionTestBase):
    def test_sent_rows_age_out_at_thirty_days(self):
        self.store.queue_digest(ORG, self.dev, "OPTICAL_WARN", "t", "b", _iso(40))
        self.store.mark_digests_sent(ORG, _iso(40))
        removed = history.prune_ledgers(self.store)
        self.assertEqual(removed.get("alert_digest"), 1)
        self.assertEqual(self._count("alert_digest"), 0)

    def test_a_PENDING_row_is_never_pruned(self):
        # sent_at NULL is the live queue, and flush_digests anchors its
        # interval on the OLDEST pending row.
        self.store.queue_digest(ORG, self.dev, "OPTICAL_WARN", "t", "b", _iso(400))
        history.prune_ledgers(self.store)
        self.assertEqual(len(self.store.pending_digest(ORG)), 1)

    def test_a_fresh_sent_row_stays(self):
        self.store.queue_digest(ORG, self.dev, "OPTICAL_WARN", "t", "b", _iso(2))
        self.store.mark_digests_sent(ORG, _iso(2))
        history.prune_ledgers(self.store)
        self.assertEqual(self._count("alert_digest"), 1)


class NodeAlertPruneTest(RetentionTestBase):
    def test_old_rows_go_but_the_newest_per_node_stays(self):
        self.store.record_node_alert(ORG, NODE, "NODE_STALE", "sent", "", _iso(400))
        self.store.record_node_alert(ORG, NODE, "NODE_OK", "sent", "", _iso(390))
        self.store.record_node_alert(ORG, NODE, "NODE_STALE", "sent", "", _iso(380))
        removed = history.prune_ledgers(self.store)
        self.assertEqual(removed.get("node_alerts"), 2)
        self.assertEqual(self._count("node_alerts"), 1)

    def test_a_stale_node_stays_stale_across_a_prune(self):
        # The watchdog is transition-only: blank its rehydration row and it
        # pages NODE_STALE all over again. On prod, 9 of 16 nodes have their
        # newest row months old, so this is the common case, not the corner.
        self.store.record_node_alert(ORG, NODE, "NODE_STALE", "sent", "", _iso(400))
        self.assertTrue(self.store.last_node_alarm(ORG, NODE))
        history.prune_ledgers(self.store)
        self.assertTrue(self.store.last_node_alarm(ORG, NODE))

    def test_an_OK_node_stays_OK_across_a_prune(self):
        self.store.record_node_alert(ORG, NODE, "NODE_STALE", "sent", "", _iso(400))
        self.store.record_node_alert(ORG, NODE, "NODE_OK", "sent", "", _iso(399))
        history.prune_ledgers(self.store)
        self.assertFalse(self.store.last_node_alarm(ORG, NODE))
        self.assertEqual(self._count("node_alerts"), 1)

    def test_a_later_failed_row_never_hides_the_last_sent_one(self):
        # node_stale_active filters status='sent', so the newest row overall
        # and the newest row it READS can be different rows.
        self.store.record_node_alert(ORG, NODE, "NODE_STALE", "sent", "", _iso(400))
        self.store.record_node_alert(ORG, NODE, "NODE_OK", "failed", "", _iso(399))
        history.prune_ledgers(self.store)
        self.assertTrue(self.store.last_node_alarm(ORG, NODE))

    def test_each_node_keeps_its_own_row(self):
        for node in ("n1", "n2", "n3"):
            self.store.record_node_alert(ORG, node, "NODE_STALE", "sent", "",
                                         _iso(400))
        history.prune_ledgers(self.store)
        self.assertEqual(self._count("node_alerts"), 3)


class LedgerCutoffTest(RetentionTestBase):
    def test_every_retention_table_gets_a_cutoff(self):
        cutoffs = history.ledger_cutoffs()
        self.assertEqual(set(cutoffs), set(history.RETENTION_DAYS))

    def test_cutoffs_match_the_stored_stamp_format(self):
        # Stored stamps are _now_iso() — T-separated, +00:00, second precision.
        # A cutoff in any other shape compares as text and silently mis-sorts.
        for value in history.ledger_cutoffs().values():
            self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")

    def test_a_table_with_no_cutoff_is_left_alone(self):
        self.store.record_node_alert(ORG, NODE, "NODE_STALE", "sent", "", _iso(400))
        self.store.record_node_alert(ORG, NODE, "NODE_OK", "sent", "", _iso(399))
        self.assertEqual(self.store.prune_alert_tables({}), {})
        self.assertEqual(self._count("node_alerts"), 2)

    def test_the_prune_survives_a_store_that_raises(self):
        # The documented invariant: a dead maintenance thread is silent
        # forever, so one bad table may never take the tick down.
        class Boom:
            def prune_proxy_audit(self, cutoff):
                raise RuntimeError("nope")

            def prune_alert_tables(self, cutoffs):
                raise RuntimeError("nope")

        self.assertEqual(history.prune_ledgers(Boom()), {})


class MaintenanceWiringTest(RetentionTestBase):
    def test_run_maintenance_prunes_the_ledgers_too(self):
        with self.store._write_lock, self.store._connect() as conn:
            conn.execute(
                "INSERT INTO proxy_audit (sid, org_id, device_id, user_id,"
                " method, path, status, ts) VALUES ('s',?,?,NULL,'GET','/',200,?)",
                (ORG, self.dev, _iso(400)))
            conn.commit()
        history.run_maintenance(self.store, self.cfg)
        self.assertEqual(self._count("proxy_audit"), 0)

    def test_the_thread_starts_even_with_the_historian_off(self):
        # Retention bounds the disk; hanging it off a feature flag is the same
        # shape as the bug that left proxy_audit pruning only on session create.
        cfg = Config(central_db=self.cfg.central_db, hist_enabled=False)
        t = history.start_history_thread(cfg, self.store)
        self.assertIsNotNone(t)
        self.assertTrue(t.daemon)


if __name__ == "__main__":
    unittest.main()
