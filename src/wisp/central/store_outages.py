"""FSM state persistence, outages, events, alert log, escalations, rollups and perf baseline state.

Mixin half of ``CentralStore`` — composed in ``store.py``, which owns the
schema, ``__init__`` and connection plumbing (``self._connect``/``self._scope``).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from wisp.central.store_util import _now_iso


class OutageStoreMixin:

    def device_states(self, org_id: str) -> dict[int, dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT device_id, state, latency_ms, packet_loss, jitter_ms FROM"
                " device_states WHERE org_id=?", (org_id,)).fetchall()
        return {r["device_id"]: dict(r) for r in rows}


    def write_device_states(self, org_id: str, rows: list[tuple], ts: str) -> None:
        if not rows:
            return
        with self._write_lock, self._connect() as conn:
            conn.executemany(
                "INSERT INTO device_states (device_id, org_id, state, latency_ms,"
                " packet_loss, jitter_ms, updated_at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(device_id) DO UPDATE SET state=excluded.state,"
                " latency_ms=excluded.latency_ms, packet_loss=excluded.packet_loss,"
                " jitter_ms=excluded.jitter_ms, updated_at=excluded.updated_at",
                [(did, org_id, state, lat, loss, jit, ts)
                 for did, state, lat, loss, jit in rows])
            conn.commit()


    def uplink_active(self, org_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM alert_log WHERE org_id=? AND"
                " (payload LIKE '%UPLINK%' OR payload LIKE '%Uplink%')"
                " ORDER BY id DESC LIMIT 1", (org_id,)).fetchone()
        return bool(row and "UPLINK_DOWN" in (row["payload"] or ""))


    def _insert_org_event(self, conn, org_id: str, device_id: int | None,
                          device_name: str | None, region: str | None, type_: str,
                          state: str | None, occurred_at: str, payload: dict) -> None:
        row = conn.execute(
            "SELECT COALESCE(MAX(edge_id), 0) + 1 FROM events WHERE org_id=? AND node_id=?",
            (org_id, self._CENTRAL_NODE)).fetchone()
        conn.execute(
            "INSERT INTO events (org_id, node_id, edge_id, type, device_id, device_name,"
            " device_ip, device_region, state, occurred_at, payload, received_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (org_id, self._CENTRAL_NODE, row[0], type_, device_id, device_name, None,
             region, state, occurred_at, json.dumps(payload, separators=(",", ":")),
             _now_iso()))


    def open_outage_id(self, org_id: str, device_id: int) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM outages WHERE org_id=? AND device_id=?"
                " AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
                (org_id, device_id)).fetchone()
        return row["id"] if row else None


    def open_outage_if_absent(self, org_id: str, device_id: int, ts: str,
                              state: str) -> None:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO outages (org_id, device_id, started_at, final_state)"
                " SELECT ?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM outages"
                " WHERE org_id=? AND device_id=? AND resolved_at IS NULL)",
                (org_id, device_id, ts, state, org_id, device_id))
            if cur.rowcount > 0:
                dev = conn.execute("SELECT name, region FROM org_devices WHERE id=?",
                                   (device_id,)).fetchone()
                self._insert_org_event(conn, org_id, device_id,
                    dev["name"] if dev else None, dev["region"] if dev else None,
                    "OUTAGE_OPENED", state, ts, {"started_at": ts})
            conn.commit()


    def recategorize_outage(self, org_id: str, device_id: int, state: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE outages SET final_state=? WHERE org_id=? AND device_id=?"
                " AND resolved_at IS NULL", (state, org_id, device_id))
            conn.commit()


    def stamp_outage_cause(self, org_id: str, outage_id: int, cause: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE outages SET root_cause = COALESCE(root_cause, ?)"
                " WHERE id=? AND org_id=? AND resolved_at IS NULL",
                (cause, outage_id, org_id))
            conn.commit()


    def resolve_outage(self, org_id: str, device_id: int, ts: str) -> None:
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT final_state FROM outages WHERE org_id=? AND device_id=?"
                " AND resolved_at IS NULL", (org_id, device_id)).fetchone()
            cur = conn.execute(
                "UPDATE outages SET resolved_at=? WHERE org_id=? AND device_id=?"
                " AND resolved_at IS NULL", (ts, org_id, device_id))
            if cur.rowcount > 0:
                dev = conn.execute("SELECT name, region FROM org_devices WHERE id=?",
                                   (device_id,)).fetchone()
                self._insert_org_event(conn, org_id, device_id,
                    dev["name"] if dev else None, dev["region"] if dev else None,
                    "OUTAGE_RESOLVED", row["final_state"] if row else None, ts,
                    {"resolved_at": ts})
            conn.commit()


    def outages_in_window(self, org_id: str, since: str, until: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT o.*, d.name, d.region FROM outages o"
                " JOIN org_devices d ON d.id = o.device_id"
                " WHERE o.org_id=? AND (o.resolved_at IS NULL OR o.resolved_at >= ?)"
                " AND o.started_at <= ? ORDER BY o.started_at",
                (org_id, since, until)).fetchall()
        return [dict(r) for r in rows]


    def fold_device_rollups(self, entries: list[tuple]) -> None:
        if not entries:
            return
        with self._write_lock, self._connect() as conn:
            conn.executemany(
                "INSERT INTO device_rollups (org_id, device_id, bucket, samples,"
                " latency_sum, latency_count, loss_sum, down_samples)"
                " VALUES (?,?,?,1,?,?,?,?)"
                " ON CONFLICT(org_id, device_id, bucket) DO UPDATE SET"
                " samples = samples + 1,"
                " latency_sum = latency_sum + excluded.latency_sum,"
                " latency_count = latency_count + excluded.latency_count,"
                " loss_sum = loss_sum + excluded.loss_sum,"
                " down_samples = down_samples + excluded.down_samples",
                [(org_id, device_id, bucket, latency_ms or 0.0,
                  1 if latency_ms is not None else 0, loss_pct if loss_pct is not None else 0.0,
                  down)
                 for org_id, device_id, bucket, latency_ms, loss_pct, down in entries])
            conn.commit()


    def device_rollup_series(self, org_id: str, device_id: int, since: str,
                             until: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT bucket, samples, latency_sum, latency_count, loss_sum,"
                " down_samples FROM device_rollups WHERE org_id=? AND device_id=?"
                " AND bucket >= ? AND bucket <= ? ORDER BY bucket",
                (org_id, device_id, since, until)).fetchall()
        out = []
        for r in rows:
            avg_latency = (r["latency_sum"] / r["latency_count"]) if r["latency_count"] else None
            avg_loss = (r["loss_sum"] / r["samples"]) if r["samples"] else None
            down_pct = (100.0 * r["down_samples"] / r["samples"]) if r["samples"] else None
            out.append({
                "bucket": r["bucket"], "samples": r["samples"],
                "avg_latency_ms": round(avg_latency, 2) if avg_latency is not None else None,
                "avg_loss_pct": round(avg_loss, 2) if avg_loss is not None else None,
                "down_pct": round(down_pct, 2) if down_pct is not None else None,
            })
        return out


    def prune_rollups_older_than(self, cutoff: str) -> int:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM device_rollups WHERE bucket < ?", (cutoff,))
            conn.commit()
            return cur.rowcount


    def last_resolved_state(self, org_id: str, device_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT final_state FROM outages WHERE org_id=? AND device_id=?"
                " AND resolved_at IS NOT NULL ORDER BY id DESC LIMIT 1",
                (org_id, device_id)).fetchone()
        return row["final_state"] if row else None


    def acknowledge_outage(self, org_id: str, outage_id: int, by: str) -> bool:
        with self._write_lock, self._connect() as conn:
            now = _now_iso()
            cur = conn.execute(
                "UPDATE outages SET acknowledged_at=COALESCE(acknowledged_at, ?),"
                " acknowledged_by=? WHERE id=? AND org_id=? AND resolved_at IS NULL",
                (now, by, outage_id, org_id))
            if cur.rowcount > 0:
                row = conn.execute(
                    "SELECT o.device_id, o.final_state, d.name, d.region FROM outages o"
                    " JOIN org_devices d ON d.id = o.device_id WHERE o.id=?",
                    (outage_id,)).fetchone()
                if row:
                    self._insert_org_event(conn, org_id, row["device_id"], row["name"],
                        row["region"], "OUTAGE_ACKNOWLEDGED", row["final_state"], now,
                        {"by": by})
            conn.commit()
            return cur.rowcount > 0


    def outage_org(self, outage_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT org_id FROM outages WHERE id=?",
                               (outage_id,)).fetchone()
        return row["org_id"] if row else None


    def triage_outages(self, org_id: str, postmortem_days: int = 30) -> list[dict]:
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
                 - timedelta(days=postmortem_days)).isoformat(timespec="seconds")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT o.*, d.name AS device_name, d.region FROM outages o"
                " JOIN org_devices d ON d.id = o.device_id"
                " WHERE o.org_id=? AND d.assigned_node_id IS NOT NULL"
                " AND (o.resolved_at IS NULL"
                " OR (o.root_cause IS NULL AND o.resolved_at >= ?))"
                " ORDER BY o.started_at DESC", (org_id, cutoff)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d["resolved_at"] is None:
                d["status"] = "in_progress" if d["acknowledged_at"] else "unassigned"
            else:
                d["status"] = "pending_postmortem"
            out.append(d)
        return out


    def set_outage_postmortem(self, org_id: str, outage_id: int, root_cause: str,
                              resolution_notes: str | None) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE outages SET root_cause=?, resolution_notes=?"
                " WHERE id=? AND org_id=? AND resolved_at IS NOT NULL",
                (root_cause, resolution_notes, outage_id, org_id))
            if cur.rowcount > 0:
                row = conn.execute(
                    "SELECT o.device_id, d.name, d.region FROM outages o"
                    " JOIN org_devices d ON d.id = o.device_id WHERE o.id=?",
                    (outage_id,)).fetchone()
                if row:
                    self._insert_org_event(conn, org_id, row["device_id"], row["name"],
                        row["region"], "OUTAGE_POSTMORTEM", None, _now_iso(),
                        {"root_cause": root_cause, "resolution_notes": resolution_notes})
            conn.commit()
            return cur.rowcount > 0


    def clear_pending_postmortems(self, org_id: str, root_cause: str,
                                  resolution_notes: str | None = None) -> int:
        with self._write_lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT o.id, o.device_id, d.name, d.region FROM outages o"
                " JOIN org_devices d ON d.id = o.device_id"
                " WHERE o.org_id=? AND o.resolved_at IS NOT NULL AND o.root_cause IS NULL",
                (org_id,)).fetchall()
            for r in rows:
                conn.execute(
                    "UPDATE outages SET root_cause=?, resolution_notes=? WHERE id=?",
                    (root_cause, resolution_notes, r["id"]))
                self._insert_org_event(conn, org_id, r["device_id"], r["name"],
                    r["region"], "OUTAGE_POSTMORTEM", None, _now_iso(),
                    {"root_cause": root_cause, "resolution_notes": resolution_notes})
            conn.commit()
            return len(rows)


    def list_events(self, org_id: str, limit: int = 100,
                    before_id: int | None = None) -> list[dict]:
        scope, args = self._scope(org_id)
        cursor = ""
        if before_id is not None:
            cursor = " AND id < ?"
            args = (*args, before_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, org_id, node_id, type, device_id, device_name, device_ip,"
                " device_region, state, occurred_at, received_at, payload FROM events"
                " WHERE 1=1" + scope + cursor + " ORDER BY id DESC LIMIT ?",
                (*args, max(1, min(limit, 500)))).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            payload = d.pop("payload", None)
            try:
                d["payload"] = json.loads(payload) if payload else None
            except (TypeError, ValueError):
                d["payload"] = None
            out.append(d)
        return out


    def already_paged(self, outage_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM alert_log WHERE outage_id=? AND status='sent' LIMIT 1",
                (outage_id,)).fetchone()
        return row is not None


    def log_alert(self, org_id: str, outage_id: int | None, device_id: int | None,
                  channel: str, recipient: str | None, status: str, payload: str,
                  ts: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO alert_log (org_id, outage_id, device_id, channel,"
                " recipient, sent_at, status, payload) VALUES (?,?,?,?,?,?,?,?)",
                (org_id, outage_id, device_id, channel, recipient, ts, status, payload))
            conn.commit()


    def schedule_escalation(self, org_id: str, outage_id: int, kind: str,
                            due_at: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO escalations (org_id, outage_id, kind, due_at)"
                " VALUES (?,?,?,?)", (org_id, outage_id, kind, due_at))
            conn.commit()


    def due_escalations(self, org_id: str, now_ts: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT e.id, e.kind, o.id AS outage_id, o.device_id, o.started_at,"
                " o.acknowledged_by, o.resolved_at FROM escalations e"
                " JOIN outages o ON o.id = e.outage_id"
                " WHERE e.org_id=? AND e.executed_at IS NULL AND e.due_at <= ?",
                (org_id, now_ts)).fetchall()
        return [dict(r) for r in rows]


    def cancel_pending_escalations(self, org_id: str, device_id: int, ts: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE escalations SET executed_at=? WHERE org_id=?"
                " AND executed_at IS NULL AND outage_id IN (SELECT id FROM outages"
                " WHERE org_id=? AND device_id=? AND resolved_at IS NOT NULL)",
                (ts, org_id, org_id, device_id))
            conn.commit()


    def mark_escalation_executed(self, esc_id: int, ts: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("UPDATE escalations SET executed_at=? WHERE id=?", (ts, esc_id))
            conn.commit()


    def reschedule_escalation(self, esc_id: int, due_at: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("UPDATE escalations SET due_at=? WHERE id=?", (due_at, esc_id))
            conn.commit()


    def record_perf_sample(self, org_id: str, device_id: int, ts: str,
                           latency_ms: float | None, packet_loss: float | None,
                           jitter_ms: float | None, state: str, keep: int) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO device_perf_samples (org_id, device_id, ts, latency_ms,"
                " packet_loss, jitter_ms, state) VALUES (?,?,?,?,?,?,?)",
                (org_id, device_id, ts, latency_ms, packet_loss, jitter_ms, state))
            conn.execute(
                "DELETE FROM device_perf_samples WHERE org_id=? AND device_id=? AND id"
                " NOT IN (SELECT id FROM device_perf_samples WHERE org_id=? AND"
                " device_id=? ORDER BY id DESC LIMIT ?)",
                (org_id, device_id, org_id, device_id, keep))
            conn.commit()


    def perf_sample_window(self, org_id: str, device_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, latency_ms, packet_loss, jitter_ms, state FROM"
                " device_perf_samples WHERE org_id=? AND device_id=? ORDER BY id",
                (org_id, device_id)).fetchall()
        return [dict(r) for r in rows]


    def device_perf_state(self, org_id: str, device_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT degraded, metric, baseline_ms, current_ms, since FROM"
                " device_perf WHERE org_id=? AND device_id=?",
                (org_id, device_id)).fetchone()
        return dict(row) if row else None


    def write_device_perf(self, org_id: str, device_id: int, degraded: bool,
                          metric: str | None, baseline_ms: float | None,
                          current_ms: float | None, since: str | None, ts: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO device_perf (device_id, org_id, degraded, metric,"
                " baseline_ms, current_ms, since, updated_at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(device_id) DO UPDATE SET degraded=excluded.degraded,"
                " metric=excluded.metric, baseline_ms=excluded.baseline_ms,"
                " current_ms=excluded.current_ms, since=excluded.since,"
                " updated_at=excluded.updated_at",
                (device_id, org_id, 1 if degraded else 0, metric, baseline_ms,
                 current_ms, since, ts))
            conn.commit()
