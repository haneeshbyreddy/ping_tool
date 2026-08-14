from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from wisp.central import onuroster
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
                f" LEFT JOIN device_states ds ON ds.device_id = sp.device_id"
                f" WHERE sp.org_id=? AND sp.monitored=1 AND sp.{flag_col}=1"
                f" AND d.is_active=1"
                f" AND COALESCE(ds.state,'') NOT IN ('DOWN','UNREACHABLE')"
                f" ORDER BY sp.{since_col}", (org_id,)).fetchall()
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


    def down_ports(self, org_id: str) -> list[dict]:


        with self._connect() as conn:
            rows = conn.execute(
                "SELECT sp.id AS port_id, sp.device_id, sp.if_index, sp.if_name,"
                " sp.if_alias, sp.admin_status, sp.oper_status, sp.alarm_since,"
                " sp.updated_at, d.name AS switch_name, d.region,"
                " ds.state AS device_state"
                " FROM switch_ports sp JOIN org_devices d ON d.id = sp.device_id"
                " LEFT JOIN device_states ds ON ds.device_id = sp.device_id"
                " WHERE sp.org_id=? AND sp.monitored=1 AND sp.alarm=1"
                " AND d.is_active=1"
                " ORDER BY d.name, sp.if_index", (org_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            base = r["if_name"] or f"if{r['if_index']}"
            d["label"] = f"{base} ({r['if_alias']})" if r["if_alias"] else base
            out.append(d)
        return out


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


    def set_port_uplink(self, org_id: str, port_id: int,
                        uplink_device_id: int | None) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE switch_ports SET uplink_device_id=? WHERE id=? AND org_id=?",
                (uplink_device_id, port_id, org_id))
            conn.commit()
            return cur.rowcount > 0


    def list_link_ports(self, org_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT p.id, p.device_id, p.if_index, p.if_name, p.if_alias,"
                " p.admin_status, p.oper_status, p.monitored, p.alarm,"
                " p.bw_alarm, p.bw_high_alarm, p.in_bps, p.out_bps, p.updated_at,"
                " p.feeds_device_id, p.uplink_device_id"
                " FROM switch_ports p JOIN org_devices d ON d.id = p.device_id"
                " WHERE p.org_id=? AND d.is_active=1"
                " AND (p.feeds_device_id IS NOT NULL OR p.uplink_device_id IS NOT NULL)"
                " ORDER BY p.device_id, p.if_index", (org_id,)).fetchall()
        return [dict(r) for r in rows]


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


    def onu_rx_counts(self, org_id: str, device_id: int) -> dict:

        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(CASE WHEN rx_dbm IS NOT NULL THEN 1 ELSE 0 END) AS with_rx"
                " FROM onu_optics WHERE org_id=? AND device_id=?",
                (org_id, device_id)).fetchone()
        return {"total": int(row["total"] or 0), "with_rx": int(row["with_rx"] or 0)}


    _LABEL_JOIN = (" LEFT JOIN onu_places pl ON pl.org_id = o.org_id"
                   "   AND pl.mac = wisp_norm_mac(o.serial)"
                   " LEFT JOIN radius_links rl ON rl.org_id = o.org_id"
                   "   AND rl.device_id = o.device_id AND rl.onu_key = o.onu_key"
                   " LEFT JOIN radius_customers rc ON rc.org_id = rl.org_id"
                   "   AND rc.username = rl.username"
                   "   AND rc.account_id = rl.account_id")

    _RADIUS_COLS = (" rc.name AS radius_name, rc.username AS radius_username,"
                    " rc.mobile AS radius_mobile, rc.status AS radius_status,"
                    " rl.match_by AS radius_match")

    def _with_norm_mac(self, conn):
        conn.create_function("wisp_norm_mac", 1, onuroster._norm_mac,
                             deterministic=True)
        return conn

    def list_onu_optics(self, org_id: str, device_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = self._with_norm_mac(conn).execute(
                "SELECT o.*, pl.label AS label," + self._RADIUS_COLS
                + " FROM onu_optics o"
                + self._LABEL_JOIN
                + " WHERE o.org_id=? AND o.device_id=?"
                " ORDER BY o.rx_dbm IS NULL, o.rx_dbm ASC, o.onu_key",
                (org_id, device_id)).fetchall()
        return [dict(r) for r in rows]


    def upsert_web_optics(self, org_id: str, device_id: int, rows: list[dict],
                          ts: str) -> int:
        if not rows:
            return 0
        with self._write_lock, self._connect() as conn:
            for r in rows:
                key = str(r.get("onu_key") or "").strip()
                if not key:
                    continue
                conn.execute(
                    "INSERT INTO onu_web_optics (org_id, device_id, onu_key, serial,"
                    " rx_dbm, tx_dbm, distance_m, temp_c, voltage_v, tx_bias_ma,"
                    " scraped_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(org_id, device_id, onu_key) DO UPDATE SET"
                    "   serial=excluded.serial, rx_dbm=excluded.rx_dbm,"
                    "   tx_dbm=excluded.tx_dbm, distance_m=excluded.distance_m,"
                    "   temp_c=excluded.temp_c, voltage_v=excluded.voltage_v,"
                    "   tx_bias_ma=excluded.tx_bias_ma, scraped_at=excluded.scraped_at",
                    (org_id, device_id, key, r.get("serial"), r.get("rx_dbm"),
                     r.get("tx_dbm"), r.get("distance_m"), r.get("temp_c"),
                     r.get("voltage_v"), r.get("tx_bias_ma"), ts))
            conn.commit()
        return len(rows)


    def list_web_optics(self, org_id: str, device_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT onu_key, serial, rx_dbm, tx_dbm, distance_m, temp_c,"
                " voltage_v, tx_bias_ma, scraped_at FROM onu_web_optics"
                " WHERE org_id=? AND device_id=? ORDER BY onu_key",
                (org_id, device_id)).fetchall()
        return [dict(r) for r in rows]


    def web_optics_targets(self, vendors=("dbc",),
                           device_id: int | None = None) -> list[dict]:


        names = {str(n or "").strip().lower() for n in (vendors or ())}
        names.discard("")
        if not names:
            return []
        marks = ",".join("?" * len(names))
        args = sorted(names) * 2
        only = "" if device_id is None else " AND d.id = ?"
        if device_id is not None:
            args = args + [int(device_id)]
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT d.id, d.org_id, d.name, d.ip_address, d.assigned_node_id,"
                " d.web_ip, d.web_port, d.web_scheme, c.username, c.password_enc,"
                " CASE WHEN LOWER(COALESCE(d.gpon_vendor,'')) <> ''"
                "      THEN LOWER(d.gpon_vendor) ELSE LOWER(COALESCE(s.profile,''))"
                " END AS vendor,"
                " CASE WHEN LOWER(COALESCE(d.gpon_vendor,'')) <> ''"
                "      THEN 'declared' ELSE 'detected' END AS vendor_source,"
                " (SELECT GROUP_CONCAT(DISTINCT r.pon_port) FROM onu_optics r"
                "   WHERE r.device_id = d.id) AS pon_ports"
                " FROM org_devices d"
                " JOIN device_webui_credentials c ON c.device_id = d.id"
                " JOIN orgs g ON g.org_id = d.org_id"
                " LEFT JOIN device_snmp_status s"
                "   ON s.device_id = d.id AND s.subsystem = 'optics'"
                " WHERE d.is_active=1 AND d.maintenance=0"
                f"   AND (LOWER(COALESCE(d.gpon_vendor,'')) IN ({marks})"
                "        OR (COALESCE(d.gpon_vendor,'') = ''"
                f"            AND LOWER(COALESCE(s.profile,'')) IN ({marks})"
                "            AND COALESCE(s.sysobjectid,'') <> ''))"
                "   AND EXISTS(SELECT 1 FROM onu_optics r WHERE r.device_id = d.id)"
                "   AND COALESCE(d.assigned_node_id,'') <> ''"
                "   AND COALESCE(c.username,'') <> '' AND c.password_enc IS NOT NULL"
                "   AND g.web_proxy=1" + only +
                " ORDER BY d.org_id, d.id", args).fetchall()
        return [dict(r) for r in rows]


    @staticmethod
    def _web_optics_row(row) -> dict:
        out = dict(row)
        try:
            out["spec"] = json.loads(out["spec"])
        except (TypeError, ValueError):
            out["spec"] = {}
        out["enabled"] = bool(out["enabled"])
        return out

    def list_web_optics_profiles(self, org_id: str | None) -> list[dict]:
        with self._connect() as conn:
            if org_id is None:
                rows = conn.execute(
                    "SELECT * FROM web_optics_profiles"
                    " ORDER BY org_id IS NOT NULL, name")
            else:
                rows = conn.execute(
                    "SELECT * FROM web_optics_profiles"
                    " WHERE org_id IS NULL OR org_id=?"
                    " ORDER BY org_id IS NOT NULL, name", (org_id,))
            return [self._web_optics_row(r) for r in rows.fetchall()]

    def create_web_optics_profile(self, org_id: str | None, clean: dict) -> int:
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO web_optics_profiles (org_id, name, spec, enabled,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (org_id, clean["name"], json.dumps(clean["spec"]),
                 1 if clean["enabled"] else 0, now, now))
            conn.commit()
            return int(cur.lastrowid)

    def update_web_optics_profile(self, profile_id: int, clean: dict) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE web_optics_profiles SET name=?, spec=?, enabled=?,"
                " updated_at=? WHERE id=?",
                (clean["name"], json.dumps(clean["spec"]),
                 1 if clean["enabled"] else 0, _now_iso(), profile_id))
            conn.commit()
            return cur.rowcount > 0

    def delete_web_optics_profile(self, profile_id: int) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM web_optics_profiles WHERE id=?",
                               (profile_id,))
            conn.commit()
            return cur.rowcount > 0

    def get_web_optics_profile(self, profile_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM web_optics_profiles WHERE id=?",
                               (profile_id,)).fetchone()
        return self._web_optics_row(row) if row else None


    def set_web_optics_status(self, org_id: str, device_id: int, profile: str,
                              state: str, detail: str | None, rows: int) -> None:
        now = _now_iso()
        ok = state in ("ok", "partial")
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO web_optics_status (device_id, org_id, profile, state,"
                " detail, rows, updated_at, last_ok_at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(device_id) DO UPDATE SET org_id=excluded.org_id,"
                " profile=excluded.profile, state=excluded.state,"
                " detail=excluded.detail, rows=excluded.rows,"
                " updated_at=excluded.updated_at,"
                " last_ok_at=CASE WHEN excluded.last_ok_at IS NOT NULL"
                "   THEN excluded.last_ok_at ELSE web_optics_status.last_ok_at END",
                (device_id, org_id, profile or "", state,
                 (detail[:400] if detail else None), int(rows), now,
                 now if ok else None))
            conn.commit()

    def get_web_optics_status(self, org_id: str, device_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM web_optics_status WHERE org_id=? AND device_id=?",
                (org_id, device_id)).fetchone()
        return dict(row) if row else None


    def upsert_user_macs(self, org_id: str, device_id: int, rows: list[dict],
                         ts: str) -> int:


        if not rows:
            return 0
        kept = 0
        with self._write_lock, self._connect() as conn:
            for r in rows:
                key = str(r.get("onu_key") or "").strip()
                mac = str(r.get("mac") or "").strip().upper()
                if not key or not mac:
                    continue
                conn.execute(
                    "INSERT INTO onu_user_macs (org_id, device_id, onu_key, mac,"
                    " vlan, kind, port_label, first_seen_at, last_seen_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(device_id, onu_key, mac) DO UPDATE SET"
                    "   org_id=excluded.org_id, vlan=excluded.vlan,"
                    "   kind=excluded.kind, port_label=excluded.port_label,"
                    "   last_seen_at=excluded.last_seen_at",
                    (org_id, device_id, key, mac, r.get("vlan"), r.get("kind"),
                     r.get("port_label"), ts, ts))
                kept += 1
            conn.commit()
        return kept

    def list_user_macs(self, org_id: str, device_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT onu_key, mac, vlan, kind, port_label, first_seen_at,"
                " last_seen_at FROM onu_user_macs WHERE org_id=? AND device_id=?"
                " ORDER BY onu_key, last_seen_at DESC, mac",
                (org_id, device_id)).fetchall()
        return [dict(r) for r in rows]

    def user_macs_for_slot(self, org_id: str, device_id: int,
                           onu_key: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT mac, vlan, kind, port_label, first_seen_at, last_seen_at"
                " FROM onu_user_macs WHERE org_id=? AND device_id=? AND onu_key=?"
                " ORDER BY last_seen_at DESC, mac",
                (org_id, device_id, str(onu_key or ""))).fetchall()
        return [dict(r) for r in rows]

    def user_mac_counts(self, org_id: str) -> dict[int, int]:


        with self._connect() as conn:
            rows = conn.execute(
                "SELECT device_id, COUNT(DISTINCT onu_key) n FROM onu_user_macs"
                " WHERE org_id=? GROUP BY device_id", (org_id,)).fetchall()
        return {int(r["device_id"]): int(r["n"]) for r in rows}

    def set_web_mac_status(self, org_id: str, device_id: int, profile: str,
                           state: str, detail: str | None, rows: int,
                           declared: int | None = None) -> None:
        now = _now_iso()
        ok = state in ("ok", "partial")
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO web_mac_status (device_id, org_id, profile, state,"
                " detail, rows, declared, updated_at, last_ok_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(device_id) DO UPDATE SET org_id=excluded.org_id,"
                " profile=excluded.profile, state=excluded.state,"
                " detail=excluded.detail, rows=excluded.rows,"
                " declared=excluded.declared, updated_at=excluded.updated_at,"
                " last_ok_at=CASE WHEN excluded.last_ok_at IS NOT NULL"
                "   THEN excluded.last_ok_at ELSE web_mac_status.last_ok_at END",
                (device_id, org_id, profile or "", state,
                 (detail[:400] if detail else None), int(rows),
                 (int(declared) if declared is not None else None), now,
                 now if ok else None))
            conn.commit()

    def get_web_mac_status(self, org_id: str, device_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM web_mac_status WHERE org_id=? AND device_id=?",
                (org_id, device_id)).fetchone()
        return dict(row) if row else None

    def list_web_mac_profiles(self, org_id: str | None) -> list[dict]:
        with self._connect() as conn:
            if org_id is None:
                rows = conn.execute(
                    "SELECT * FROM web_mac_profiles ORDER BY"
                    " IFNULL(org_id,''), name").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM web_mac_profiles WHERE org_id IS NULL"
                    " OR org_id=? ORDER BY IFNULL(org_id,''), name",
                    (org_id,)).fetchall()
        return [self._web_optics_row(r) for r in rows]

    def user_mac_targets(self, vendors=("dbc",),
                         device_id: int | None = None) -> list[dict]:


        names = {str(n or "").strip().lower() for n in (vendors or ())}
        names.discard("")
        if not names:
            return []
        marks = ",".join("?" * len(names))
        args = sorted(names) * 2
        only = "" if device_id is None else " AND d.id = ?"
        if device_id is not None:
            args = args + [int(device_id)]
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT d.id, d.org_id, d.name, d.ip_address, d.assigned_node_id,"
                " d.web_ip, d.web_port, d.web_scheme, c.username, c.password_enc,"
                " CASE WHEN LOWER(COALESCE(d.gpon_vendor,'')) <> ''"
                "      THEN LOWER(d.gpon_vendor) ELSE LOWER(COALESCE(s.profile,''))"
                " END AS vendor,"
                " CASE WHEN LOWER(COALESCE(d.gpon_vendor,'')) <> ''"
                "      THEN 'declared' ELSE 'detected' END AS vendor_source"
                " FROM org_devices d"
                " JOIN device_webui_credentials c ON c.device_id = d.id"
                " JOIN orgs g ON g.org_id = d.org_id"
                " LEFT JOIN device_snmp_status s"
                "   ON s.device_id = d.id AND s.subsystem = 'optics'"
                " WHERE d.is_active=1 AND d.maintenance=0"
                f"   AND (LOWER(COALESCE(d.gpon_vendor,'')) IN ({marks})"
                "        OR (COALESCE(d.gpon_vendor,'') = ''"
                f"            AND LOWER(COALESCE(s.profile,'')) IN ({marks})"
                "            AND COALESCE(s.sysobjectid,'') <> ''))"
                "   AND EXISTS(SELECT 1 FROM onu_optics r WHERE r.device_id = d.id)"
                "   AND COALESCE(d.assigned_node_id,'') <> ''"
                "   AND COALESCE(c.username,'') <> '' AND c.password_enc IS NOT NULL"
                "   AND g.web_proxy=1" + only +
                " ORDER BY d.org_id, d.id", args).fetchall()
        return [dict(r) for r in rows]


    def upsert_nvr_channels(self, org_id: str, device_id: int, rows: list[dict],
                            ts: str, prune: bool = True) -> dict:
        with self._write_lock, self._connect() as conn:
            prior: dict[int, str] = {}
            unwatched: set[int] = set()
            for r in conn.execute(
                    "SELECT channel_no, state, monitored FROM nvr_channels"
                    " WHERE org_id=? AND device_id=?", (org_id, device_id)):
                prior[int(r["channel_no"])] = str(r["state"])
                if not r["monitored"]:
                    unwatched.add(int(r["channel_no"]))
            kept = 0
            seen: set[int] = set()
            for r in rows or ():
                try:
                    chan = int(r["channel_no"])
                except (TypeError, ValueError, KeyError):
                    continue
                state = str(r.get("state") or "unknown")
                if state not in ("online", "offline", "unknown"):
                    state = "unknown"
                seen.add(chan)
                conn.execute(
                    "INSERT INTO nvr_channels (org_id, device_id, channel_no,"
                    " name, ip_address, port, camera_kind, enabled, state,"
                    " last_online_at, first_seen_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(device_id, channel_no) DO UPDATE SET"
                    "   org_id=excluded.org_id, name=excluded.name,"
                    "   ip_address=excluded.ip_address, port=excluded.port,"
                    "   camera_kind=excluded.camera_kind,"
                    "   enabled=excluded.enabled, state=excluded.state,"
                    "   last_online_at=CASE WHEN excluded.state='online'"
                    "     THEN excluded.last_online_at"
                    "     ELSE nvr_channels.last_online_at END,"
                    "   updated_at=excluded.updated_at",
                    (org_id, device_id, chan, r.get("name"), r.get("ip_address"),
                     r.get("port"), r.get("camera_kind"),
                     1 if r.get("enabled", True) else 0, state,
                     ts if state == "online" else None, ts, ts))
                kept += 1
            if prune and seen:
                marks = ",".join("?" * len(seen))
                conn.execute(
                    f"DELETE FROM nvr_channels WHERE org_id=? AND device_id=?"
                    f" AND channel_no NOT IN ({marks})",
                    (org_id, device_id, *sorted(seen)))
            elif prune and not rows:
                conn.execute(
                    "DELETE FROM nvr_channels WHERE org_id=? AND device_id=?",
                    (org_id, device_id))
            conn.commit()
        return {"kept": kept, "prior": prior, "unwatched": unwatched}

    def list_nvr_channels(self, org_id: str, device_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT channel_no, name, ip_address, port, camera_kind,"
                " enabled, monitored, state, last_online_at, first_seen_at,"
                " updated_at"
                " FROM nvr_channels WHERE org_id=? AND device_id=?"
                " ORDER BY channel_no", (org_id, device_id)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["enabled"] = bool(d["enabled"])
            d["monitored"] = bool(d["monitored"])
            out.append(d)
        return out

    def dark_cameras(self, org_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT nc.device_id, nc.channel_no, nc.name, nc.ip_address,"
                " nc.last_online_at, d.name AS nvr_name, d.region"
                " FROM nvr_channels nc JOIN org_devices d ON d.id = nc.device_id"
                " WHERE nc.org_id=? AND nc.enabled=1 AND nc.monitored=1"
                "   AND nc.state='offline' AND d.is_active=1"
                " ORDER BY d.name, nc.channel_no", (org_id,)).fetchall()
        return [dict(r) for r in rows]

    def set_nvr_channel_watch(self, org_id: str, device_id: int,
                              channel_no: int, monitored: bool) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE nvr_channels SET monitored=?"
                " WHERE org_id=? AND device_id=? AND channel_no=?",
                (1 if monitored else 0, org_id, device_id, int(channel_no)))
            conn.commit()
        return cur.rowcount > 0

    def set_nvr_status(self, org_id: str, device_id: int, profile: str,
                       state: str, detail: str | None, channels: int = 0) -> None:
        now = _now_iso()
        ok = state in ("ok", "partial")
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO nvr_status (device_id, org_id, profile, state,"
                " detail, channels, updated_at, last_ok_at)"
                " VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(device_id) DO UPDATE SET org_id=excluded.org_id,"
                " profile=excluded.profile, state=excluded.state,"
                " detail=excluded.detail, channels=excluded.channels,"
                " updated_at=excluded.updated_at,"
                " last_ok_at=CASE WHEN excluded.last_ok_at IS NOT NULL"
                "   THEN excluded.last_ok_at ELSE nvr_status.last_ok_at END",
                (device_id, org_id, profile or "", state,
                 (detail[:400] if detail else None), int(channels), now,
                 now if ok else None))
            conn.commit()

    def get_nvr_status(self, org_id: str, device_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM nvr_status WHERE org_id=? AND device_id=?",
                (org_id, device_id)).fetchone()
        return dict(row) if row else None

    def list_nvr_profiles(self, org_id: str | None) -> list[dict]:
        with self._connect() as conn:
            if org_id is None:
                rows = conn.execute(
                    "SELECT * FROM nvr_profiles ORDER BY"
                    " IFNULL(org_id,''), name").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM nvr_profiles WHERE org_id IS NULL"
                    " OR org_id=? ORDER BY IFNULL(org_id,''), name",
                    (org_id,)).fetchall()
        return [self._web_optics_row(r) for r in rows]

    def nvr_targets(self, vendors=("cpplus",),
                    device_id: int | None = None) -> list[dict]:
        names = {str(n or "").strip().lower() for n in (vendors or ())}
        names.discard("")
        if not names:
            return []
        marks = ",".join("?" * len(names))
        args: list = sorted(names)
        only = "" if device_id is None else " AND d.id = ?"
        if device_id is not None:
            args = args + [int(device_id)]
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT d.id, d.org_id, d.name, d.ip_address, d.assigned_node_id,"
                " d.web_ip, d.web_port, d.web_scheme, c.username, c.password_enc,"
                " LOWER(d.nvr_vendor) AS vendor"
                " FROM org_devices d"
                " JOIN device_webui_credentials c ON c.device_id = d.id"
                " JOIN orgs g ON g.org_id = d.org_id"
                " WHERE d.is_active=1 AND d.maintenance=0"
                "   AND d.device_type='nvr'"
                f"  AND LOWER(COALESCE(d.nvr_vendor,'')) IN ({marks})"
                "   AND COALESCE(d.assigned_node_id,'') <> ''"
                "   AND COALESCE(c.username,'') <> '' AND c.password_enc IS NOT NULL"
                "   AND g.web_proxy=1" + only +
                " ORDER BY d.org_id, d.id", args).fetchall()
        return [dict(r) for r in rows]

    def onu_search_device_ids(self, org_id: str, needle: str) -> list[int]:


        if not needle:
            return []
        with self._connect() as conn:
            conn.create_function("wisp_search_key", 1, onuroster.search_key,
                                 deterministic=True)
            self._with_norm_mac(conn)
            rows = conn.execute(
                "SELECT DISTINCT o.device_id FROM onu_optics o"
                " JOIN org_devices d ON d.id = o.device_id"
                + self._LABEL_JOIN
                + " WHERE o.org_id=? AND d.org_id=? AND d.is_active=1"
                " AND (wisp_search_key(o.serial) LIKE ?"
                "      OR wisp_search_key(o.name) LIKE ?"
                "      OR wisp_search_key(pl.label) LIKE ?"
                "      OR wisp_search_key(rc.name) LIKE ?"
                "      OR wisp_search_key(rc.username) LIKE ?"
                "      OR wisp_search_key(rc.mobile) LIKE ?)",
                (org_id, org_id, f"%{needle}%", f"%{needle}%", f"%{needle}%",
                 f"%{needle}%", f"%{needle}%", f"%{needle}%")).fetchall()
        return [r["device_id"] for r in rows]


    def org_onu_rows(self, org_id: str, device_id: int | None = None) -> list[dict]:

        q = ("SELECT o.device_id, o.onu_key, o.pon_port, o.onu_id, o.name, o.serial,"
             " o.state, o.distance_m, o.last_online_at, o.updated_at,"
             " o.rx_dbm, o.severity, pl.label AS label,"
             + self._RADIUS_COLS + ","
             " d.name AS device_name"
             " FROM onu_optics o JOIN org_devices d ON d.id = o.device_id"
             + self._LABEL_JOIN
             + " WHERE o.org_id=? AND d.org_id=? AND d.is_active=1")
        args: list = [org_id, org_id]
        if device_id is not None:
            q += " AND o.device_id=?"
            args.append(device_id)
        with self._connect() as conn:
            rows = self._with_norm_mac(conn).execute(q, args).fetchall()
        return [dict(r) for r in rows]


    _PLACE_COLS = ("mac, lat, lng, label, phone, notes, witness, accuracy_m,"
                   " place_source, placed_by, placed_at, created_at, updated_at")

    _PLACE_RADIUS = (
        ", (SELECT CASE WHEN COUNT(DISTINCT rc.name) = 1 THEN MIN(rc.name) END"
        "   FROM onu_optics o"
        "   JOIN radius_links rl ON rl.org_id = o.org_id"
        "     AND rl.device_id = o.device_id AND rl.onu_key = o.onu_key"
        "   JOIN radius_customers rc ON rc.org_id = rl.org_id"
        "     AND rc.username = rl.username AND rc.account_id = rl.account_id"
        "   WHERE o.org_id = onu_places.org_id"
        "     AND wisp_norm_mac(o.serial) = onu_places.mac) AS radius_name")

    def list_onu_places(self, org_id: str, *,
                        located_only: bool = False) -> list[dict]:


        q = (f"SELECT {self._PLACE_COLS}{self._PLACE_RADIUS}"
             " FROM onu_places WHERE org_id=?")
        if located_only:
            q += " AND lat IS NOT NULL AND lng IS NOT NULL"
        with self._connect() as conn:
            rows = self._with_norm_mac(conn).execute(
                q + " ORDER BY label, mac", (org_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_onu_place(self, org_id: str, mac: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {self._PLACE_COLS} FROM onu_places"
                " WHERE org_id=? AND mac=?", (org_id, mac)).fetchone()
        return dict(row) if row else None

    def onu_place_macs(self, org_id: str, *, witness_only: bool = True,
                       located_only: bool = False) -> set[str]:


        q = "SELECT mac FROM onu_places WHERE org_id=?"
        if witness_only:
            q += " AND witness=1"
        if located_only:
            q += " AND lat IS NOT NULL AND lng IS NOT NULL"
        with self._connect() as conn:
            rows = conn.execute(q, (org_id,)).fetchall()
        return {r["mac"] for r in rows}

    def onu_interfaces(self, org_id: str, device_ids: set[int]) -> dict:

        if not device_ids:
            return {}
        marks = ",".join("?" * len(device_ids))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT device_id, if_index, if_name, if_alias, oper_status,"
                f" in_bps, out_bps, updated_at FROM switch_ports"
                f" WHERE org_id=? AND device_id IN ({marks}) AND if_name IS NOT NULL",
                (org_id, *sorted(device_ids))).fetchall()
        out: dict = {}
        for r in rows:
            token = (r["if_name"] or "").split()[0] if r["if_name"] else ""
            if token:
                out.setdefault((r["device_id"], token), dict(r))
        return out

    def set_onu_place(self, org_id: str, mac: str, lat: float, lng: float,
                      label: str | None, notes: str | None,
                      *, witness: bool, phone: str | None = None) -> bool:


        if not mac:
            return False
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO onu_places (org_id, mac, lat, lng, label, phone,"
                " notes, witness, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(org_id, mac) DO UPDATE SET lat=excluded.lat,"
                " lng=excluded.lng, label=excluded.label, notes=excluded.notes,"
                " phone=COALESCE(excluded.phone, onu_places.phone),"
                " witness=excluded.witness, updated_at=excluded.updated_at",
                (org_id, mac, lat, lng, label or None, phone or None,
                 notes or None, 1 if witness else 0, now, now))
            conn.commit()
        return True

    def place_onu_in_field(self, org_id: str, mac: str, lat: float, lng: float,
                           *, witness: bool, accuracy_m: float | None,
                           source: str, placed_by: str,
                           label: str | None = None,
                           phone: str | None = None) -> bool:

        if not mac:
            return False
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO onu_places (org_id, mac, lat, lng, label, phone,"
                " notes, witness, accuracy_m, place_source, placed_by,"
                " placed_at, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(org_id, mac) DO UPDATE SET lat=excluded.lat,"
                " lng=excluded.lng, witness=excluded.witness,"
                " label=COALESCE(excluded.label, onu_places.label),"
                " phone=COALESCE(excluded.phone, onu_places.phone),"
                " accuracy_m=excluded.accuracy_m,"
                " place_source=excluded.place_source,"
                " placed_by=excluded.placed_by, placed_at=excluded.placed_at,"
                " updated_at=excluded.updated_at",
                (org_id, mac, lat, lng, label or None, phone or None, None,
                 1 if witness else 0, accuracy_m, source, placed_by, now,
                 now, now))
            conn.commit()
        return True

    def set_onu_place_contact(self, org_id: str, mac: str, label: str | None,
                              phone: str | None = None) -> bool:


        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE onu_places SET label=?, phone=?, updated_at=?"
                " WHERE org_id=? AND mac=?",
                (label or None, phone or None, _now_iso(), org_id, mac))
            conn.commit()
        return cur.rowcount > 0

    def onu_place_witness(self, org_id: str, mac: str) -> bool | None:

        with self._connect() as conn:
            row = conn.execute(
                "SELECT witness FROM onu_places WHERE org_id=? AND mac=?",
                (org_id, mac)).fetchone()
        return None if row is None else bool(row["witness"])

    def set_onu_contact(self, org_id: str, mac: str, label: str | None,
                        phone: str | None, notes: str | None) -> bool:


        if not mac:
            return False
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO onu_places (org_id, mac, lat, lng, label, phone,"
                " notes, witness, created_at, updated_at)"
                " VALUES (?,?,NULL,NULL,?,?,?,0,?,?)"
                " ON CONFLICT(org_id, mac) DO UPDATE SET label=excluded.label,"
                " phone=excluded.phone, notes=excluded.notes,"
                " updated_at=excluded.updated_at",
                (org_id, mac, label or None, phone or None, notes or None,
                 now, now))
            self._prune_onu_place(conn, org_id, mac)
            conn.commit()
        return True

    def set_onu_witness(self, org_id: str, mac: str, witness: bool) -> bool:


        if not mac:
            return False
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE onu_places SET witness=?, updated_at=?"
                " WHERE org_id=? AND mac=?",
                (1 if witness else 0, _now_iso(), org_id, mac))
            if cur.rowcount:
                self._prune_onu_place(conn, org_id, mac)
            conn.commit()
        return cur.rowcount > 0

    def clear_onu_place_coords(self, org_id: str, mac: str) -> bool:


        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE onu_places SET lat=NULL, lng=NULL, accuracy_m=NULL,"
                " place_source=NULL, placed_by=NULL, placed_at=NULL, witness=0,"
                " updated_at=? WHERE org_id=? AND mac=?",
                (_now_iso(), org_id, mac))
            self._prune_onu_place(conn, org_id, mac)
            conn.commit()
        return cur.rowcount > 0

    @staticmethod
    def _prune_onu_place(conn, org_id: str, mac: str) -> None:


        conn.execute(
            "DELETE FROM onu_places WHERE org_id=? AND mac=?"
            " AND lat IS NULL AND lng IS NULL AND witness=0"
            " AND (label IS NULL OR label='')"
            " AND (phone IS NULL OR phone='')"
            " AND (notes IS NULL OR notes='')", (org_id, mac))

    def delete_onu_place(self, org_id: str, mac: str) -> bool:

        with self._write_lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM onu_places WHERE org_id=? AND mac=?",
                               (org_id, mac))
            conn.commit()
        return cur.rowcount > 0


    def list_onu_drops(self, org_id: str) -> list[dict]:

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT mac, passive_id, waypoints, created_at, updated_at"
                " FROM onu_drops"
                " WHERE org_id=? ORDER BY passive_id, mac", (org_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["waypoints"] = json.loads(d["waypoints"] or "[]")
            except (TypeError, ValueError):
                d["waypoints"] = []
            out.append(d)
        return out

    def set_onu_drop_route(self, org_id: str, mac: str,
                           waypoints: list[list[float]]) -> bool:

        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE onu_drops SET waypoints=?, updated_at=?"
                " WHERE org_id=? AND mac=?",
                (json.dumps(waypoints), _now_iso(), org_id, mac))
            conn.commit()
            return cur.rowcount > 0

    def onu_drop_map(self, org_id: str) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT mac, passive_id FROM onu_drops WHERE org_id=?",
                (org_id,)).fetchall()
        return {r["mac"]: r["passive_id"] for r in rows}

    def set_onu_drops(self, org_id: str, macs: list[str], passive_id: int,
                      leg_no: int | None = None) -> int:


        if not macs:
            return 0
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            conn.executemany(
                "INSERT INTO onu_drops (org_id, mac, passive_id, leg_no,"
                " waypoints, created_at, updated_at) VALUES (?,?,?,?,'[]',?,?)"
                " ON CONFLICT(org_id, mac) DO UPDATE SET"
                " waypoints=CASE WHEN passive_id != excluded.passive_id"
                "   THEN '[]' ELSE waypoints END,"
                " leg_no=CASE WHEN passive_id != excluded.passive_id THEN NULL"
                "   ELSE COALESCE(excluded.leg_no, leg_no) END,"
                " passive_id=excluded.passive_id, updated_at=excluded.updated_at",
                [(org_id, m, passive_id, leg_no, now, now) for m in macs])
            conn.commit()
        return len(macs)

    def clear_onu_drops(self, org_id: str, macs: list[str]) -> int:
        if not macs:
            return 0
        marks = ",".join("?" * len(macs))
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                f"DELETE FROM onu_drops WHERE org_id=? AND mac IN ({marks})",
                (org_id, *macs))
            conn.commit()
        return cur.rowcount

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
                                 active: bool, since: str | None, ts: str,
                                 online_members: int = 0) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO onu_dup_mac_state (org_id, mac, members, online_members,"
                " active, since, updated_at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(org_id, mac) DO UPDATE SET members=excluded.members,"
                " online_members=excluded.online_members,"
                " active=excluded.active, since=excluded.since,"
                " updated_at=excluded.updated_at",
                (org_id, mac, members, online_members, 1 if active else 0, since, ts))
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
                           error: str | None = None,
                           truncated: bool = False) -> bool:
        status = "error" if error else "done"
        result = (json.dumps(varbinds, separators=(",", ":"))
                  if varbinds is not None and not error else None)
        count = len(varbinds) if varbinds is not None and not error else None
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE snmp_walks SET status=?, error=?, result=?, varbind_count=?,"
                " truncated=?, completed_at=? WHERE id=? AND org_id=? AND node_id=?"
                " AND status='pending'",
                (status, error, result, count, 1 if truncated and not error else 0,
                 _now_iso(), walk_id, org_id, node_id))
            conn.commit()
            return cur.rowcount > 0


    def list_snmp_walks(self, org_id: str, device_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, node_id, root_oid, max_varbinds, status, requested_by,"
                " error, varbind_count, truncated, created_at, completed_at"
                " FROM snmp_walks WHERE org_id=? AND device_id=? ORDER BY id DESC",
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

        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(seconds=fresh_window_s)).isoformat(timespec="seconds")
        with self._connect() as conn:
            org_names = {r["org_id"]: r["name"] for r in conn.execute(
                "SELECT org_id, name FROM orgs ORDER BY org_id")}
            rows = conn.execute(
                "SELECT d.id, d.org_id, d.name, d.device_type, d.snmp_enabled,"
                " h.updated_at AS health_at, st.state AS dev_state,"
                " g.updated_at AS optics_at, g.onus_total, g.onus_online,"
                " ps.discovered AS ports_discovered, ps.monitored AS ports_monitored,"
                " ps.fresh AS ports_fresh, ps.alarms AS ports_alarms,"
                " ps.newest AS ports_at"
                " FROM org_devices d"
                " LEFT JOIN device_health h ON h.device_id = d.id"
                " LEFT JOIN device_states st ON st.device_id = d.id"
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
                               "ever arrived: device silent or edge not walking it")
                else:
                    problem = ("snmp", "stale", "SNMP data stopped arriving")
            if is_olt and snmp_on and (r["id"], "optics") not in unsupported:
                o["optics"]["olts"] += 1
                if _fresh(r["optics_at"]):
                    o["optics"]["working"] += 1
                    o["optics"]["onus_total"] += r["onus_total"] or 0
                    if r["dev_state"] not in ("DOWN", "UNREACHABLE"):
                        o["optics"]["onus_online"] += r["onus_online"] or 0
                elif snmp_ok:
                    problem = (("optics", "stale", "optics stopped arriving")
                               if r["optics_at"] is not None else
                               ("optics", "never", "no optics reported: vendor "
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
