"""ONU-roster hygiene — pure math over onu_optics rows, no I/O.

Two checks the OLT's ONU roster hands us for free, both distinct from the
mass-drop verdict in ponfault.py:

  * per-PON ONU cap — an EPON PON port tops out at a 1:64 split, so a PON that
    has reached its ONU limit can take no more subscribers. `capacity_faults`
    flags every PON at or over its limit (per-OLT override → global default).
  * redundant MAC — one ONU MAC registered on two or more ONU slots means a
    cloned CPE, a bridging loop, or a stale double-registration.
    `duplicate_macs` groups the whole org's roster by normalized serial (the
    DBC/EPON `serial` IS the MAC; a Huawei GPON serial-number collision is the
    same class of fault) and reports any MAC on ≥ 2 distinct slots.

Both read only the CURRENT roster: `current_roster` keeps, per OLT, the rows
from that OLT's freshest walk (one sync_device pass stamps the whole walk with
the same `updated_at`; an ONU dropped from the roster keeps an older stamp and
falls away) and skips an OLT whose newest walk is staler than STALE_S — the
same 900s rule ponfault uses so a down/silent OLT never fabricates a story.
onu_optics never deletes removed-ONU rows, so this current-roster filter is
what keeps zombie rows from over-counting the cap or faking a duplicate.

Like ponfault this module never opens outages and never pages — callers render
it and any alert lives with the caller (central/onualert.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from wisp.core.analytics import _parse

# Same staleness rule as ponfault.STALE_S — an OLT walk older than this is skipped
# outright (the ICMP outage owns a down OLT; stale optics must not tell a second
# story). Kept local so onuroster stands alone.
STALE_S = 900


def _naive_utc(now: datetime) -> datetime:
    """core.analytics._parse yields NAIVE UTC — meet it there."""
    if now.tzinfo is not None:
        return now.astimezone(timezone.utc).replace(tzinfo=None)
    return now


def _ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return _parse(raw)
    except (ValueError, TypeError):
        return None


def _norm_mac(raw: str | None) -> str:
    """Exact, case-insensitive identity for a serial/MAC — no separator stripping
    (that would mangle a Huawei ASCII serial number; same-vendor DBC MACs are
    already formatted identically walk-to-walk)."""
    return (raw or "").strip().upper()


@dataclass(frozen=True)
class PonCap:
    device_id: int
    device_name: str
    pon_port: str
    onus: int          # ONUs currently on this PON
    limit: int         # the cap it reached (per-OLT override or global default)

    def as_dict(self) -> dict:
        return {"device_id": self.device_id, "device_name": self.device_name,
                "pon_port": self.pon_port, "onus": self.onus, "limit": self.limit}


@dataclass(frozen=True)
class DupMac:
    mac: str
    members: tuple[dict, ...]   # {device_id, device_name, pon_port, onu_id, onu_key, state}
    online_members: int = 0     # slots currently ONLINE — ≥2 is a live clone/loop;
                                # fewer is C-Data reg-table history (an ONU that
                                # moved slots leaves its old row forever, offline)

    def as_dict(self) -> dict:
        return {"mac": self.mac, "members": [dict(m) for m in self.members],
                "online_members": self.online_members}


def current_roster(rows: list[dict], now: datetime, *,
                   stale_s: int | None = STALE_S) -> list[dict]:
    """Per-OLT, the rows from that OLT's freshest walk; stale OLTs dropped.
    `stale_s=None` keeps stale OLTs (the staleness-blind view callers use to
    tell 'genuinely gone' from 'walk went stale')."""
    now = _naive_utc(now)
    by_dev: dict[int, list[dict]] = {}
    for r in rows:
        by_dev.setdefault(r["device_id"], []).append(r)

    out: list[dict] = []
    for onus in by_dev.values():
        newest = max((t for r in onus if (t := _ts(r.get("updated_at")))),
                     default=None)
        if newest is None:
            continue
        if stale_s is not None and (now - newest).total_seconds() > stale_s:
            continue
        out.extend(r for r in onus
                   if (t := _ts(r.get("updated_at"))) is not None and t == newest)
    return out


def fresh_device_ids(rows: list[dict], now: datetime, *,
                     stale_s: int = STALE_S) -> set[int]:
    """OLTs whose newest walk is fresh — the ones this sweep actually observed.
    A verdict about a device NOT in this set is a guess; alerting must freeze
    its state rather than clear it (skip = no verdict, the ponfault rule)."""
    now = _naive_utc(now)
    newest: dict[int, datetime] = {}
    for r in rows:
        t = _ts(r.get("updated_at"))
        if t is not None and (r["device_id"] not in newest or t > newest[r["device_id"]]):
            newest[r["device_id"]] = t
    return {dev for dev, t in newest.items()
            if (now - t).total_seconds() <= stale_s}


def capacity_faults(rows: list[dict], now: datetime,
                    limit_for: Callable[[int], int]) -> list[PonCap]:
    """PONs at or over their ONU limit. `limit_for(device_id)` yields the cap
    (per-OLT override → cfg.onu_pon_limit). A PON with no port label is skipped —
    an unnameable 'PON at capacity' page helps no one."""
    ports: dict[tuple[int, str], list[dict]] = {}
    for r in current_roster(rows, now):
        port = r.get("pon_port")
        if not port:
            continue
        ports.setdefault((r["device_id"], port), []).append(r)

    out: list[PonCap] = []
    for (dev_id, port), onus in ports.items():
        limit = limit_for(dev_id)
        # each onu_optics row is a distinct slot (UNIQUE org,device,onu_key)
        if limit and len(onus) >= limit:
            out.append(PonCap(
                device_id=dev_id,
                device_name=onus[0].get("device_name") or f"#{dev_id}",
                pon_port=port, onus=len(onus), limit=limit))
    out.sort(key=lambda c: (-c.onus, c.device_name, c.pon_port))
    return out


def duplicate_macs(rows: list[dict], now: datetime, *,
                   stale_s: int | None = STALE_S) -> list[DupMac]:
    """MACs (serials) registered on ≥ 2 distinct ONU slots across the org's
    current roster."""
    groups: dict[str, dict[tuple[int, str], dict]] = {}
    for r in current_roster(rows, now, stale_s=stale_s):
        mac = _norm_mac(r.get("serial"))
        if not mac:
            continue
        # distinct physical slot = (OLT, onu_key) — a MAC re-listed twice under
        # one key is not a duplicate; two keys sharing it is
        slots = groups.setdefault(mac, {})
        slots[(r["device_id"], r["onu_key"])] = {
            "device_id": r["device_id"],
            "device_name": r.get("device_name") or f"#{r['device_id']}",
            "pon_port": r.get("pon_port"), "onu_id": r.get("onu_id"),
            "onu_key": r["onu_key"], "state": r.get("state"),
        }

    out: list[DupMac] = []
    for mac, slots in groups.items():
        if len(slots) < 2:
            continue
        members = sorted(slots.values(),
                         key=lambda m: (m["device_name"], m["pon_port"] or "",
                                        m["onu_id"] if m["onu_id"] is not None else -1))
        out.append(DupMac(mac=mac, members=tuple(members),
                          online_members=sum(1 for m in members
                                             if m.get("state") == "online")))
    out.sort(key=lambda d: (-d.online_members, -len(d.members), d.mac))
    return out
