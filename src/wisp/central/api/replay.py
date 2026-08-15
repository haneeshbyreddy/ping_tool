from __future__ import annotations

# THE TIME MACHINE'S one endpoint. It answers "what did this org's fleet look
# like at any moment in the last N days" with an INTERVAL LIST, never a
# per-tick sample: months-deep outage tables exist and a replay that shipped a
# state per device per minute would be a megabyte a day of arithmetic the
# browser can do from three small lists.
#
# The reply is deliberately three kinds of fact, not one:
#   spans   — what the record SAYS (outage rows, the same set
#             analytics.device_reliability reads, so the map replay, the
#             availability strip and the reliability table can never disagree
#             about when a box was down);
#   devices — each device's own recording FLOOR (created_at);
#   blind   — windows in which the probe covering a device was silent.
# The last two exist because `unknown` is a first-class state on the client.
# Without them the reconstruction can only say up/down, and "no outage row
# covers 03:00 last Tuesday" would render as a green pin over four hours
# nobody was watching — the frozen doctrine's exact failure, moved into the
# past.
#
# Epoch SECONDS on the wire (the historian's convention, not the ISO-TEXT one)
# because every consumer does scrub arithmetic on these numbers.

from wisp.central import analytics as central_analytics
from wisp.central.api.common import (org_or_400, q_int_or, reader_or_401,
                                     visible_device_ids)
from wisp.central.history import epoch_s

# The window cap. 90 days is the outage table's useful depth for a REPLAY (the
# reliability strip goes to a year, but nobody scrubs a year minute by minute),
# and it bounds the one unindexed-ish read here.
MAX_DAYS = 90
DEFAULT_DAYS = 7

# An interval: (start, end) in epoch seconds; end None means "still open at
# the end of the window".
Interval = tuple[int, "int | None"]

_OPEN = float("inf")


def _hi(iv: Interval) -> float:
    return _OPEN if iv[1] is None else iv[1]


def stale_intervals(marks: list[dict], since: int, until: int,
                    grace: int) -> dict[str, list[Interval]]:
    # The watchdog's NODE_STALE / NODE_OK transitions folded into per-probe
    # silent windows.
    #
    # A NODE_STALE row is BACK-DATED by the stale threshold, and that is the
    # honest direction rather than the generous one: the watchdog only calls a
    # probe stale BECAUSE nothing has arrived for `central_node_stale_s`, so
    # the silence demonstrably began that long before the row was written.
    # Taking the row's own timestamp would paint three minutes of "up" over a
    # gap we can prove existed.
    out: dict[str, list[Interval]] = {}
    open_at: dict[str, int] = {}
    for m in marks:
        node = m["node_id"]
        try:
            at = epoch_s(m["at"])
        except (ValueError, TypeError):
            continue
        if m["kind"] == "NODE_STALE":
            if node not in open_at:
                open_at[node] = max(since, at - grace)
        elif node in open_at:
            start = open_at.pop(node)
            end = max(start, at)
            if end > since and start < until:
                out.setdefault(node, []).append((max(since, start), end))
    for node, start in open_at.items():
        if start < until:
            out.setdefault(node, []).append((max(since, start), None))
    return out


def intersect(a: list[Interval], b: list[Interval]) -> list[Interval]:
    # Both sides sorted and non-overlapping. Used for a device with NO probe
    # assignment: the pre-assignment default is "every node for this org
    # covers it", so such a device is only unanswerable when EVERY probe was
    # silent at once.
    out: list[Interval] = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(_hi(a[i]), _hi(b[j]))
        if hi > lo:
            out.append((lo, None if hi == _OPEN else int(hi)))
        if _hi(a[i]) < _hi(b[j]):
            i += 1
        else:
            j += 1
    return out


def device_blind(floors: list[dict], by_node: dict[str, list[Interval]],
                 node_ids: list[str]) -> dict[int, list[Interval]]:
    # ABSENCE OF A PROBE RECORD IS NOT EVIDENCE OF A BLACKOUT. An org with no
    # `node_alerts` rows at all gets no blind windows, and an org with no
    # registered probe gets none either — inventing a fleet-wide blackout out
    # of a missing row would be a fabrication in the other direction, and the
    # recording floors already cover "before the record can answer".
    shared: list[Interval] | None = None
    if node_ids:
        shared = by_node.get(node_ids[0], [])
        for n in node_ids[1:]:
            if not shared:
                break
            shared = intersect(shared, by_node.get(n, []))
    out: dict[int, list[Interval]] = {}
    for f in floors:
        node = f.get("assigned_node_id")
        ivs = by_node.get(node, []) if node else (shared or [])
        if ivs:
            out[f["device_id"]] = ivs
    return out


def replay(h, qs):
    # Reader-level and WORKER-REACHABLE, narrowed by the same
    # `visible_device_ids` helper every other list endpoint uses: a worker
    # replays exactly the fleet they can see live, and all three lists narrow
    # TOGETHER off one scope read (a span for a device whose floor was
    # withheld would render as a down mark on a device that is not on this
    # viewer's map).
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    days = min(max(1, q_int_or(qs, "days", DEFAULT_DAYS)), MAX_DAYS)
    since_iso, until_iso = central_analytics.window(days)
    since, until = epoch_s(since_iso), epoch_s(until_iso)
    scope = visible_device_ids(h, user, org)

    floors = [f for f in h.store.replay_device_floors(org)
              if scope is None or f["device_id"] in scope]
    marks = h.store.node_stale_marks(org, since_iso)
    blind = device_blind(
        floors,
        stale_intervals(marks, since, until, h.cfg.central_node_stale_s),
        h.store.org_node_ids(org))

    spans = []
    for o in h.store.outages_in_window(org, since_iso, until_iso):
        did = int(o["device_id"])
        if scope is not None and did not in scope:
            continue
        # The TRUE start ships, never one clipped to the window: an outage
        # that began before `since` is still covering the whole left edge, and
        # a clipped start would read as "it dropped exactly when this window
        # opened". The client clips for drawing.
        spans.append({"outage_id": int(o["id"]), "device_id": did,
                      "start": epoch_s(o["started_at"]),
                      "end": epoch_s(o["resolved_at"]) if o["resolved_at"] else None,
                      "state": o["final_state"]})

    org_floor = h.store.org_recording_floor(org)
    h._reply(200, {
        "since": since, "until": until, "days": days,
        "now": epoch_s(until_iso),
        "org_since": epoch_s(org_floor) if org_floor else None,
        "devices": [{"device_id": f["device_id"],
                     "since": epoch_s(f["created_at"]) if f["created_at"] else None}
                    for f in floors],
        "spans": spans,
        "blind": [{"device_id": did, "start": s, "end": e}
                  for did, ivs in sorted(blind.items()) for s, e in ivs],
    })
