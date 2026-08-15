from __future__ import annotations

# BUSY-HOUR CAPACITY (Wave 2, chart E). The question is "when do I buy
# backhaul", and the action is a purchase — so everything here is a CAPACITY
# fact, never an alarm. A port pinned near its ceiling is reported in neutral
# text with a meter; the only status claim this module makes is one the product
# already made elsewhere (ports.py's bw_high_alarm / bw_alarm / alarm flags,
# echoed verbatim, never re-derived).
#
# ONE DERIVATION FOR BOTH VIEWS. The ranking's "busy hour" is the ARGMAX of the
# very hour-of-day means the heatmap draws, so the darkest cell in a row and the
# figure beside it are the same number by construction — a drill-down that
# disagrees with the list it was opened from is worse than none.
#
# THE WINDOW IS BOUNDED BY THE HOUR TIER, not by MAX_DAYS. hist_port_hour keeps
# cfg.hist_port_hour_days (30) and the day tier's busy columns are written only
# by the nightly fold, so a longer ask cannot be served from one derivation.
# The reply says what was asked for and what was served; the panel says so too.

from datetime import datetime, timezone

from wisp.central import analytics as central_analytics
from wisp.central import history as central_history
from wisp.central.api.common import (in_scope, org_or_400, q_int_or,
                                     reader_or_401, visible_device_ids)

HOUR_S = 3600
DAY_S = 86400

DEFAULT_DAYS = 30

# How many ports the heatmap ships cells for. It is a PREFIX of `ranking`, so
# the two can never name different ports; the panel draws fewer still. Cells
# for every eligible port would be ~4,200 objects on a Home panel's payload.
HEATMAP_PORTS = 10


def _iso_utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(
        tzinfo=None).isoformat(timespec="seconds")


def _mean(total, n) -> float | None:
    # rate_n counts the samples that HAD a rate. Zero of them is "walked, no
    # rate computable" — absent, and absent is never zero.
    return (total / n) if n else None


def direction_bps(in_bps: float | None, out_bps: float | None,
                  direction: str | None) -> float | None:
    # Mirrors ports.py:_bw_above's direction selection, so a utilisation figure
    # and the alarm that fires off the same ceiling can never disagree about
    # WHICH rate is being compared. 'either' is the default there too.
    d = direction or "either"
    if d == "in":
        return in_bps
    if d == "out":
        return out_bps
    if d == "total":
        if in_bps is None or out_bps is None:
            return None
        return in_bps + out_bps
    vals = [v for v in (in_bps, out_bps) if v is not None]
    return max(vals) if vals else None


def fold_cells(rows: list[dict]) -> dict[int, dict]:
    # hour-of-day rows -> {hod: cell}. Cells that computed no rate are DROPPED
    # rather than zero-filled: the caller renders the dead zone for a missing
    # hour, and a heatmap cell shaded like an idle hour would be a lie about an
    # hour nobody measured.
    cells: dict[int, dict] = {}
    for r in rows:
        n = int(r["rate_n"] or 0)
        hod = int(r["hod"])
        cells[hod] = {
            "h": hod,
            "in_bps": _mean(r["in_sum"], n),
            "out_bps": _mean(r["out_sum"], n),
            "peak_in_bps": r.get("in_max"),
            "peak_out_bps": r.get("out_max"),
            "n": n,
            "days": int(r["days"] or 0),
            "samples": int(r["samples"] or 0),
        }
    return {h: c for h, c in cells.items() if c["n"] > 0}


def busiest(cells: dict[int, dict], pick) -> tuple[float | None, int | None]:
    # Ties break on the EARLIER hour so the answer is stable across refetches.
    best_v: float | None = None
    best_h: int | None = None
    for h in sorted(cells):
        v = pick(cells[h])
        if v is None:
            continue
        if best_v is None or v > best_v:
            best_v, best_h = v, h
    return best_v, best_h


def port_label(if_name, if_alias, if_index) -> str:
    base = if_name or f"if{if_index}"
    return f"{base} ({if_alias})" if if_alias else base


def _window(h, qs) -> dict:
    # The hour tier's retention is the ceiling on every window here. Clamped to
    # history_since as well, and floored to the bucket grid so the partial
    # first hour still ships (its `samples` carries its own coverage).
    requested = max(1, q_int_or(qs, "days", DEFAULT_DAYS))
    max_days = max(1, int(h.cfg.hist_port_hour_days))
    days = min(requested, max_days)
    since, until = central_analytics.window(days)
    recording = h.store.history_since()
    if recording:
        since = max(since, recording[:19])
    since_s = (central_history.epoch_s(since) // HOUR_S) * HOUR_S
    until_s = central_history.epoch_s(until) + 1
    return {"since_s": since_s, "until_s": until_s, "days": days,
            "days_requested": requested, "max_days": max_days,
            "clamped": requested > max_days, "recording_since": recording}


def _window_meta(win: dict) -> dict:
    return {"since": _iso_utc(win["since_s"]), "until": _iso_utc(win["until_s"]),
            "days": win["days"], "days_requested": win["days_requested"],
            "max_days": win["max_days"], "clamped": win["clamped"],
            "recording_since": win["recording_since"]}


def capacity(h, qs):
    # The org's busy-hour ranking + the hour-of-day heatmap behind it.
    # OWNER-ONLY: this enumerates every eligible port in the org, which leaks
    # past a worker's assignment scope (the onus()/paging() gate, verbatim).
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    if not h._can_write(user, org):
        h._reply(403, {"error": "owner only"})
        return
    win = _window(h, qs)

    meta = {(m["device_id"], m["if_index"]): m
            for m in h.store.org_port_meta(org)
            if central_history.port_eligible(m)}
    totals = {(t["device_id"], t["if_index"]): t
              for t in h.store.org_port_totals(org, win["since_s"], win["until_s"])}
    by_port: dict[tuple, list[dict]] = {}
    for r in h.store.org_port_hour_profile(org, win["since_s"], win["until_s"]):
        by_port.setdefault((r["device_id"], r["if_index"]), []).append(r)

    ranking = []
    cells_by_port: dict[tuple, dict[int, dict]] = {}
    for key, m in meta.items():
        # A port whose history exists but whose eligibility the operator has
        # since revoked is NOT reported: the ranking's population is the
        # eligible set, and its old rows age out of the hour tier on their own.
        cells = fold_cells(by_port.get(key, []))
        cells_by_port[key] = cells
        tot = totals.get(key, {})
        direction = m["bw_direction"] or "either"
        busy_in, busy_in_h = busiest(cells, lambda c: c["in_bps"])
        busy_out, busy_out_h = busiest(cells, lambda c: c["out_bps"])
        busy, busy_h = busiest(
            cells, lambda c: direction_bps(c["in_bps"], c["out_bps"], direction))
        ceiling = m["bw_max_mbps"]
        # A port with no ceiling recorded gets NO percentage. "Nobody wrote the
        # ceiling down" and "0% used" are different sentences, and inventing a
        # denominator is how a capacity plan gets built on a guess.
        util = (round(busy / (ceiling * 1e6) * 100, 1)
                if (ceiling and busy is not None) else None)
        ranking.append({
            "device_id": key[0], "if_index": key[1],
            "device_name": m["device_name"], "device_type": m["device_type"],
            "region": m["region"], "device_state": m["device_state"],
            "if_name": m["if_name"], "if_alias": m["if_alias"],
            "label": port_label(m["if_name"], m["if_alias"], m["if_index"]),
            "monitored": int(m["monitored"] or 0),
            "feeds_device_id": m["feeds_device_id"],
            "uplink_device_id": m["uplink_device_id"],
            "admin_status": m["admin_status"], "oper_status": m["oper_status"],
            "alarm": int(m["alarm"] or 0),
            "bw_alarm": int(m["bw_alarm"] or 0),
            "bw_high_alarm": int(m["bw_high_alarm"] or 0),
            "bw_max_mbps": ceiling, "bw_threshold_mbps": m["bw_threshold_mbps"],
            "bw_direction": direction, "updated_at": m["updated_at"],
            "busy_bps": busy, "busy_hour": busy_h, "util_pct": util,
            "busy_in_bps": busy_in, "busy_in_hour": busy_in_h,
            "busy_out_bps": busy_out, "busy_out_hour": busy_out_h,
            "peak_in_bps": tot.get("peak_in_bps"),
            "peak_out_bps": tot.get("peak_out_bps"),
            "days": int(tot.get("days") or 0),
            "hour_buckets": int(tot.get("hours") or 0),
            "samples": int(tot.get("samples") or 0),
            "rate_n": int(tot.get("rate_n") or 0),
            "up_samples": int(tot.get("up_samples") or 0),
            "first_bucket": tot.get("first_bucket"),
            "last_bucket": tot.get("last_bucket"),
        })

    # Most pinned first: a measured percentage of a RECORDED ceiling outranks a
    # big number with nothing to judge it against, and the no-ceiling group is
    # counted in the reply so the panel can ask for the missing ceilings rather
    # than pretend it ranked them.
    ranking.sort(key=lambda r: (
        0 if r["util_pct"] is not None else 1,
        -(r["util_pct"] if r["util_pct"] is not None else 0.0),
        -(r["busy_bps"] or 0.0),
        r["device_name"] or "", r["if_index"]))

    heatmap = []
    for row in ranking:
        if len(heatmap) >= HEATMAP_PORTS:
            break
        cells = cells_by_port.get((row["device_id"], row["if_index"]), {})
        if not cells:
            continue
        # `bps` is the cell shaded on screen, resolved through the SAME
        # direction rule the row's busy_bps and util_pct are, and shipped
        # rather than re-derived: the darkest cell of a row and the figure
        # printed beside it are then the same number by construction, with no
        # copy of this rule living in the SPA to drift.
        direction = row["bw_direction"]
        heatmap.append({
            "device_id": row["device_id"], "if_index": row["if_index"],
            "cells": [{**cells[hod],
                       "bps": direction_bps(cells[hod]["in_bps"],
                                            cells[hod]["out_bps"], direction)}
                      for hod in sorted(cells)],
        })

    sampled = [r for r in ranking if r["rate_n"] > 0]
    h._reply(200, {
        **_window_meta(win),
        "eligible": len(ranking),
        "sampled": len(sampled),
        "no_ceiling": sum(1 for r in sampled if r["bw_max_mbps"] is None),
        "heatmap_ports": HEATMAP_PORTS,
        "ranking": ranking,
        "heatmap": heatmap,
    })


def port_history(h, qs):
    # One port's traffic record: the hour-of-day profile (the same fold the org
    # ranking is built on) plus the per-day busy hour behind it. Device-scoped,
    # so a worker sees it for the devices it is assigned and nothing else.
    user = reader_or_401(h)
    if not user:
        return
    try:
        device_id = int((qs.get("device_id") or [None])[0])
        if_index = int((qs.get("if_index") or [None])[0])
    except (TypeError, ValueError):
        h._reply(400, {"error": "device_id and if_index required"})
        return
    org = h.store.device_org(device_id)
    if org is None or not (user["is_superadmin"] or user["org_id"] == org):
        h._reply(403, {"error": "forbidden"})
        return
    if not in_scope(visible_device_ids(h, user, org), device_id):
        h._reply(403, {"error": "forbidden"})
        return

    win = _window(h, qs)
    rows = h.store.port_history(org, device_id, if_index, win["since_s"],
                                win["until_s"], tier="hour")

    # The per-day busy hour is derived from the SAME hour rows rather than read
    # off hist_port_day: the day tier is written by the nightly fold, so
    # today's hours are not in it and a young historian has nothing at all
    # there. Identical definition to the fold's (fold_history_day) — the max
    # HOURLY MEAN of the day, and which hour it fell in.
    hod: dict[int, dict] = {}
    days: dict[int, dict] = {}
    for r in rows:
        bucket = int(r["bucket"])
        n = int(r["rate_n"] or 0)
        cell = hod.setdefault((bucket % DAY_S) // HOUR_S, {
            "hod": (bucket % DAY_S) // HOUR_S, "rate_n": 0, "samples": 0,
            "in_sum": 0.0, "out_sum": 0.0, "in_max": None, "out_max": None,
            "days": 0})
        cell["rate_n"] += n
        cell["samples"] += int(r["samples"] or 0)
        cell["in_sum"] += r["in_sum"] or 0.0
        cell["out_sum"] += r["out_sum"] or 0.0
        cell["days"] += 1        # one bucket per (day, hour-of-day)
        for col in ("in_max", "out_max"):
            v = r[col]
            if v is not None and (cell[col] is None or v > cell[col]):
                cell[col] = v

        day = days.setdefault(bucket - (bucket % DAY_S), {
            "day": bucket - (bucket % DAY_S), "samples": 0, "rate_n": 0,
            "up_samples": 0, "busy_in_bps": None, "busy_in_hour": None,
            "busy_out_bps": None, "busy_out_hour": None})
        day["samples"] += int(r["samples"] or 0)
        day["rate_n"] += n
        day["up_samples"] += int(r["up_samples"] or 0)
        if n:
            hour = (bucket % DAY_S) // HOUR_S
            mean_in = (r["in_sum"] or 0.0) / n
            mean_out = (r["out_sum"] or 0.0) / n
            if day["busy_in_bps"] is None or mean_in > day["busy_in_bps"]:
                day["busy_in_bps"], day["busy_in_hour"] = mean_in, hour
            if day["busy_out_bps"] is None or mean_out > day["busy_out_bps"]:
                day["busy_out_bps"], day["busy_out_hour"] = mean_out, hour

    cells = fold_cells(list(hod.values()))
    busy_in, busy_in_h = busiest(cells, lambda c: c["in_bps"])
    busy_out, busy_out_h = busiest(cells, lambda c: c["out_bps"])
    # The ceiling comparison is resolved HERE, through the same helpers the org
    # ranking uses, rather than in the SPA off the two per-direction figures:
    # for bw_direction='total' those cannot reconstruct it (the busiest hour of
    # in+out is not the sum of each direction's own busiest hour), and a drill
    # that grades a port differently from the list it was opened from is the
    # failure this whole feature is built to avoid.
    meta = h.store.port_meta(org, device_id, if_index) or {}
    direction = meta.get("bw_direction") or "either"
    busy, busy_h = busiest(
        cells, lambda c: direction_bps(c["in_bps"], c["out_bps"], direction))
    ceiling = meta.get("bw_max_mbps")
    util = (round(busy / (ceiling * 1e6) * 100, 1)
            if (ceiling and busy is not None) else None)
    h._reply(200, {
        **_window_meta(win),
        "device_id": device_id, "if_index": if_index,
        "if_name": meta.get("if_name"), "if_alias": meta.get("if_alias"),
        "label": port_label(meta.get("if_name"), meta.get("if_alias"), if_index),
        "device_name": meta.get("device_name"),
        "bw_max_mbps": ceiling, "bw_threshold_mbps": meta.get("bw_threshold_mbps"),
        "bw_direction": direction,
        "busy_bps": busy, "busy_hour": busy_h, "util_pct": util,
        "hours": [cells[k] for k in sorted(cells)],
        "series": [days[k] for k in sorted(days)],
        "busy_in_bps": busy_in, "busy_in_hour": busy_in_h,
        "busy_out_bps": busy_out, "busy_out_hour": busy_out_h,
        "peak_in_bps": max([c["peak_in_bps"] for c in cells.values()
                            if c["peak_in_bps"] is not None], default=None),
        "peak_out_bps": max([c["peak_out_bps"] for c in cells.values()
                             if c["peak_out_bps"] is not None], default=None),
        "days_covered": len(days),
        "rate_n": sum(d["rate_n"] for d in days.values()),
        "samples": sum(d["samples"] for d in days.values()),
        "up_samples": sum(d["up_samples"] for d in days.values()),
    })
