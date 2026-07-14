"""SNMP-sourced state: switch ports, ONU/OLT optics, PON fault state, device health, snmp status/capabilities, diagnostic walks, vendor profiles, bandwidth alarms, admin coverage overview.

Mixin half of ``CentralStore`` — composed in ``store.py``, which owns the
schema, ``__init__`` and connection plumbing (``self._connect``/``self._scope``).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from wisp.central.store_util import _now_iso, SNMP_WALKS_KEEP, SNMP_SUBSYSTEMS, SNMP_STATUS_STATES


class SnmpStoreMixin:

    def _bandwidth_alarms(self, org_id: str, *, flag_col: str, limit_col: str,
                          limit_key: str, since_col: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT sp.id AS port_id, sp.device_id, d.name AS switch_name,"
                f" sp.if_index, sp.if_name, sp.if_alias, sp.in_bps, sp.out_bps,"
                f" sp.{limit_col}, sp.bw_direction, sp.{since_col}"
                f" FROM switch_ports sp JOIN org_devices d ON d.id = sp.device_id"
                f" WHERE sp.org_id=? AND sp.monitored=1 AND sp.{flag_col}=1"
                f" AND d.is_active=1 ORDER BY sp.{since_col}", (org_id,)).fetchall()
        out = []
        for r in rows:
            base = r["if_name"] or f"if{r['if_index']}"
            label = f"{base} ({r['if_alias']})" if r["if_alias"] else base
            out.append({
                "port_id": r["port_id"], "device_id": r["device_id"],
                "switch_name": r["switch_name"], "label": label,
                "in_mbps": round(r["in_bps"] / 1e6, 2) if r["in_bps"] is not None else None,
                "out_mbps": round(r["out_bps"] / 1e6, 2) if r["out_bps"] is not None else None,
                limit_key: r[limit_col],
                "direction": r["bw_direction"] or "either",
                "since": r[since_col],
            })
        return out


    def low_bandwidth_alarms(self, org_id: str) -> list[dict]:
        return self._bandwidth_alarms(org_id, flag_col="bw_alarm",
                                      limit_col="bw_threshold_mbps",
                                      limit_key="threshold_mbps",
                                      since_col="bw_alarm_since")


    def high_bandwidth_alarms(self, org_id: str) -> list[dict]:
        return self._bandwidth_alarms(org_id, flag_col="bw_high_alarm",
                                      limit_col="bw_max_mbps", limit_key="max_mbps",
                                      since_col="bw_high_alarm_since")


    def list_switch_ports(self, org_id: str, device_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM switch_ports WHERE org_id=? AND device_id=?"
                " ORDER BY if_index", (org_id, device_id)).fetchall()
        return [dict(r) for r in rows]


    def switch_port_org(self, port_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT org_id FROM switch_ports WHERE id=?",
                               (port_id,)).fetchone()
        return row["org_id"] if row else None


    def upsert_switch_port(self, org_id: str, device_id: int, if_index: int,
                           if_name: str | None, if_alias: str | None, admin_status: str,
                           oper_status: str, last_change: str | None, down_streak: int,
                           alarm: bool, alarm_since: str | None, ts: str, *,
                           bw: tuple | None = None) -> None:
        in_octets = out_octets = counters_at = in_bps = out_bps = None
        bw_low_streak, bw_alarm, bw_alarm_since = 0, False, None
        bw_high_streak, bw_high_alarm, bw_high_alarm_since = 0, False, None
        if bw is not None:
            (in_octets, out_octets, counters_at, in_bps, out_bps,
             bw_low_streak, bw_alarm, bw_alarm_since,
             bw_high_streak, bw_high_alarm, bw_high_alarm_since) = bw
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO switch_ports (org_id, device_id, if_index, if_name,"
                " if_alias, admin_status, oper_status, last_change, down_streak, alarm,"
                " alarm_since, updated_at, in_octets, out_octets, counters_at, in_bps,"
                " out_bps, bw_low_streak, bw_alarm, bw_alarm_since, bw_high_streak,"
                " bw_high_alarm, bw_high_alarm_since)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(org_id, device_id, if_index) DO UPDATE SET"
                " if_name=excluded.if_name, if_alias=excluded.if_alias,"
                " admin_status=excluded.admin_status, oper_status=excluded.oper_status,"
                " last_change=excluded.last_change, down_streak=excluded.down_streak,"
                " alarm=excluded.alarm, alarm_since=excluded.alarm_since,"
                " updated_at=excluded.updated_at, in_octets=excluded.in_octets,"
                " out_octets=excluded.out_octets, counters_at=excluded.counters_at,"
                " in_bps=excluded.in_bps, out_bps=excluded.out_bps,"
                " bw_low_streak=excluded.bw_low_streak, bw_alarm=excluded.bw_alarm,"
                " bw_alarm_since=excluded.bw_alarm_since,"
                " bw_high_streak=excluded.bw_high_streak,"
                " bw_high_alarm=excluded.bw_high_alarm,"
                " bw_high_alarm_since=excluded.bw_high_alarm_since",
                (org_id, device_id, if_index, if_name, if_alias, admin_status,
                 oper_status, last_change, down_streak, 1 if alarm else 0, alarm_since, ts,
                 str(in_octets) if in_octets is not None else None,
                 str(out_octets) if out_octets is not None else None,
                 counters_at, in_bps, out_bps, bw_low_streak, 1 if bw_alarm else 0,
                 bw_alarm_since, bw_high_streak, 1 if bw_high_alarm else 0,
                 bw_high_alarm_since))
            conn.commit()


    def set_port_monitored(self, org_id: str, port_id: int, on: bool) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE switch_ports SET monitored=? WHERE id=? AND org_id=?",
                (1 if on else 0, port_id, org_id))
            conn.commit()
            return cur.rowcount > 0


    def set_port_feeds(self, org_id: str, port_id: int,
                       feeds_device_id: int | None) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE switch_ports SET feeds_device_id=? WHERE id=? AND org_id=?",
                (feeds_device_id, port_id, org_id))
            conn.commit()
            return cur.rowcount > 0


    def set_port_bandwidth_config(self, org_id: str, port_id: int,
                                  threshold_mbps: float | None, direction: str,
                                  max_mbps: float | None = None) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE switch_ports SET bw_threshold_mbps=?, bw_direction=?,"
                " bw_max_mbps=? WHERE id=? AND org_id=?",
                (threshold_mbps, direction, max_mbps, port_id, org_id))
            conn.commit()
            return cur.rowcount > 0


    def list_onu_optics(self, org_id: str, device_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM onu_optics WHERE org_id=? AND device_id=?"
                " ORDER BY rx_dbm IS NULL, rx_dbm ASC, onu_key",
                (org_id, device_id)).fetchall()
        return [dict(r) for r in rows]


    def org_onu_rows(self, org_id: str, device_id: int | None = None) -> list[dict]:
        """Slim ONU rows for the PON fault detector (central/ponfault.py) and the
        roster-hygiene checks (central/onuroster.py — serial + onu_id used there,
        ignored by ponfault)."""
        q = ("SELECT o.device_id, o.onu_key, o.pon_port, o.onu_id, o.name, o.serial,"
             " o.state, o.distance_m, o.last_online_at, o.updated_at,"
             " d.name AS device_name"
             " FROM onu_optics o JOIN org_devices d ON d.id = o.device_id"
             " WHERE o.org_id=? AND d.org_id=? AND d.is_active=1")
        args: list = [org_id, org_id]
        if device_id is not None:
            q += " AND o.device_id=?"
            args.append(device_id)
        with self._connect() as conn:
            rows = conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]


    def pon_fault_states(self, org_id: str) -> dict[tuple[int, str], dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pon_fault_state WHERE org_id=?", (org_id,)).fetchall()
        return {(r["device_id"], r["pon_port"]): dict(r) for r in rows}


    def upsert_pon_fault_state(self, org_id: str, device_id: int, pon_port: str,
                               *, kind: str, dark: int, active: bool,
                               since: str | None, ts: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO pon_fault_state (org_id, device_id, pon_port, kind,"
                " dark, active, since, updated_at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(org_id, device_id, pon_port) DO UPDATE SET"
                " kind=excluded.kind, dark=excluded.dark, active=excluded.active,"
                " since=excluded.since, updated_at=excluded.updated_at",
                (org_id, device_id, pon_port, kind, dark, 1 if active else 0,
                 since, ts))
            conn.commit()


    # --- ONU-roster hygiene ladder state (central/onualert.py) -----------------

    def pon_capacity_states(self, org_id: str) -> dict[tuple[int, str], dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pon_capacity_state WHERE org_id=?", (org_id,)).fetchall()
        return {(r["device_id"], r["pon_port"]): dict(r) for r in rows}


    def upsert_pon_capacity_state(self, org_id: str, device_id: int, pon_port: str,
                                  *, onus: int, active: bool, since: str | None,
                                  ts: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO pon_capacity_state (org_id, device_id, pon_port, onus,"
                " active, since, updated_at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(org_id, device_id, pon_port) DO UPDATE SET"
                " onus=excluded.onus, active=excluded.active,"
                " since=excluded.since, updated_at=excluded.updated_at",
                (org_id, device_id, pon_port, onus, 1 if active else 0, since, ts))
            conn.commit()


    def onu_dup_mac_states(self, org_id: str) -> dict[str, dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM onu_dup_mac_state WHERE org_id=?", (org_id,)).fetchall()
        return {r["mac"]: dict(r) for r in rows}


    def upsert_onu_dup_mac_state(self, org_id: str, mac: str, *, members: int,
                                 active: bool, since: str | None, ts: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO onu_dup_mac_state (org_id, mac, members, active, since,"
                " updated_at) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(org_id, mac) DO UPDATE SET members=excluded.members,"
                " active=excluded.active, since=excluded.since,"
                " updated_at=excluded.updated_at",
                (org_id, mac, members, 1 if active else 0, since, ts))
            conn.commit()


    def upsert_onu_optics(self, org_id: str, device_id: int, onu_key: str, *,
                          pon_port: str | None, onu_id: int | None, name: str | None,
                          serial: str | None, state: str | None, rx_dbm: float | None,
                          tx_dbm: float | None, olt_rx_dbm: float | None,
                          distance_m: int | None, rx_ref_dbm: float | None,
                          rx_ref_at: str | None, severity: str, ts: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO onu_optics (org_id, device_id, onu_key, pon_port, onu_id,"
                " name, serial, state, rx_dbm, tx_dbm, olt_rx_dbm, distance_m,"
                " rx_ref_dbm, rx_ref_at, severity, updated_at, last_online_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(org_id, device_id, onu_key) DO UPDATE SET"
                " pon_port=excluded.pon_port, onu_id=excluded.onu_id, name=excluded.name,"
                " serial=excluded.serial, state=excluded.state, rx_dbm=excluded.rx_dbm,"
                " tx_dbm=excluded.tx_dbm, olt_rx_dbm=excluded.olt_rx_dbm,"
                " distance_m=excluded.distance_m, rx_ref_dbm=excluded.rx_ref_dbm,"
                " rx_ref_at=excluded.rx_ref_at, severity=excluded.severity,"
                " updated_at=excluded.updated_at,"
                # freeze the timestamp the moment an ONU goes dark — the fault
                # detector clusters cohorts on it
                " last_online_at=CASE WHEN excluded.state='online'"
                "   THEN excluded.updated_at ELSE onu_optics.last_online_at END",
                (org_id, device_id, onu_key, pon_port, onu_id, name, serial, state,
                 rx_dbm, tx_dbm, olt_rx_dbm, distance_m, rx_ref_dbm, rx_ref_at,
                 severity, ts, ts if state == "online" else None))
            conn.commit()


    def get_olt_optics(self, org_id: str, device_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM olt_optics WHERE org_id=? AND device_id=?",
                (org_id, device_id)).fetchone()
        return dict(row) if row else None


    def upsert_olt_optics(self, org_id: str, device_id: int, *, onus_total: int,
                          onus_online: int, warn_count: int, crit_count: int,
                          alarm: bool, alarm_since: str | None, ts: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO olt_optics (device_id, org_id, onus_total, onus_online,"
                " warn_count, crit_count, alarm, alarm_since, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(device_id) DO UPDATE SET onus_total=excluded.onus_total,"
                " onus_online=excluded.onus_online, warn_count=excluded.warn_count,"
                " crit_count=excluded.crit_count, alarm=excluded.alarm,"
                " alarm_since=excluded.alarm_since, updated_at=excluded.updated_at",
                (device_id, org_id, onus_total, onus_online, warn_count, crit_count,
                 1 if alarm else 0, alarm_since, ts))
            conn.commit()


    def onu_optics_org(self, onu_row_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT org_id FROM onu_optics WHERE id=?",
                               (onu_row_id,)).fetchone()
        return row["org_id"] if row else None


    def set_onu_ack(self, org_id: str, onu_row_id: int, until: str | None) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE onu_optics SET ack_until=? WHERE id=? AND org_id=?",
                (until, onu_row_id, org_id))
            conn.commit()
            return cur.rowcount > 0


    def set_olt_optical_thresholds(self, org_id: str, device_id: int,
                                   warn_dbm: float | None, crit_dbm: float | None,
                                   onu_pon_limit: int | None = None) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE org_devices SET optical_warn_dbm=?, optical_crit_dbm=?,"
                " onu_pon_limit=? WHERE id=? AND org_id=? AND is_active=1",
                (warn_dbm, crit_dbm, onu_pon_limit, device_id, org_id))
            conn.commit()
            return cur.rowcount > 0


    def upsert_device_health(self, org_id: str, device_id: int, health: dict,
                             ts: str) -> None:
        def _f(key):
            v = health.get(key)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        def _i(key):
            v = _f(key)
            return int(v) if v is not None else None

        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO device_health (device_id, org_id, cpu_pct, mem_used_bytes,"
                " mem_total_bytes, mem_pct, temp_c, updated_at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(device_id) DO UPDATE SET cpu_pct=excluded.cpu_pct,"
                " mem_used_bytes=excluded.mem_used_bytes,"
                " mem_total_bytes=excluded.mem_total_bytes, mem_pct=excluded.mem_pct,"
                " temp_c=excluded.temp_c, updated_at=excluded.updated_at",
                (device_id, org_id, _f("cpu_pct"), _i("mem_used_bytes"),
                 _i("mem_total_bytes"), _f("mem_pct"), _f("temp_c"), ts))
            conn.commit()


    def upsert_snmp_statuses(self, org_id: str,
                             rows: list[tuple[int, str, dict]], ts: str) -> None:
        """Fold one report's per-device sweep diagnoses in a single transaction.
        Rows outside the closed subsystem/state vocabularies are dropped; string
        fields are length-bounded — the edge is trusted code but the wire isn't."""
        def _s(v, cap: int) -> str | None:
            return None if v is None else str(v)[:cap]

        clean: list[tuple] = []
        for device_id, subsystem, status in rows:
            state = str((status or {}).get("state") or "")
            if subsystem not in SNMP_SUBSYSTEMS or state not in SNMP_STATUS_STATES:
                continue
            count = status.get("count")
            try:
                count = int(count) if count is not None else None
            except (TypeError, ValueError):
                count = None
            clean.append((device_id, org_id, subsystem, state,
                          _s(status.get("detail"), 300),
                          _s(status.get("sysobjectid"), 128),
                          _s(status.get("profile"), 64), count, ts,
                          ts if state == "ok" else None))
        if not clean:
            return
        with self._write_lock, self._connect() as conn:
            conn.executemany(
                "INSERT INTO device_snmp_status (device_id, org_id, subsystem,"
                " state, detail, sysobjectid, profile, item_count, updated_at,"
                " last_ok_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(device_id, subsystem) DO UPDATE SET"
                " state=excluded.state, detail=excluded.detail,"
                " sysobjectid=COALESCE(excluded.sysobjectid, sysobjectid),"
                " profile=excluded.profile, item_count=excluded.item_count,"
                " updated_at=excluded.updated_at,"
                " last_ok_at=COALESCE(excluded.last_ok_at, last_ok_at)",
                clean)
            conn.commit()


    def device_snmp_status(self, org_id: str, device_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT subsystem, state, detail, sysobjectid, profile, item_count,"
                " updated_at, last_ok_at FROM device_snmp_status"
                " WHERE org_id=? AND device_id=? ORDER BY subsystem",
                (org_id, device_id)).fetchall()
        return [dict(r) for r in rows]


    def device_capabilities(self, org_id: str, device_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT subsystem, supported, note, updated_by, updated_at"
                " FROM device_capability WHERE org_id=? AND device_id=?"
                " ORDER BY subsystem", (org_id, device_id)).fetchall()
        out = [dict(r) for r in rows]
        for r in out:
            r["supported"] = bool(r["supported"])
        return out


    def set_device_capability(self, org_id: str, device_id: int, subsystem: str,
                              supported: bool, note: str | None = None,
                              updated_by: str | None = None) -> bool:
        """supported=True deletes the row — supported is the default, and keeping
        the table to only the exceptions keeps the coverage suppression query O(few)."""
        if subsystem not in SNMP_SUBSYSTEMS:
            return False
        with self._write_lock, self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM org_devices WHERE id=? AND org_id=? AND is_active=1",
                (device_id, org_id)).fetchone()
            if not exists:
                return False
            if supported:
                conn.execute(
                    "DELETE FROM device_capability WHERE device_id=? AND subsystem=?",
                    (device_id, subsystem))
            else:
                conn.execute(
                    "INSERT INTO device_capability (device_id, org_id, subsystem,"
                    " supported, note, updated_by, updated_at) VALUES (?,?,?,0,?,?,?)"
                    " ON CONFLICT(device_id, subsystem) DO UPDATE SET supported=0,"
                    " note=excluded.note, updated_by=excluded.updated_by,"
                    " updated_at=excluded.updated_at",
                    (device_id, org_id, subsystem,
                     (str(note)[:200] if note else None), updated_by, _now_iso()))
            conn.commit()
            return True


    def create_snmp_walk(self, org_id: str, device_id: int, node_id: str,
                         root_oid: str, max_varbinds: int,
                         requested_by: str | None = None) -> int:
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            # One pending walk per device — a re-request supersedes the stale one
            # instead of queueing behind it.
            conn.execute(
                "UPDATE snmp_walks SET status='error', error='superseded',"
                " completed_at=? WHERE org_id=? AND device_id=? AND status='pending'",
                (now, org_id, device_id))
            cur = conn.execute(
                "INSERT INTO snmp_walks (org_id, device_id, node_id, root_oid,"
                " max_varbinds, requested_by, created_at) VALUES (?,?,?,?,?,?,?)",
                (org_id, device_id, node_id, root_oid, max_varbinds, requested_by, now))
            conn.execute(
                "DELETE FROM snmp_walks WHERE org_id=? AND device_id=? AND id NOT IN"
                " (SELECT id FROM snmp_walks WHERE org_id=? AND device_id=?"
                "  ORDER BY id DESC LIMIT ?)",
                (org_id, device_id, org_id, device_id, SNMP_WALKS_KEEP))
            conn.commit()
            return int(cur.lastrowid)


    def pending_snmp_walks(self, org_id: str, node_id: str) -> list[dict]:
        # Target coordinates come from org_devices at DELIVERY time (not queue time)
        # so a community/port edit between queue and pickup is honored.
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT w.id, w.root_oid, w.max_varbinds, d.ip_address,"
                " d.snmp_community, d.snmp_port, d.snmp_version"
                " FROM snmp_walks w JOIN org_devices d"
                "  ON d.id=w.device_id AND d.org_id=w.org_id"
                " WHERE w.org_id=? AND w.node_id=? AND w.status='pending'"
                " AND d.is_active=1 AND d.snmp_enabled=1 ORDER BY w.id",
                (org_id, node_id)).fetchall()
        return [dict(r) for r in rows]


    def complete_snmp_walk(self, org_id: str, node_id: str, walk_id: int, *,
                           varbinds: list | None = None,
                           error: str | None = None) -> bool:
        status = "error" if error else "done"
        result = (json.dumps(varbinds, separators=(",", ":"))
                  if varbinds is not None and not error else None)
        count = len(varbinds) if varbinds is not None and not error else None
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE snmp_walks SET status=?, error=?, result=?, varbind_count=?,"
                " completed_at=? WHERE id=? AND org_id=? AND node_id=?"
                " AND status='pending'",
                (status, error, result, count, _now_iso(), walk_id, org_id, node_id))
            conn.commit()
            return cur.rowcount > 0


    def list_snmp_walks(self, org_id: str, device_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, node_id, root_oid, max_varbinds, status, requested_by,"
                " error, varbind_count, created_at, completed_at FROM snmp_walks"
                " WHERE org_id=? AND device_id=? ORDER BY id DESC",
                (org_id, device_id)).fetchall()
        return [dict(r) for r in rows]


    def get_snmp_walk(self, org_id: str, walk_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM snmp_walks WHERE id=? AND org_id=?",
                (walk_id, org_id)).fetchone()
        if not row:
            return None
        out = dict(row)
        try:
            out["result"] = json.loads(out["result"]) if out["result"] else None
        except (TypeError, ValueError):
            out["result"] = None
        return out


    def snmp_walk_org(self, walk_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT org_id FROM snmp_walks WHERE id=?",
                               (walk_id,)).fetchone()
        return row["org_id"] if row else None


    def list_snmp_profiles(self, org_id: str | None) -> list[dict]:
        # An org sees global profiles + its own; superadmin scope (None) sees all.
        with self._connect() as conn:
            if org_id is None:
                rows = conn.execute(
                    "SELECT * FROM snmp_profiles ORDER BY org_id IS NOT NULL, name")
            else:
                rows = conn.execute(
                    "SELECT * FROM snmp_profiles WHERE org_id IS NULL OR org_id=?"
                    " ORDER BY org_id IS NOT NULL, name", (org_id,))
            out = [dict(r) for r in rows.fetchall()]
        for p in out:
            try:
                p["metrics"] = json.loads(p["metrics"])
            except (TypeError, ValueError):
                p["metrics"] = {}
            p["enabled"] = bool(p["enabled"])
        return out


    def snmp_profiles_for_edge(self, org_id: str) -> list[dict]:
        return [{"name": p["name"], "match_sysobjectid": p["match_sysobjectid"],
                 "metrics": p["metrics"]}
                for p in self.list_snmp_profiles(org_id) if p["enabled"]]


    def create_snmp_profile(self, org_id: str | None, clean: dict) -> int:
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO snmp_profiles (org_id, name, match_sysobjectid, metrics,"
                " enabled, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (org_id, clean["name"], clean["match_sysobjectid"],
                 json.dumps(clean["metrics"], separators=(",", ":")),
                 1 if clean.get("enabled", True) else 0, now, now))
            conn.commit()
            return int(cur.lastrowid)


    def update_snmp_profile(self, profile_id: int, clean: dict) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE snmp_profiles SET name=?, match_sysobjectid=?, metrics=?,"
                " enabled=?, updated_at=? WHERE id=?",
                (clean["name"], clean["match_sysobjectid"],
                 json.dumps(clean["metrics"], separators=(",", ":")),
                 1 if clean.get("enabled", True) else 0, _now_iso(), profile_id))
            conn.commit()
            return cur.rowcount > 0


    def delete_snmp_profile(self, profile_id: int) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM snmp_profiles WHERE id=?", (profile_id,))
            conn.commit()
            return cur.rowcount > 0


    def get_snmp_profile(self, profile_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM snmp_profiles WHERE id=?",
                               (profile_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        try:
            out["metrics"] = json.loads(out["metrics"])
        except (TypeError, ValueError):
            out["metrics"] = {}
        out["enabled"] = bool(out["enabled"])
        return out


    # ----- GPON vendor profiles (optics counterpart of snmp_profiles) --------

    @staticmethod
    def _gpon_row(row) -> dict:
        out = dict(row)
        try:
            out["spec"] = json.loads(out["spec"])
        except (TypeError, ValueError):
            out["spec"] = {}
        out["enabled"] = bool(out["enabled"])
        return out

    def list_gpon_profiles(self, org_id: str | None) -> list[dict]:
        # An org sees global profiles + its own; superadmin scope (None) sees all.
        with self._connect() as conn:
            if org_id is None:
                rows = conn.execute(
                    "SELECT * FROM gpon_profiles ORDER BY org_id IS NOT NULL, name")
            else:
                rows = conn.execute(
                    "SELECT * FROM gpon_profiles WHERE org_id IS NULL OR org_id=?"
                    " ORDER BY org_id IS NOT NULL, name", (org_id,))
            return [self._gpon_row(r) for r in rows.fetchall()]

    def gpon_profiles_for_edge(self, org_id: str) -> list[dict]:
        # The wire shape IS the spec (name/match riding inside it) — exactly what
        # ingress/gpon.py's gpon_profile_from_dict validates.
        out = []
        for p in self.list_gpon_profiles(org_id):
            if not p["enabled"]:
                continue
            spec = dict(p["spec"])
            spec["name"] = p["name"]
            spec["match_sysobjectid"] = p["match_sysobjectid"]
            out.append(spec)
        return out

    def create_gpon_profile(self, org_id: str | None, clean: dict) -> int:
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO gpon_profiles (org_id, name, match_sysobjectid, spec,"
                " enabled, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (org_id, clean["name"], clean["match_sysobjectid"],
                 json.dumps(clean["spec"], separators=(",", ":")),
                 1 if clean.get("enabled", True) else 0, now, now))
            conn.commit()
            return int(cur.lastrowid)

    def update_gpon_profile(self, profile_id: int, clean: dict) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE gpon_profiles SET name=?, match_sysobjectid=?, spec=?,"
                " enabled=?, updated_at=? WHERE id=?",
                (clean["name"], clean["match_sysobjectid"],
                 json.dumps(clean["spec"], separators=(",", ":")),
                 1 if clean.get("enabled", True) else 0, _now_iso(), profile_id))
            conn.commit()
            return cur.rowcount > 0

    def delete_gpon_profile(self, profile_id: int) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM gpon_profiles WHERE id=?", (profile_id,))
            conn.commit()
            return cur.rowcount > 0

    def get_gpon_profile(self, profile_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM gpon_profiles WHERE id=?",
                               (profile_id,)).fetchone()
        return self._gpon_row(row) if row else None


    def admin_overview(self, fresh_window_s: int = 900,
                       now: datetime | None = None) -> dict:
        """Superadmin fleet coverage: per org, how much of the configured
        SNMP / GPON-optics / port monitoring is actually landing fresh data.

        "Working" means a reading newer than `fresh_window_s` — the edge SNMP
        cadence is ~90s, so 15 minutes of silence is a broken pipeline, not a
        gap between walks. Never-reported and gone-stale are distinguished in
        `problems` because they need different fixes (config vs dead agent).
        Optics/ports problems are suppressed on a device whose SNMP is dead
        outright — one root cause, one line.
        """
        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(seconds=fresh_window_s)).isoformat(timespec="seconds")
        with self._connect() as conn:
            org_names = {r["org_id"]: r["name"] for r in conn.execute(
                "SELECT org_id, name FROM orgs ORDER BY org_id")}
            rows = conn.execute(
                "SELECT d.id, d.org_id, d.name, d.device_type, d.snmp_enabled,"
                " h.updated_at AS health_at,"
                " g.updated_at AS optics_at, g.onus_total, g.onus_online,"
                " ps.discovered AS ports_discovered, ps.monitored AS ports_monitored,"
                " ps.fresh AS ports_fresh, ps.alarms AS ports_alarms,"
                " ps.newest AS ports_at"
                " FROM org_devices d"
                " LEFT JOIN device_health h ON h.device_id = d.id"
                " LEFT JOIN olt_optics g ON g.device_id = d.id"
                " LEFT JOIN (SELECT device_id, COUNT(*) AS discovered,"
                "    SUM(monitored) AS monitored,"
                "    SUM(CASE WHEN monitored=1 AND updated_at >= ? THEN 1 ELSE 0 END)"
                "      AS fresh,"
                "    SUM(CASE WHEN monitored=1 AND alarm=1 THEN 1 ELSE 0 END) AS alarms,"
                "    MAX(updated_at) AS newest"
                "    FROM switch_ports GROUP BY device_id) ps ON ps.device_id = d.id"
                " WHERE d.is_active=1 ORDER BY d.org_id, d.name",
                (cutoff,)).fetchall()
            # Operator-confirmed "this hardware can't do X" — those gaps are facts,
            # not problems; drop them from both the denominators and the problem list.
            unsupported = {(r["device_id"], r["subsystem"]) for r in conn.execute(
                "SELECT device_id, subsystem FROM device_capability WHERE supported=0")}

        def _fresh(ts: str | None) -> bool:
            return ts is not None and ts >= cutoff

        def _blank() -> dict:
            return {"devices": 0,
                    "snmp": {"enabled": 0, "working": 0},
                    "optics": {"olts": 0, "working": 0,
                               "onus_total": 0, "onus_online": 0},
                    "ports": {"switches": 0, "discovered": 0, "monitored": 0,
                              "working": 0, "alarms": 0},
                    "problems": []}

        orgs: dict[str, dict] = {oid: _blank() for oid in org_names}
        for r in rows:
            o = orgs.setdefault(r["org_id"], _blank())
            o["devices"] += 1
            is_olt = r["device_type"] == "OLT"
            snmp_on = bool(r["snmp_enabled"])
            last = max(filter(None, (r["health_at"], r["optics_at"],
                                     r["ports_at"])), default=None)
            snmp_ok = _fresh(last)
            problem = None
            if snmp_on:
                o["snmp"]["enabled"] += 1
                if snmp_ok:
                    o["snmp"]["working"] += 1
                elif last is None:
                    problem = ("snmp", "never", "SNMP enabled but no data has "
                               "ever arrived — device silent or edge not walking it")
                else:
                    problem = ("snmp", "stale", "SNMP data stopped arriving")
            if is_olt and snmp_on and (r["id"], "optics") not in unsupported:
                o["optics"]["olts"] += 1
                if _fresh(r["optics_at"]):
                    o["optics"]["working"] += 1
                    o["optics"]["onus_total"] += r["onus_total"] or 0
                    o["optics"]["onus_online"] += r["onus_online"] or 0
                elif snmp_ok:
                    problem = (("optics", "stale", "optics stopped arriving")
                               if r["optics_at"] is not None else
                               ("optics", "never", "no optics reported — vendor "
                                "unmatched (check sysObjectID) or ONU table empty"))
            if r["ports_discovered"]:
                o["ports"]["switches"] += 1
                o["ports"]["discovered"] += r["ports_discovered"]
                o["ports"]["monitored"] += r["ports_monitored"] or 0
                o["ports"]["working"] += r["ports_fresh"] or 0
                o["ports"]["alarms"] += r["ports_alarms"] or 0
                stale_ports = (r["ports_monitored"] or 0) - (r["ports_fresh"] or 0)
                if (stale_ports > 0 and snmp_ok and problem is None
                        and (r["id"], "ports") not in unsupported):
                    problem = ("ports", "stale",
                               f"{stale_ports} of {r['ports_monitored']} monitored "
                               "ports have stale status")
            if problem is not None:
                area, reason, detail = problem
                o["problems"].append({
                    "device_id": r["id"], "name": r["name"], "area": area,
                    "reason": reason, "detail": detail, "last_at": last})

        totals = _blank()
        problems_total = 0
        for o in orgs.values():
            totals["devices"] += o["devices"]
            for section in ("snmp", "optics", "ports"):
                for k in totals[section]:
                    totals[section][k] += o[section][k]
            problems_total += len(o["problems"])
        totals.pop("problems")

        return {"fresh_window_s": fresh_window_s,
                "generated_at": now.isoformat(timespec="seconds"),
                "totals": totals, "problems_total": problems_total,
                "orgs": [{"org_id": oid, "name": org_names.get(oid), **o}
                         for oid, o in sorted(orgs.items())]}
