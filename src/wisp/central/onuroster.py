from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from wisp.core.analytics import _parse

STALE_S = 900


def _naive_utc(now: datetime) -> datetime:
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
    return (raw or "").strip().upper()


def search_key(raw: str | None) -> str:

    return "".join(c for c in (raw or "") if c.isalnum()).upper()


def display_name(row: dict) -> str:
    # THE USERNAME IS THE IDENTITY; THE CUSTOMER NAME IS EXTRA INFO (the ISPs'
    # own words, 2026-08-17: "everybody recognise the user by username only").
    # `radius_username` was inserted above `radius_name` on that instruction,
    # and the fleet had already voted with its hands: 253 of the 289 surveyed,
    # billing-linked subscribers carry a hand-typed label that IS the username
    # (punctuation-blind). So this slot was ALREADY showing a username wherever
    # somebody had surveyed the drop, and the billing name wherever nobody had
    # — one fleet rendering two kinds of string in one position, decided by
    # whether a worker had visited. The billing name is also the weaker
    # identifier on three of the four live books (1,442 of rapidnetworks' 1,784
    # names are a single word; MS-Telecom has 779 all-lowercase), while a
    # username is present for every customer and unique by construction.
    #
    # `label` STILL OUTRANKS IT, unchanged: that is the human who stood at the
    # drop, and demoting it would make a typed correction invisible on the
    # single-slot surfaces (the map plate, a chip) — the "a name visible only on
    # the screen that captured it" failure, from the other side. In 88% of cases
    # it is the same string anyway; in the rest it is a deliberate correction.
    for key in ("label", "radius_username", "radius_name", "name",
                "serial", "onu_key"):
        v = row.get(key)
        if v not in (None, ""):
            return str(v)
    return ""


def onu_if_token(pon_port: str | None, onu_id: int | None) -> str | None:


    if not pon_port or onu_id is None:
        return None
    token = pon_port.replace("/", "").strip()
    if not token:
        return None
    return f"{token}ONU{onu_id}"


@dataclass(frozen=True)
class PonCap:
    device_id: int
    device_name: str
    pon_port: str
    onus: int
    limit: int

    def as_dict(self) -> dict:
        return {"device_id": self.device_id, "device_name": self.device_name,
                "pon_port": self.pon_port, "onus": self.onus, "limit": self.limit}


@dataclass(frozen=True)
class DupMac:
    mac: str
    members: tuple[dict, ...]
    online_members: int = 0

    def as_dict(self) -> dict:
        return {"mac": self.mac, "members": [dict(m) for m in self.members],
                "online_members": self.online_members}


def current_roster(rows: list[dict], now: datetime, *,
                   stale_s: int | None = STALE_S) -> list[dict]:
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
    ports: dict[tuple[int, str], list[dict]] = {}
    for r in current_roster(rows, now):
        port = r.get("pon_port")
        if not port:
            continue
        ports.setdefault((r["device_id"], port), []).append(r)

    out: list[PonCap] = []
    for (dev_id, port), onus in ports.items():
        limit = limit_for(dev_id)
        if limit and len(onus) >= limit:
            out.append(PonCap(
                device_id=dev_id,
                device_name=onus[0].get("device_name") or f"#{dev_id}",
                pon_port=port, onus=len(onus), limit=limit))
    out.sort(key=lambda c: (-c.onus, c.device_name, c.pon_port))
    return out


def duplicate_macs(rows: list[dict], now: datetime, *,
                   stale_s: int | None = STALE_S) -> list[DupMac]:
    groups: dict[str, dict[tuple[int, str], dict]] = {}
    for r in current_roster(rows, now, stale_s=stale_s):
        mac = _norm_mac(r.get("serial"))
        if not mac:
            continue
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
