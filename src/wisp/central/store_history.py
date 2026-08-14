from __future__ import annotations

from datetime import datetime, timezone

HOUR_S = 3600
DAY_S = 86400

# Belt-and-braces row caps, enforced by prune_history AFTER the age prunes.
# The age prune is the policy; these make unbounded growth impossible even if
# a clock writes wild timestamps that never age out or port eligibility
# explodes. Generous (~4-6x today's expected steady rows) so a legitimately
# growing fleet doesn't trip them silently — enforcement logs a warning, which
# is the signal to revisit both the cap and the fleet maths in viz-plan.md.
HIST_CAPS = {
    "hist_olt_sweep": 100_000,
    "hist_olt_hour": 300_000,
    "hist_olt_day": 100_000,
    "hist_pon_hour": 300_000,
    "hist_pon_day": 600_000,
    "hist_port_sweep": 500_000,
    "hist_port_hour": 600_000,
    "hist_port_day": 600_000,
    "hist_device_day": 200_000,
    "hist_radius_day": 20_000,
}

# The time column each table is pruned and capped on.
_HIST_TIME_COL = {
    "hist_olt_sweep": "ts",
    "hist_olt_hour": "bucket",
    "hist_olt_day": "day",
    "hist_pon_hour": "bucket",
    "hist_pon_day": "day",
    "hist_port_sweep": "ts",
    "hist_port_hour": "bucket",
    "hist_port_day": "day",
    "hist_device_day": "day",
    "hist_radius_day": "day",
}

# NULL-ignoring running MIN/MAX for the hour-tier upserts: sqlite's scalar
# min()/max() return NULL when either operand is NULL, which would let one
# unmeasured sweep erase an hour's extreme.
_KEEP_MIN = ("CASE WHEN {c} IS NULL THEN excluded.{c}"
             " WHEN excluded.{c} IS NULL THEN {c}"
             " ELSE min({c}, excluded.{c}) END")
_KEEP_MAX = ("CASE WHEN {c} IS NULL THEN excluded.{c}"
             " WHEN excluded.{c} IS NULL THEN {c}"
             " ELSE max({c}, excluded.{c}) END")


class HistoryStoreMixin:

    # -- meta stamps ---------------------------------------------------------

    @staticmethod
    def _stamp_history_since(conn) -> None:
        # history_since: what "recording since <date>" renders. Set once, at
        # the migration that created the tables, never touched again.
        # hist_folded_through: the covered-through stamp that keeps "no day
        # row" unambiguous (absent + folded >= day = a true gap; folded < day
        # = not yet folded). Starts at yesterday's bucket — vacuously covered,
        # since recording started today.
        row = conn.execute(
            "SELECT value FROM meta WHERE key='history_since'").fetchone()
        if row:
            return
        now = datetime.now(timezone.utc)
        conn.execute("INSERT INTO meta (key, value) VALUES ('history_since', ?)",
                     (now.isoformat(timespec="seconds"),))
        day0 = (int(now.timestamp()) // DAY_S) * DAY_S
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value)"
            " VALUES ('hist_folded_through', ?)", (str(day0 - DAY_S),))

    def history_since(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='history_since'").fetchone()
        return row["value"] if row else None

    def hist_folded_through(self) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='hist_folded_through'").fetchone()
        try:
            return int(row["value"]) if row else None
        except (TypeError, ValueError):
            return None

    def set_hist_folded_through(self, day_s: int) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value)"
                " VALUES ('hist_folded_through', ?)", (str(int(day_s)),))
            conn.commit()

    # -- sweep-time writes (one transaction per walk) ------------------------

    def record_olt_sweep(self, org_id: str, device_id: int, ts_s: int,
                         olt: dict, pons: list[dict]) -> None:
        bucket = (ts_s // HOUR_S) * HOUR_S
        med = olt.get("rx_med")
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO hist_olt_sweep (org_id, device_id, ts,"
                " onus, online, warn, crit, measured, rx_med, rx_p10, rx_min)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (org_id, device_id, ts_s, olt["onus"], olt["online"],
                 olt["warn"], olt["crit"], olt["measured"], med,
                 olt.get("rx_p10"), olt.get("rx_min")))
            conn.execute(
                "INSERT INTO hist_olt_hour (org_id, device_id, bucket, samples,"
                " onus_max, online_min, warn_max, crit_max, measured_min,"
                " rx_med_sum, rx_med_n, rx_min)"
                " VALUES (?,?,?,1,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(device_id, bucket) DO UPDATE SET"
                " samples = samples + 1,"
                " onus_max = max(onus_max, excluded.onus_max),"
                " online_min = min(online_min, excluded.online_min),"
                " warn_max = max(warn_max, excluded.warn_max),"
                " crit_max = max(crit_max, excluded.crit_max),"
                " measured_min = min(measured_min, excluded.measured_min),"
                " rx_med_sum = rx_med_sum + excluded.rx_med_sum,"
                " rx_med_n = rx_med_n + excluded.rx_med_n,"
                " rx_min = " + _KEEP_MIN.format(c="rx_min"),
                (org_id, device_id, bucket, olt["onus"], olt["online"],
                 olt["warn"], olt["crit"], olt["measured"],
                 med if med is not None else 0.0,
                 1 if med is not None else 0, olt.get("rx_min")))
            for pon in pons:
                pmed = pon.get("rx_med")
                conn.execute(
                    "INSERT INTO hist_pon_hour (org_id, device_id, pon_port,"
                    " bucket, samples, onus_max, online_min, crit_max,"
                    " rx_med_sum, rx_med_n, rx_min)"
                    " VALUES (?,?,?,?,1,?,?,?,?,?,?)"
                    " ON CONFLICT(device_id, pon_port, bucket) DO UPDATE SET"
                    " samples = samples + 1,"
                    " onus_max = max(onus_max, excluded.onus_max),"
                    " online_min = min(online_min, excluded.online_min),"
                    " crit_max = max(crit_max, excluded.crit_max),"
                    " rx_med_sum = rx_med_sum + excluded.rx_med_sum,"
                    " rx_med_n = rx_med_n + excluded.rx_med_n,"
                    " rx_min = " + _KEEP_MIN.format(c="rx_min"),
                    (org_id, device_id, pon["pon_port"], bucket, pon["onus"],
                     pon["online"], pon["crit"],
                     pmed if pmed is not None else 0.0,
                     1 if pmed is not None else 0, pon.get("rx_min")))
            conn.commit()

    def record_port_sweeps(self, org_id: str, device_id: int, ts_s: int,
                           rows: list[tuple]) -> None:
        # rows: (if_index, in_bps|None, out_bps|None, oper_up: bool)
        if not rows:
            return
        bucket = (ts_s // HOUR_S) * HOUR_S
        with self._write_lock, self._connect() as conn:
            for if_index, in_bps, out_bps, oper_up in rows:
                rated = 1 if (in_bps is not None and out_bps is not None) else 0
                conn.execute(
                    "INSERT OR REPLACE INTO hist_port_sweep (org_id, device_id,"
                    " if_index, ts, in_bps, out_bps, oper_up)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (org_id, device_id, if_index, ts_s, in_bps, out_bps,
                     1 if oper_up else 0))
                conn.execute(
                    "INSERT INTO hist_port_hour (org_id, device_id, if_index,"
                    " bucket, samples, rate_n, in_sum, in_max, out_sum,"
                    " out_max, up_samples)"
                    " VALUES (?,?,?,?,1,?,?,?,?,?,?)"
                    " ON CONFLICT(device_id, if_index, bucket) DO UPDATE SET"
                    " samples = samples + 1,"
                    " rate_n = rate_n + excluded.rate_n,"
                    " in_sum = in_sum + excluded.in_sum,"
                    " in_max = " + _KEEP_MAX.format(c="in_max") + ","
                    " out_sum = out_sum + excluded.out_sum,"
                    " out_max = " + _KEEP_MAX.format(c="out_max") + ","
                    " up_samples = up_samples + excluded.up_samples",
                    (org_id, device_id, if_index, bucket, rated,
                     in_bps if rated else 0.0, in_bps if rated else None,
                     out_bps if rated else 0.0, out_bps if rated else None,
                     1 if oper_up else 0))
            conn.commit()

    def upsert_radius_day(self, org_id: str, day_s: int, counts: dict) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO hist_radius_day (org_id, day, customers,"
                " active, expired, expiring7, linked) VALUES (?,?,?,?,?,?,?)",
                (org_id, day_s, counts["customers"], counts["active"],
                 counts["expired"], counts["expiring7"], counts["linked"]))
            conn.commit()

    # -- the nightly fold ----------------------------------------------------

    def fold_history_day(self, day_s: int) -> int:
        # Folds ONE complete UTC day from the hour tiers (and device_rollups)
        # into the day tables. Idempotent: every column is replaced, so a
        # re-run over the same day converges. Returns rows written.
        lo, hi = day_s, day_s + DAY_S
        lo_iso = datetime.fromtimestamp(lo, tz=timezone.utc).replace(
            tzinfo=None).isoformat(timespec="seconds")
        hi_iso = datetime.fromtimestamp(hi, tz=timezone.utc).replace(
            tzinfo=None).isoformat(timespec="seconds")
        written = 0
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO hist_olt_day (org_id, device_id, day, samples,"
                " onus_max, online_min, warn_max, crit_max, measured_min,"
                " rx_med_sum, rx_med_n, rx_min)"
                " SELECT org_id, device_id, ?, SUM(samples), MAX(onus_max),"
                "  MIN(online_min), MAX(warn_max), MAX(crit_max),"
                "  MIN(measured_min), SUM(rx_med_sum), SUM(rx_med_n), MIN(rx_min)"
                " FROM hist_olt_hour WHERE bucket >= ? AND bucket < ?"
                " GROUP BY device_id"
                " ON CONFLICT(device_id, day) DO UPDATE SET"
                " samples=excluded.samples, onus_max=excluded.onus_max,"
                " online_min=excluded.online_min, warn_max=excluded.warn_max,"
                " crit_max=excluded.crit_max, measured_min=excluded.measured_min,"
                " rx_med_sum=excluded.rx_med_sum, rx_med_n=excluded.rx_med_n,"
                " rx_min=excluded.rx_min",
                (day_s, lo, hi))
            written += cur.rowcount
            cur = conn.execute(
                "INSERT INTO hist_pon_day (org_id, device_id, pon_port, day,"
                " samples, onus_max, online_min, crit_max, rx_med_sum,"
                " rx_med_n, rx_min)"
                " SELECT org_id, device_id, pon_port, ?, SUM(samples),"
                "  MAX(onus_max), MIN(online_min), MAX(crit_max),"
                "  SUM(rx_med_sum), SUM(rx_med_n), MIN(rx_min)"
                " FROM hist_pon_hour WHERE bucket >= ? AND bucket < ?"
                " GROUP BY device_id, pon_port"
                " ON CONFLICT(device_id, pon_port, day) DO UPDATE SET"
                " samples=excluded.samples, onus_max=excluded.onus_max,"
                " online_min=excluded.online_min, crit_max=excluded.crit_max,"
                " rx_med_sum=excluded.rx_med_sum, rx_med_n=excluded.rx_med_n,"
                " rx_min=excluded.rx_min",
                (day_s, lo, hi))
            written += cur.rowcount

            # Ports in Python: the busy_* columns need per-hour means compared
            # across the day, which SQL GROUP BY can't express in one pass.
            ports: dict[tuple, dict] = {}
            for r in conn.execute(
                    "SELECT * FROM hist_port_hour WHERE bucket >= ? AND bucket < ?",
                    (lo, hi)):
                key = (r["device_id"], r["if_index"])
                agg = ports.setdefault(key, {
                    "org_id": r["org_id"], "samples": 0, "rate_n": 0,
                    "in_sum": 0.0, "in_max": None, "out_sum": 0.0,
                    "out_max": None, "up_samples": 0,
                    "busy_in": None, "busy_out": None})
                agg["samples"] += r["samples"]
                agg["rate_n"] += r["rate_n"]
                agg["in_sum"] += r["in_sum"]
                agg["out_sum"] += r["out_sum"]
                agg["up_samples"] += r["up_samples"]
                for col in ("in_max", "out_max"):
                    v = r[col]
                    if v is not None and (agg[col] is None or v > agg[col]):
                        agg[col] = v
                if r["rate_n"] > 0:
                    hour = (r["bucket"] % DAY_S) // HOUR_S
                    mean_in = r["in_sum"] / r["rate_n"]
                    mean_out = r["out_sum"] / r["rate_n"]
                    if agg["busy_in"] is None or mean_in > agg["busy_in"][0]:
                        agg["busy_in"] = (mean_in, hour)
                    if agg["busy_out"] is None or mean_out > agg["busy_out"][0]:
                        agg["busy_out"] = (mean_out, hour)
            for (device_id, if_index), a in ports.items():
                conn.execute(
                    "INSERT OR REPLACE INTO hist_port_day (org_id, device_id,"
                    " if_index, day, samples, rate_n, in_sum, in_max, out_sum,"
                    " out_max, up_samples, busy_in_bps, busy_in_hour,"
                    " busy_out_bps, busy_out_hour)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (a["org_id"], device_id, if_index, day_s, a["samples"],
                     a["rate_n"], a["in_sum"], a["in_max"], a["out_sum"],
                     a["out_max"], a["up_samples"],
                     a["busy_in"][0] if a["busy_in"] else None,
                     a["busy_in"][1] if a["busy_in"] else None,
                     a["busy_out"][0] if a["busy_out"] else None,
                     a["busy_out"][1] if a["busy_out"] else None))
                written += 1

            cur = conn.execute(
                "INSERT INTO hist_device_day (org_id, device_id, day, samples,"
                " down_samples, latency_sum, latency_n, loss_sum)"
                " SELECT org_id, device_id, ?, SUM(samples), SUM(down_samples),"
                "  SUM(latency_sum), SUM(latency_count), SUM(loss_sum)"
                " FROM device_rollups WHERE bucket >= ? AND bucket < ?"
                " GROUP BY device_id"
                " ON CONFLICT(device_id, day) DO UPDATE SET"
                " samples=excluded.samples, down_samples=excluded.down_samples,"
                " latency_sum=excluded.latency_sum, latency_n=excluded.latency_n,"
                " loss_sum=excluded.loss_sum",
                (day_s, lo_iso, hi_iso))
            written += cur.rowcount
            conn.commit()
        return written

    # -- prune + caps --------------------------------------------------------

    def prune_history(self, cutoffs: dict[str, int],
                      caps: dict[str, int] = HIST_CAPS) -> dict[str, int]:
        # cutoffs: {table: epoch_s} — rows strictly older are deleted. Caps
        # delete oldest-beyond-N afterwards; the OFFSET subquery yields NULL
        # when the table is under its cap, and NULL comparisons delete nothing.
        removed: dict[str, int] = {}
        with self._write_lock, self._connect() as conn:
            for table, cutoff in cutoffs.items():
                col = _HIST_TIME_COL[table]
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE {col} < ?", (int(cutoff),))
                n = cur.rowcount
                cap = caps.get(table)
                if cap:
                    # The subquery names the cap-th newest time value; deleting
                    # strictly older keeps the newest `cap` rows (more when
                    # several rows share the boundary value — a bound, not an
                    # exact count). Under the cap it yields NULL and the
                    # NULL comparison deletes nothing.
                    cur = conn.execute(
                        f"DELETE FROM {table} WHERE {col} < ("
                        f" SELECT {col} FROM {table} ORDER BY {col} DESC"
                        f" LIMIT 1 OFFSET ?)", (int(cap) - 1,))
                    n += cur.rowcount
                if n:
                    removed[table] = n
            conn.commit()
        return removed

    # -- readers (Stage 3 endpoints build on these; tests drive them now) ----

    def olt_history(self, org_id: str, device_id: int, since_s: int,
                    until_s: int, tier: str = "hour") -> list[dict]:
        table, col = {"sweep": ("hist_olt_sweep", "ts"),
                      "hour": ("hist_olt_hour", "bucket"),
                      "day": ("hist_olt_day", "day")}[tier]
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE org_id=? AND device_id=?"
                f" AND {col} >= ? AND {col} < ? ORDER BY {col}",
                (org_id, device_id, int(since_s), int(until_s))).fetchall()
        return [dict(r) for r in rows]

    def pon_history(self, org_id: str, device_id: int, pon_port: str,
                    since_s: int, until_s: int, tier: str = "hour") -> list[dict]:
        table, col = {"hour": ("hist_pon_hour", "bucket"),
                      "day": ("hist_pon_day", "day")}[tier]
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE org_id=? AND device_id=?"
                f" AND pon_port=? AND {col} >= ? AND {col} < ? ORDER BY {col}",
                (org_id, device_id, pon_port, int(since_s),
                 int(until_s))).fetchall()
        return [dict(r) for r in rows]

    def port_history(self, org_id: str, device_id: int, if_index: int,
                     since_s: int, until_s: int, tier: str = "hour") -> list[dict]:
        table, col = {"sweep": ("hist_port_sweep", "ts"),
                      "hour": ("hist_port_hour", "bucket"),
                      "day": ("hist_port_day", "day")}[tier]
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE org_id=? AND device_id=?"
                f" AND if_index=? AND {col} >= ? AND {col} < ? ORDER BY {col}",
                (org_id, device_id, if_index, int(since_s),
                 int(until_s))).fetchall()
        return [dict(r) for r in rows]

    def org_optics_hours(self, org_id: str, since_s: int,
                         until_s: int) -> list[dict]:
        # The org's ONU story at hour grain: per-OLT hourly rows summed per
        # bucket. crit/warn are sums of each OLT's HOURLY WORST (crit_max), so
        # the series reads "the worst this hour" — labeled that way wherever
        # it renders. `olts` says how many OLTs contributed to a bucket, which
        # is the coverage channel (a bucket missing half the fleet's walks is
        # a different sentence from a quiet fleet).
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT bucket, COUNT(*) AS olts, SUM(samples) AS samples,"
                " SUM(onus_max) AS onus, SUM(online_min) AS online,"
                " SUM(warn_max) AS warn, SUM(crit_max) AS crit"
                " FROM hist_olt_hour WHERE org_id=? AND bucket >= ? AND bucket < ?"
                " GROUP BY bucket ORDER BY bucket",
                (org_id, int(since_s), int(until_s))).fetchall()
        return [dict(r) for r in rows]

    def device_day_history(self, org_id: str, device_id: int, since_s: int,
                           until_s: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM hist_device_day WHERE org_id=? AND device_id=?"
                " AND day >= ? AND day < ? ORDER BY day",
                (org_id, device_id, int(since_s), int(until_s))).fetchall()
        return [dict(r) for r in rows]

    def radius_day_history(self, org_id: str, since_s: int,
                           until_s: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM hist_radius_day WHERE org_id=?"
                " AND day >= ? AND day < ? ORDER BY day",
                (org_id, int(since_s), int(until_s))).fetchall()
        return [dict(r) for r in rows]
