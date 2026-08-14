from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime

from wisp.central import onuroster
from wisp.central.webmacs import normalise_mac

MAX_ROWS = 50000

MATCH_MAC = "mac"
MATCH_NAME = "name"

MAX_SLOT_MACS = 128

_MAC_SPLIT = re.compile(r"[,;/|]+|\s+")


@dataclass(frozen=True, slots=True)
class RadiusLink:
    device_id: int
    onu_key: str
    username: str
    match_by: str
    account_id: int | None = None


@dataclass(slots=True)
class LinkResult:
    links: list[RadiusLink]
    by_mac: int = 0
    by_name: int = 0
    ambiguous_mac: int = 0
    ambiguous_name: int = 0
    unmatched: int = 0
    crowded_slots: int = 0
    cross_panel: int = 0

    def as_dict(self) -> dict:
        return {"links": len(self.links), "by_mac": self.by_mac,
                "by_name": self.by_name, "ambiguous_mac": self.ambiguous_mac,
                "ambiguous_name": self.ambiguous_name, "unmatched": self.unmatched,
                "crowded_slots": self.crowded_slots,
                "cross_panel": self.cross_panel}


def mac_field(raw) -> str | None:

    text = str(raw or "").strip()
    if not text:
        return None
    found = set()
    for part in _MAC_SPLIT.split(text):
        mac = normalise_mac(part)
        if mac:
            found.add(mac)
    return found.pop() if len(found) == 1 else None


@dataclass(slots=True)
class Roster:
    customers: list[dict]
    data_rows: int
    skipped: int
    missing_headings: tuple[str, ...]

    @property
    def with_mac(self) -> int:
        return sum(1 for c in self.customers if c.get("mac"))


def _text(raw) -> str | None:
    val = str(raw or "").strip()
    return val or None


def parse_roster(text: str, profile) -> Roster:

    reader = csv.DictReader(io.StringIO(text))
    heads = [h.strip() for h in (reader.fieldnames or [])]
    lookup = {h.strip().lower(): h for h in heads}

    wanted: dict[str, str] = {}
    missing: list[str] = []
    for field_name in profile.columns:
        heading = profile.heading_of(field_name)
        real = lookup.get(str(heading or "").strip().lower())
        if real is None:
            missing.append(field_name)
            continue
        wanted[field_name] = real

    out: list[dict] = []
    seen: set[str] = set()
    rows = skipped = 0
    if "username" in wanted:
        for raw in reader:
            rows += 1
            if rows > MAX_ROWS:
                break
            username = _text(raw.get(wanted["username"]))
            if not username:
                skipped += 1
                continue
            key = username.lower()
            if key in seen:
                skipped += 1
                continue
            seen.add(key)
            rec: dict = {"username": username}
            for field_name, col in wanted.items():
                if field_name == "username":
                    continue
                rec[field_name] = _text(raw.get(col))
            rec["mac"] = mac_field(rec.get("mac"))
            rec["status"] = profile.status_of(rec.get("status"))
            out.append(rec)
    return Roster(customers=out, data_rows=rows, skipped=skipped,
                  missing_headings=tuple(sorted(missing)))


_TIME_RE = r"(?:\s+(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp][Mm])?)?"
_NUMERIC_RE = re.compile(
    r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})" + _TIME_RE + r"$")
_ISO_RE = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{2})(?::\d{2})?)?")
_NAMED_DMY_RE = re.compile(
    r"^(\d{1,2})[ -]([A-Za-z]{3,9})\.?,?[ -](\d{4})" + _TIME_RE + r"$")
_NAMED_MDY_RE = re.compile(
    r"^([A-Za-z]{3,9})\.?,?\s+(\d{1,2}),?\s+(\d{4})" + _TIME_RE + r"$")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"))}


def _clock(hh, mm, ampm) -> tuple[int, int]:
    hour = int(hh) if hh else 0
    minute = int(mm) if mm else 0
    half = (ampm or "").lower()
    if half == "pm" and hour != 12:
        hour += 12
    elif half == "am" and hour == 12:
        hour = 0
    return hour, minute


def parse_expiry(text, date_format: str) -> str | None:

    raw = " ".join(str(text or "").split())
    if not raw or not date_format:
        return None
    try:
        if date_format in ("dmy", "mdy"):
            hit = _NUMERIC_RE.match(raw)
            if not hit:
                return None
            a, b = int(hit.group(1)), int(hit.group(2))
            day, month = (a, b) if date_format == "dmy" else (b, a)
            hour, minute = _clock(hit.group(4), hit.group(5), hit.group(6))
            return datetime(int(hit.group(3)), month, day, hour, minute).isoformat()
        if date_format == "iso":
            hit = _ISO_RE.match(raw)
            if not hit:
                return None
            hour, minute = _clock(hit.group(4), hit.group(5), None)
            return datetime(int(hit.group(1)), int(hit.group(2)),
                            int(hit.group(3)), hour, minute).isoformat()
        if date_format == "named-month":
            hit = _NAMED_DMY_RE.match(raw)
            day_idx, month_idx = (1, 2) if hit else (2, 1)
            hit = hit or _NAMED_MDY_RE.match(raw)
            if not hit:
                return None
            month = _MONTHS.get(hit.group(month_idx)[:3].lower())
            if month is None:
                return None
            hour, minute = _clock(hit.group(4), hit.group(5), hit.group(6))
            return datetime(int(hit.group(3)), month, int(hit.group(day_idx)),
                            hour, minute).isoformat()
    except ValueError:
        return None
    return None


def days_until(expiry_iso: str | None, today: date) -> int | None:
    if not expiry_iso:
        return None
    try:
        return (datetime.fromisoformat(expiry_iso).date() - today).days
    except ValueError:
        return None


def _sole(values: set) -> object | None:
    return next(iter(values)) if len(values) == 1 else None


def _claims_by_mac(customers: list[dict]) -> tuple[dict[str, str | None], int]:

    ordered: dict[str, list[tuple]] = {}
    for cust in customers:
        if cust.get("mac"):
            ordered.setdefault(cust["mac"], []).append(
                (cust.get("account_id"), cust["username"]))

    claims: dict[str, str | None] = {}
    cross = 0
    for mac, rows in ordered.items():
        first = rows[0][0]
        mine = [user for account, user in rows if account == first]
        if len(mine) != len(rows):
            cross += 1
        claims[mac] = mine[0] if len(set(mine)) == 1 else None
    return claims, cross


def link_customers(customers: list[dict], mac_rows: list[dict],
                   onu_rows: list[dict], *,
                   max_slot_macs: int = MAX_SLOT_MACS) -> LinkResult:

    per_slot: dict[tuple[int, str], int] = {}
    for row in mac_rows or ():
        slot = (int(row["device_id"]), str(row["onu_key"]))
        per_slot[slot] = per_slot.get(slot, 0) + 1
    crowded = {slot for slot, n in per_slot.items() if n > max_slot_macs}

    slots_for_mac: dict[str, set[tuple[int, str]]] = {}
    for row in mac_rows or ():
        mac = normalise_mac(row.get("mac") or "")
        if not mac:
            continue
        slot = (int(row["device_id"]), str(row["onu_key"]))
        if slot in crowded:
            continue
        slots_for_mac.setdefault(mac, set()).add(slot)

    users_for_mac, cross_panel = _claims_by_mac(customers)

    slots_for_name: dict[str, set[tuple[int, str]]] = {}
    for row in onu_rows or ():
        key = onuroster.search_key(row.get("name"))
        if not key:
            continue
        slots_for_name.setdefault(key, set()).add(
            (int(row["device_id"]), str(row["onu_key"])))

    users_for_name: dict[str, set[str]] = {}
    for cust in customers:
        key = onuroster.search_key(cust["username"])
        if key:
            users_for_name.setdefault(key, set()).add(cust["username"])

    res = LinkResult(links=[], crowded_slots=len(crowded), cross_panel=cross_panel)
    taken: set[tuple[int, str]] = set()

    wanted: dict[tuple[int, str], list[dict]] = {}
    for cust in customers:
        mac = cust.get("mac")
        if not mac:
            continue
        slots = slots_for_mac.get(mac)
        if not slots:
            continue
        slot = _sole(slots)
        if slot is None or users_for_mac.get(mac) != cust["username"]:
            res.ambiguous_mac += 1
            continue
        wanted.setdefault(slot, []).append(cust)

    for slot, claimants in wanted.items():
        if len(claimants) != 1:
            res.ambiguous_mac += len(claimants)
            continue
        won = claimants[0]
        taken.add(slot)
        res.links.append(RadiusLink(slot[0], slot[1], won["username"], MATCH_MAC,
                                    won.get("account_id")))
        res.by_mac += 1

    linked = {(l.account_id, l.username) for l in res.links}
    for cust in customers:
        if (cust.get("account_id"), cust["username"]) in linked:
            continue
        key = onuroster.search_key(cust["username"])
        slots = slots_for_name.get(key) if key else None
        if not slots:
            res.unmatched += 1
            continue
        slot = _sole(slots)
        if slot is None or _sole(users_for_name.get(key, set())) is None:
            res.ambiguous_name += 1
            continue
        if slot in taken:
            res.unmatched += 1
            continue
        taken.add(slot)
        res.links.append(RadiusLink(slot[0], slot[1], cust["username"], MATCH_NAME,
                                    cust.get("account_id")))
        res.by_name += 1

    return res
