"""SNMP-sourced state: switch ports, ONU/OLT optics, PON fault state, device health, snmp status/capabilities, diagnostic walks, vendor profiles, bandwidth alarms, admin coverage overview.

Mixin half of ``CentralStore`` — composed in ``store.py``, which owns the
schema, ``__init__`` and connection plumbing (``self._connect``/``self._scope``).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from wisp.central import onuroster
from wisp.central.store_util import _now_iso, SNMP_WALKS_KEEP, SNMP_SUBSYSTEMS, SNMP_STATUS_STATES


class SnmpStoreMixin:

    def _bandwidth_alarms(self, org_id: str, *, flag_col: str, limit_col: str,
                          limit_key: str, since_col: str) -> list[dict]:
        # A device that isn't answering ICMP isn't answering SNMP either, so its
        # bw_alarm flag and in/out_bps are frozen at the last walk before it
        # dropped. Reporting that as a live bandwidth alarm points a top-bar chip
        # and a Home tile at a box whose real problem is the outage — the classic
        # one-fault-two-alarms split this dashboard keeps closing. DISPLAY ONLY:
        # this feeds /api/summary and nothing else (ports.py owns the paging and
        # its own transition state, which stays untouched — a suppressed chip must
        # never mean a suppressed page).
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
        """Every monitored port currently in the port-down alarm, org-wide, with
        its switch's name and ICMP state.

        Counted off `alarm` (the flap-suppressed flag ports.py owns), NOT a raw
        `oper_status`, so this list and the `ports_down` count on each device row
        describe the same thing — a drill-down that disagrees with the tile it
        was opened from is worse than no drill-down.

        A port on a DOWN/UNREACHABLE switch is KEPT (unlike the bandwidth
        alarms, which are a rate reading and go stale): the flag itself is still
        the last thing we knew, and the caller says so on the row rather than
        dropping it and leaving the tile's count unexplained."""
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
        """Every port bound to a link, either side: feeds_device_id names the child a
        parent-side port cables to, uplink_device_id names the parent a child-side
        port faces. One org-wide list so the map labels every link in one query."""
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
        """How much of one OLT's roster carries a real Rx figure.

        The two numbers the Rx diagnosis turns into a sentence: "none of 412
        ONUs report optical power" is a vendor verdict, "3 of 412" is a scrape
        that half-worked, and they must never render as the same empty column.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(CASE WHEN rx_dbm IS NOT NULL THEN 1 ELSE 0 END) AS with_rx"
                " FROM onu_optics WHERE org_id=? AND device_id=?",
                (org_id, device_id)).fetchone()
        return {"total": int(row["total"] or 0), "with_rx": int(row["with_rx"] or 0)}


    # The operator's own name for a subscriber lives in `onu_places.label`, keyed
    # on the MAC — never in `onu_optics.name`, which every SNMP walk overwrites
    # from the OLT (`name=excluded.name`). So a roster row has to be JOINED to it
    # to carry the name a human typed.
    #
    # Joined in the STORE rather than folded in by each caller, because "each
    # caller remembers" is exactly what failed: a name captured in the field
    # reached the DB correctly and then rendered nowhere — not the Optical tab,
    # not ONU search, not the WhatsApp lookup, not the issue list — all four
    # naming the ONU off the walked column alone. Every roster read now carries
    # `label`, and `onuroster.display_name` is the one place the order is decided.
    #
    # The join key is `onuroster._norm_mac` REGISTERED as a SQL function, not
    # mirrored as an UPPER(TRIM(...)) expression, for the reason `wisp_search_key`
    # already exists: one normalizer, so SQL identity and Python identity cannot
    # drift apart and silently stop matching a sticker.
    #
    # That safety is FREE — measured on a 15,580-row / 19-OLT fleet with 2,000
    # located subscribers: no join 117ms, this join 156ms, the "native" UPPER(TRIM)
    # version 195ms. Neither form can use an index on a computed key, so the
    # callback costs less than the expression it would have replaced. Don't
    # "optimize" it back into a second spelling of identity.
    _LABEL_JOIN = (" LEFT JOIN onu_places pl ON pl.org_id = o.org_id"
                   "   AND pl.mac = wisp_norm_mac(o.serial)")

    def _with_norm_mac(self, conn):
        conn.create_function("wisp_norm_mac", 1, onuroster._norm_mac,
                             deterministic=True)
        return conn

    def list_onu_optics(self, org_id: str, device_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = self._with_norm_mac(conn).execute(
                "SELECT o.*, pl.label AS label FROM onu_optics o"
                + self._LABEL_JOIN
                + " WHERE o.org_id=? AND o.device_id=?"
                " ORDER BY o.rx_dbm IS NULL, o.rx_dbm ASC, o.onu_key",
                (org_id, device_id)).fetchall()
        return [dict(r) for r in rows]


    # ----- web-UI scraped optics (central/weboptics.py) -----------------------

    def upsert_web_optics(self, org_id: str, device_id: int, rows: list[dict],
                          ts: str) -> int:
        """Store one scrape's readings. UPSERT per row, never delete-then-insert:
        a scrape is allowed to come back partial (the OLT keeps ONE session slot,
        so a tech logging in can end ours mid-sweep), and wiping the device's rows
        first would turn every partial into a blackout of the PONs that didn't get
        re-read. A row nothing refreshes simply ages past web_optics_max_age_s and
        stops being merged, which is the honest outcome."""
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
        """This OLT's scraped readings, freshest-first. Staleness is judged by the
        caller (weboptics.merge_scraped) against the report timestamp rather than
        filtered here — the merge is where the age has meaning, and keeping it
        there makes it testable without a clock."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT onu_key, serial, rx_dbm, tx_dbm, distance_m, temp_c,"
                " voltage_v, tx_bias_ma, scraped_at FROM onu_web_optics"
                " WHERE org_id=? AND device_id=? ORDER BY onu_key",
                (org_id, device_id)).fetchall()
        return [dict(r) for r in rows]


    def web_optics_targets(self, vendors=("dbc",),
                           device_id: int | None = None) -> list[dict]:
        """OLTs the web-optics sweeper should scrape, fleet-wide.

        ``device_id`` narrows the same query to one OLT for the dashboard's
        manual refresh. It is a FILTER on this query rather than a lookup of its
        own on purpose: "may this box be scraped, and with what" must have one
        answer, or a hand-triggered read could reach a device the sweep would
        have refused (no roster, no credentials, org's tunnel not granted).

        Two ways an OLT qualifies as a scrapable vendor, and the second one is
        why the subsystem reaches more than one box:

        1. an EXPLICIT ``gpon_vendor`` naming a vendor a web-optics profile
           covers — the operator named it; or
        2. the EDGE's own optics sweep matched that vendor's profile from the
           box's sysObjectID (``device_snmp_status`` subsystem='optics'), with
           the device's own vendor field left on automatic.

        (2) was written off as impossible — "auto-detection lives on the edge
        and is never reported to central" — and that was simply out of date:
        `device_snmp_status` has carried the matched profile name AND the raw
        sysObjectID up on every report since the SNMP-diagnosis work. It is not
        a weaker signal than the dropdown, it is a STRONGER one: the dropdown is
        a human's recollection, this is the box answering with its maker's own
        PEN arc, which is the exact evidence the human was going on. Requiring
        `sysobjectid` to be present is what keeps it that: it is only ever
        stamped on a real auto-detect, so a fleet-wide ``WISP_GPON_VENDOR``
        default can never launder itself into a detection here.

        A roster is required as well (``onu_optics``). The scrape does not
        create ONUs — it merges onto slots the SNMP walk already reported — so
        an OLT with no roster has nothing for a reading to attach to, and
        scraping it is a login and a page fetch that can only ever be discarded.

        `pon_ports` rides along for the same reason it is needed at all: the
        scrape is one POST per PON, and the roster is the only honest source of
        how many an OLT has.

        Also requires stored credentials, an assigned probe (the tunnel's route)
        and the org's web_proxy grant (without it the edge holds no long-poll,
        so every request would just eat its timeout).

        WHICH vendors qualify is no longer the literal 'dbc' baked in here: the
        caller passes the profile set in force (`ProfileSet.names()`), so
        onboarding an OLT is a dashboard row rather than an edit to this SQL.
        The vendor token each device resolved to comes back as `vendor`, so the
        sweeper does not have to re-derive it."""
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
                # The vendor this device resolved AS, and which of the two ways
                # it got there — an explicit dropdown beats a detection, exactly
                # as GponPollerPool.resolve orders them.
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
                # Only ever stamped on a real auto-detect, so a fleet-wide
                # WISP_GPON_VENDOR default can't launder itself into one.
                "            AND COALESCE(s.sysobjectid,'') <> ''))"
                "   AND EXISTS(SELECT 1 FROM onu_optics r WHERE r.device_id = d.id)"
                "   AND COALESCE(d.assigned_node_id,'') <> ''"
                "   AND COALESCE(c.username,'') <> '' AND c.password_enc IS NOT NULL"
                "   AND g.web_proxy=1" + only +
                " ORDER BY d.org_id, d.id", args).fetchall()
        return [dict(r) for r in rows]


    # ----- web-optics vendor profiles (central/weboptics_profiles.py) ---------

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
        # An org sees global profiles + its own; superadmin scope (None) sees all.
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


    # ----- web-optics scrape outcome (central/weboptics_sweep.py) -------------

    def set_web_optics_status(self, org_id: str, device_id: int, profile: str,
                              state: str, detail: str | None, rows: int) -> None:
        """Record the last scrape outcome. `last_ok_at` only advances on a state
        that actually produced readings, so a panel can always say "was working
        until <ts>" — the same contract device_snmp_status keeps for SNMP."""
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


    def onu_search_device_ids(self, org_id: str, needle: str) -> list[int]:
        """OLTs carrying at least one ONU whose serial/MAC **or name** contains
        `needle` — a tech looks a subscriber up by whichever they happen to have
        (the MAC off the sticker, or the name the OLT was provisioned with).

        Narrowing step for the Network-page search: the caller only wants a
        handful of OLTs to load rosters for, and a fleet's onu_optics is far too
        big to ship wholesale on every keystroke. `needle` MUST already be
        `onuroster.search_key`-normalized (alphanumeric, upper) — that is what
        makes it safe to interpolate as a LIKE pattern, since the normalizer
        strips `%` and `_` along with everything else non-alphanumeric.

        That same normalizer is registered as a SQL function rather than mirrored
        as a REPLACE chain, so the two sides cannot drift: the chain only knew
        the four separators MACs use, which silently failed the underscore in a
        real provisioned name like "hc_kiran". It costs a full scan of the org's
        ONU rows, which a function of a column would need anyway — and the
        3-character floor in the API keeps that off the common keystroke.
        """
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
                # The OPERATOR's name is searched beside the walked one. A tech
                # types the name they know, and after a field survey that is the
                # one they typed themselves — matching only the OLT's string
                # would answer "no such subscriber" about a drop somebody had
                # just stood at and named.
                + " WHERE o.org_id=? AND d.org_id=? AND d.is_active=1"
                " AND (wisp_search_key(o.serial) LIKE ?"
                "      OR wisp_search_key(o.name) LIKE ?"
                "      OR wisp_search_key(pl.label) LIKE ?)",
                (org_id, org_id, f"%{needle}%", f"%{needle}%",
                 f"%{needle}%")).fetchall()
        return [r["device_id"] for r in rows]


    def org_onu_rows(self, org_id: str, device_id: int | None = None) -> list[dict]:
        """Slim ONU rows for the PON fault detector (central/ponfault.py) and the
        roster-hygiene checks (central/onuroster.py — serial + onu_id used there,
        ignored by ponfault).

        `rx_dbm`/`severity` ride along for the org-wide optical rollup
        (`api/outages.py:pon_summary`), which has to count crit/weak ONUs over
        the SAME freshest-walk view the fault and capacity checks use — reading
        them from a second query would let the KPI strip and the drill-down
        disagree about which walk they are describing."""
        q = ("SELECT o.device_id, o.onu_key, o.pon_port, o.onu_id, o.name, o.serial,"
             " o.state, o.distance_m, o.last_online_at, o.updated_at,"
             " o.rx_dbm, o.severity, pl.label AS label,"
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


    # ----- reference ONUs (operator-placed witnesses) -------------------------

    def list_onu_places(self, org_id: str) -> list[dict]:
        """Every ONU this org has put on the map — witnesses AND plain locations.

        No longer small by design: an ISP vouches for a handful of power-backed
        subscribers, but the field survey records wherever a tech happens to
        stand, so this can run to the size of the roster. `witness` is what
        separates the two, and every caller that cares must read it."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT mac, lat, lng, label, notes, witness, accuracy_m,"
                " place_source, placed_by, placed_at, created_at, updated_at"
                " FROM onu_places WHERE org_id=? ORDER BY label, mac",
                (org_id,)).fetchall()
        return [dict(r) for r in rows]

    def onu_place_macs(self, org_id: str, *, witness_only: bool = True) -> set[str]:
        """The WITNESS keys only — what ponfault marks power-backed rows with.

        `witness_only=False` asks the other question — "which subscribers have a
        pin at all" — for the survey's coverage count. It defaults TRUE so that
        every existing caller (all of them alerting) keeps the narrow meaning: a
        paging path that accidentally widened to every located drop is exactly
        the failure the witness column was added to prevent.

        The `witness=1` filter is the whole safety property of letting the field
        drop location pins on ordinary subscribers. Without it, geo-tagging a
        street's worth of ONUs would enrol every one of them as a power-backed
        witness, and the next dark subscriber would read as PROOF of a fibre cut
        (`ponfault._witness_verdict`: a witness dark silently ⇒ fiber). Locating
        is an observation; witnessing is a claim about a power supply that
        nothing can detect — they must never be the same write.

        Its own query rather than a comprehension over ``list_onu_places``
        because this one runs on the report cycle, once per optics fold."""
        q = "SELECT mac FROM onu_places WHERE org_id=?"
        if witness_only:
            q += " AND witness=1"
        with self._connect() as conn:
            rows = conn.execute(q, (org_id,)).fetchall()
        return {r["mac"] for r in rows}

    def onu_interfaces(self, org_id: str, device_ids: set[int]) -> dict:
        """Per-ONU ifTable rows for these OLTs, keyed (device_id, first token of
        if_name) — the shape `onuroster.onu_if_token` produces.

        Only the reference-ONU list needs this, and that list is a handful of
        rows, so it takes the device ids it actually wants rather than scanning
        a fleet's switch_ports. The first token is the key because a described
        ONU reads `EPON03ONU5 BSNL-238`."""
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
                      *, witness: bool = True) -> bool:
        """Place (or move) an ONU on the map. `mac` must already be in
        ``onuroster._norm_mac`` form — identity is the caller's to normalize, and
        exactly once, or two spellings of one sticker become two witnesses.

        `witness` defaults TRUE because that is what every caller predating the
        field survey meant, and a silent default of False would quietly retire
        the reference-ONU feature. The field path passes it explicitly.

        A re-place carries the flag it is given, which is deliberate in BOTH
        directions: a tech recording a location for an ONU somebody had vouched
        for must not silently strip its witness status, so the field route
        refuses to downgrade (see `api/devices.field_onu`) rather than relying on
        the value passed here."""
        if not mac:
            return False
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO onu_places (org_id, mac, lat, lng, label, notes,"
                " witness, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(org_id, mac) DO UPDATE SET lat=excluded.lat,"
                " lng=excluded.lng, label=excluded.label, notes=excluded.notes,"
                " witness=excluded.witness, updated_at=excluded.updated_at",
                (org_id, mac, lat, lng, label or None, notes or None,
                 1 if witness else 0, now, now))
            conn.commit()
        return True

    def place_onu_in_field(self, org_id: str, mac: str, lat: float, lng: float,
                           *, witness: bool, accuracy_m: float | None,
                           source: str, placed_by: str,
                           label: str | None = None) -> bool:
        """A subscriber pin taken standing at the drop.

        Separate from `set_onu_place` for the same reason `place_org_device` is
        separate from `set_org_device_location`: it cannot clear a row (lat/lng
        are non-optional), and it always stamps provenance. It also leaves
        `notes` alone — the operator's notes are desk knowledge about the site,
        and a location capture has no business overwriting them with nothing."""
        if not mac:
            return False
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO onu_places (org_id, mac, lat, lng, label, notes,"
                " witness, accuracy_m, place_source, placed_by, placed_at,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(org_id, mac) DO UPDATE SET lat=excluded.lat,"
                " lng=excluded.lng, witness=excluded.witness,"
                " label=COALESCE(excluded.label, onu_places.label),"
                " accuracy_m=excluded.accuracy_m,"
                " place_source=excluded.place_source,"
                " placed_by=excluded.placed_by, placed_at=excluded.placed_at,"
                " updated_at=excluded.updated_at",
                (org_id, mac, lat, lng, label or None, None,
                 1 if witness else 0, accuracy_m, source, placed_by, now,
                 now, now))
            conn.commit()
        return True

    def set_onu_place_label(self, org_id: str, mac: str,
                            label: str | None) -> bool:
        """Rename a located subscriber. Touches the label and NOTHING else.

        Its own method rather than a `place_onu_in_field` call with the old
        coordinates, because re-placing would restamp `accuracy_m`/`place_source`
        /`placed_by` — so correcting a typo in somebody's name would quietly
        downgrade a real 6 m GPS fix to a hand-placed point with no accuracy, and
        reattribute the placement to whoever fixed the spelling.

        Clearing IS allowed here (unlike a pin): a label is descriptive, so an
        empty one is a fact about what the operator knows, not the loss of plant
        record. False = no such placement, which the caller reports as a 404
        rather than silently creating a pin-less row."""
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE onu_places SET label=?, updated_at=?"
                " WHERE org_id=? AND mac=?",
                (label or None, _now_iso(), org_id, mac))
            conn.commit()
        return cur.rowcount > 0

    def onu_place_witness(self, org_id: str, mac: str) -> bool | None:
        """Is this MAC already placed, and as what? None = not placed at all.

        Exists so the field route can refuse to DOWNGRADE a witness: a tech
        recording where a box physically is must never quietly cancel the
        operator's claim that it runs on a UPS, because that claim is invisible
        on the handset and losing it changes a PON verdict."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT witness FROM onu_places WHERE org_id=? AND mac=?",
                (org_id, mac)).fetchone()
        return None if row is None else bool(row["witness"])

    def delete_onu_place(self, org_id: str, mac: str) -> bool:
        """Clearing a reference point is a DELETE — the table is sparse, so
        there is no such thing as a placed-but-unplaced row."""
        with self._write_lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM onu_places WHERE org_id=? AND mac=?",
                               (org_id, mac))
            conn.commit()
        return cur.rowcount > 0


    # ----- subscriber drops (which passive feeds an ONU) ----------------------

    def list_onu_drops(self, org_id: str) -> list[dict]:
        """Every recorded drop for this org: MAC -> the passive it comes off.

        Unlike reference points this table is NOT sparse by intent — the goal is
        one row per subscriber — so callers resolve it against the roster in
        Python rather than joining in SQL: `_norm_mac` is the one identity
        normalizer and it does not get a second spelling inside a query."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT mac, passive_id, created_at, updated_at FROM onu_drops"
                " WHERE org_id=? ORDER BY passive_id, mac", (org_id,)).fetchall()
        return [dict(r) for r in rows]

    def onu_drop_map(self, org_id: str) -> dict[str, int]:
        """Just MAC -> passive_id. The shape every read path actually wants."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT mac, passive_id FROM onu_drops WHERE org_id=?",
                (org_id,)).fetchall()
        return {r["mac"]: r["passive_id"] for r in rows}

    def set_onu_drops(self, org_id: str, macs: list[str], passive_id: int) -> int:
        """Attach these ONUs to a passive. `macs` must already be in
        ``onuroster._norm_mac`` form — identity is the caller's to normalize,
        exactly once, or one sticker inflates a splitter's recorded load.

        Re-attaching MOVES the drop: a subscriber comes off exactly one box, so
        there is nothing to merge and the newest statement wins."""
        if not macs:
            return 0
        now = _now_iso()
        with self._write_lock, self._connect() as conn:
            conn.executemany(
                "INSERT INTO onu_drops (org_id, mac, passive_id, created_at,"
                " updated_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(org_id, mac) DO UPDATE SET"
                " passive_id=excluded.passive_id, updated_at=excluded.updated_at",
                [(org_id, m, passive_id, now, now) for m in macs])
            conn.commit()
        return len(macs)

    def clear_onu_drops(self, org_id: str, macs: list[str]) -> int:
        """Detaching is a DELETE — 'no splitter recorded' is the absence of a
        row, never a row pointing nowhere."""
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
                    # A down OLT has no reachable subscribers — its last walk is
                    # still fresh, but none of those ONUs are online. Count them
                    # in the total (blast radius) yet zero for online.
                    if r["dev_state"] not in ("DOWN", "UNREACHABLE"):
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
