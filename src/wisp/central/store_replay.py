from __future__ import annotations

# Reads for the time machine (`api/replay.py`). Everything here answers one
# question: WHAT COULD THE RECORD SAY ABOUT A MOMENT IN THE PAST? — which is
# not the same as "what was down". A replay that paints a device green because
# no outage row covers 03:00 last Tuesday is lying whenever the probe was
# silent then, or the device did not exist yet. So these three reads exist
# purely to supply the honest floors and blind windows the reconstruction
# needs; the outage spans themselves come from `outages_in_window`, the same
# read `analytics.device_reliability` and the availability strip already use
# (count agreement: the replay and the reliability chart can never disagree
# about when a box was down, because they read one row set).
#
# Deliberately NOT folded into `list_org_devices` — that is the hottest query
# in the app and this is a per-page read of a page nobody has open most of the
# time.


class ReplayStoreMixin:

    def replay_device_floors(self, org_id: str) -> list[dict]:
        # A device's own recording floor. Before its `created_at` the record
        # cannot answer for it AT ALL — it is not that the box was up, it is
        # that nothing was watching a box that had not been entered yet. That
        # is `unknown`, and it is the difference between a replay of last
        # month showing a fleet of nine and showing today's fleet of eleven
        # with two impossible green pins.
        #
        # `assigned_node_id` rides along because probe blindness is per PROBE:
        # when an edge goes silent every device it carries is unanswerable,
        # and a NULL here means "every node for this org covers it" (the
        # pre-assignment default), which is a different, wider rule.
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, created_at, assigned_node_id FROM org_devices"
                " WHERE org_id=? AND is_active=1", (org_id,)).fetchall()
        return [{"device_id": int(r["id"]), "created_at": r["created_at"],
                 "assigned_node_id": r["assigned_node_id"]} for r in rows]

    def org_recording_floor(self, org_id: str) -> str | None:
        # The org's own floor: the earlier of when the org row appeared and
        # its first outage. Two sources because either can be the earlier
        # truth — `_ensure_org` creates the row on first ingest, but a row
        # recreated after a delete would post-date outages that plainly did
        # get recorded. The MIN of the two is the earliest moment this record
        # demonstrably answers for; anything before it is `unknown`.
        with self._connect() as conn:
            row = conn.execute(
                "SELECT (SELECT created_at FROM orgs WHERE org_id=?) AS org,"
                "       (SELECT MIN(started_at) FROM outages WHERE org_id=?)"
                "         AS first_outage", (org_id, org_id)).fetchone()
        if not row:
            return None
        cands = [t for t in (row["org"], row["first_outage"]) if t]
        return min(cands) if cands else None

    def node_stale_marks(self, org_id: str, since: str,
                         limit: int = 2000) -> list[dict]:
        # The watchdog's own transition record (`node_alerts`, NODE_STALE /
        # NODE_OK), which is the only history this product keeps of whether a
        # probe was reporting. Transition-only, so a handful of rows per node
        # per month — an interval list, never a per-tick sample.
        #
        # ONE ROW BEFORE THE WINDOW PER NODE IS INCLUDED, or a probe that went
        # silent last Tuesday and is still silent shows no mark inside a
        # 24-hour window and the whole blackout reads as "up". `status` is
        # deliberately ignored: a page that FAILED to send still records a
        # transition that happened, and whether WhatsApp went through says
        # nothing about whether the probe was reporting.
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT node_id, kind, created_at FROM node_alerts"
                " WHERE org_id=? AND kind IN ('NODE_STALE','NODE_OK')"
                "   AND (created_at >= ? OR id IN ("
                "        SELECT MAX(id) FROM node_alerts"
                "         WHERE org_id=? AND kind IN ('NODE_STALE','NODE_OK')"
                "           AND created_at < ? GROUP BY node_id))"
                " ORDER BY created_at, id LIMIT ?",
                (org_id, since, org_id, since, limit)).fetchall()
        return [{"node_id": r["node_id"], "kind": r["kind"],
                 "at": r["created_at"]} for r in rows]

    def org_node_ids(self, org_id: str) -> list[str]:
        # Which probes cover this org, by the `node_liveness` rule rather than
        # `SELECT * FROM nodes` — that table remembers every identity ever
        # seen, and a revoked probe's ancient staleness must not blind a
        # device that some live probe has been reporting all along.
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT n.node_id FROM nodes n WHERE n.org_id=?"
                " AND (NOT EXISTS (SELECT 1 FROM node_tokens nt"
                "                   WHERE nt.org_id=n.org_id)"
                "  OR EXISTS (SELECT 1 FROM node_tokens nt"
                "              WHERE nt.org_id=n.org_id AND nt.node_id=n.node_id"
                "                AND nt.revoked_at IS NULL))", (org_id,))
            return sorted({r["node_id"] for r in rows})
