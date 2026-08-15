from __future__ import annotations

# Busy-hour capacity reads (Wave 2, chart E's org half). Read-only: nothing
# here writes, samples or pages — it composes what the historian already
# stored into the "when do I buy backhaul" answer.
#
# EVERYTHING COMES OFF hist_port_hour, ON PURPOSE. The day tier carries
# busy_in_bps/busy_in_hour, which is the same question one grain coarser — but
# it is written only by the NIGHTLY FOLD, so today's hours are not in it and a
# young historian has none at all. Reading the two tiers together would make
# the ranking and the heatmap disagree about the same port on the same screen,
# which is the one thing the count-agreement rule forbids. The hour tier's own
# retention (cfg.hist_port_hour_days, 30 d) is therefore what bounds the
# window, and the API says so when the ask exceeds it.
#
# Two scans of one table rather than one: the per-(port, hour-of-day) grouping
# cannot yield a port's DISTINCT DAY count (summing per-hour day counts
# multiplies it by 24), and coverage is not a number this may guess at. Both
# run on one connection, off the report path, over ≤127 k rows at the
# documented steady state.

HOUR_S = 3600
DAY_S = 86400

# The window predicate rides hist_port_hour's own time column, which carries
# the prune index (_ensure_hist_prune_indexes); org_id and the grouping stay on
# the scan. No new secondary index — the hist tables deliberately keep the PK
# as the read path.
_WINDOW = "WHERE org_id=? AND bucket >= ? AND bucket < ?"


class CapacityStoreMixin:

    def org_port_totals(self, org_id: str, since_s: int,
                        until_s: int) -> list[dict]:
        # One row per sampled port over the whole window: the coverage channel
        # (days/samples/rate_n/up_samples) plus the absolute peaks. `days` is a
        # DISTINCT count of UTC days — a port walked for two hours on one day
        # covers one day, not two.
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT device_id, if_index,"
                " COUNT(*) AS hours,"
                " COUNT(DISTINCT bucket / 86400) AS days,"
                " MIN(bucket) AS first_bucket, MAX(bucket) AS last_bucket,"
                " SUM(samples) AS samples, SUM(rate_n) AS rate_n,"
                " SUM(up_samples) AS up_samples,"
                " MAX(in_max) AS peak_in_bps, MAX(out_max) AS peak_out_bps"
                " FROM hist_port_hour " + _WINDOW +
                " GROUP BY device_id, if_index",
                (org_id, int(since_s), int(until_s))).fetchall()
        return [dict(r) for r in rows]

    def org_port_hour_profile(self, org_id: str, since_s: int,
                              until_s: int) -> list[dict]:
        # The hour-of-day fold: every bucket in the window collapsed onto the
        # 24 hours of the clock. `in_sum`/`rate_n` are carried RAW so the mean
        # is taken once, at the one place the ranking and the heatmap both read
        # (a mean averaged from means would weight a two-sample hour like a
        # twelve-sample one). A group with rate_n = 0 is a walked hour that
        # computed no rate — absent, never zero.
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT device_id, if_index,"
                " ((bucket % 86400) / 3600) AS hod,"
                " COUNT(DISTINCT bucket / 86400) AS days,"
                " SUM(samples) AS samples, SUM(rate_n) AS rate_n,"
                " SUM(in_sum) AS in_sum, SUM(out_sum) AS out_sum,"
                " MAX(in_max) AS in_max, MAX(out_max) AS out_max,"
                " SUM(up_samples) AS up_samples"
                " FROM hist_port_hour " + _WINDOW +
                " GROUP BY device_id, if_index, hod"
                " ORDER BY device_id, if_index, hod",
                (org_id, int(since_s), int(until_s))).fetchall()
        return [dict(r) for r in rows]

    def port_meta(self, org_id: str, device_id: int, if_index: int) -> dict | None:
        # One port's operator columns, for the per-port drill. Straight onto
        # switch_ports' UNIQUE(org_id, device_id, if_index) rather than
        # filtering the org-wide read in Python.
        with self._connect() as conn:
            row = conn.execute(
                "SELECT sp.if_name, sp.if_alias, sp.monitored,"
                " sp.feeds_device_id, sp.uplink_device_id, sp.bw_threshold_mbps,"
                " sp.bw_max_mbps, sp.bw_direction, sp.admin_status,"
                " sp.oper_status, sp.alarm, sp.bw_alarm, sp.bw_high_alarm,"
                " sp.updated_at, d.name AS device_name"
                " FROM switch_ports sp JOIN org_devices d ON d.id = sp.device_id"
                " WHERE sp.org_id=? AND sp.device_id=? AND sp.if_index=?",
                (org_id, int(device_id), int(if_index))).fetchone()
        return dict(row) if row else None

    def org_port_meta(self, org_id: str) -> list[dict]:
        # Every walked port of the org's ACTIVE devices, with the operator
        # columns a walk never writes (the eligibility predicate's inputs) and
        # the live alarm flags. Deliberately unfiltered: eligibility is
        # history.port_eligible's to decide, at exactly one place, or the read
        # side and the write side would drift about which ports are sampled.
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT sp.device_id, sp.if_index, sp.if_name, sp.if_alias,"
                " sp.monitored, sp.feeds_device_id, sp.uplink_device_id,"
                " sp.bw_threshold_mbps, sp.bw_max_mbps, sp.bw_direction,"
                " sp.admin_status, sp.oper_status, sp.alarm, sp.bw_alarm,"
                " sp.bw_high_alarm, sp.updated_at,"
                " d.name AS device_name, d.device_type, d.region,"
                " ds.state AS device_state"
                " FROM switch_ports sp"
                " JOIN org_devices d ON d.id = sp.device_id"
                " LEFT JOIN device_states ds ON ds.device_id = sp.device_id"
                " WHERE sp.org_id=? AND d.is_active=1"
                " ORDER BY d.name, sp.device_id, sp.if_index",
                (org_id,)).fetchall()
        return [dict(r) for r in rows]
