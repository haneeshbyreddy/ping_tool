from __future__ import annotations

import logging
import re

from wisp.central import webmacs_profiles as _profiles
from wisp.central.weboptics import (
    TunnelHttp, _column_index, _TableRows, login,
)

log = logging.getLogger("wisp.central.webmacs")

MAX_ROWS = 20000

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")


def normalise_mac(raw: str) -> str | None:

    text = str(raw or "").strip()
    if not _MAC_RE.match(text):
        return None
    return text.replace("-", ":").upper()


class MacTable:

    __slots__ = ("rows", "data_rows", "uplink_rows", "declared_total", "truncated")

    def __init__(self, rows: list[dict], data_rows: int, uplink_rows: int,
                 declared_total: int | None) -> None:
        self.rows = rows
        self.data_rows = data_rows
        self.uplink_rows = uplink_rows
        self.declared_total = declared_total
        self.truncated = (declared_total is not None and data_rows < declared_total)

    @property
    def complete(self) -> bool | None:

        if self.declared_total is None:
            return None
        return not self.truncated

    def shortfall(self) -> int:
        if self.declared_total is None:
            return 0
        return max(0, self.declared_total - self.data_rows)


def parse_mac_table(html: str, profile) -> MacTable:


    parser = _TableRows()
    parser.feed(html)
    index = _column_index(parser.rows, profile)
    if index is None:
        if not profile.column_order:
            return MacTable([], 0, 0, profile.declared_total(html))
        index = {f: i for i, f in enumerate(profile.column_order) if f}
    need = max(index.values()) + 1
    port_at, mac_at = index["port"], index["mac"]
    vlan_at, kind_at = index.get("vlan"), index.get("kind")

    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    data_rows = uplink = 0
    for cells in parser.rows:
        if len(cells) < need:
            continue
        mac = normalise_mac(cells[mac_at])
        if mac is None:
            continue
        data_rows += 1
        if len(out) >= MAX_ROWS:
            continue
        port_label = cells[port_at].strip()
        slot = profile.slot_of(port_label)
        if slot is None:
            uplink += 1
            continue
        key = (slot, mac)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "onu_key": slot,
            "mac": mac,
            "vlan": (cells[vlan_at].strip() or None) if vlan_at is not None else None,
            "kind": (cells[kind_at].strip() or None) if kind_at is not None else None,
            "port_label": port_label,
        })
    return MacTable(out, data_rows, uplink, profile.declared_total(html))


def scrape_macs(http: TunnelHttp, username: str, password: str,
                profile=None) -> tuple[MacTable | None, str | None]:


    prof = profile if profile is not None else _profiles.builtin("dbc")
    err = login(http, username, password, prof)
    if err:
        return None, err

    resp = http.get(prof.mac_path)
    if resp.status == 404:
        return None, (f"this OLT's firmware has no {prof.mac_path} (404). It "
                      "answers on the vendor's web UI but does not carry that "
                      "address table; it needs its own capture and profile")
    if not resp.ok:
        return None, f"could not open the address table: {resp.error or resp.status}"

    html = resp.body.decode(prof.charset, "replace")
    table = parse_mac_table(html, prof)
    if not table.data_rows:
        return table, ("the page carried no address rows this profile could read: "
                       "either the table is genuinely empty, or its columns are "
                       "named differently on this build")
    if table.truncated:
        return table, (
            f"the OLT reports {table.declared_total} addresses in its table but "
            f"only {table.data_rows} were on the page — {table.shortfall()} "
            "missing, so this read is PARTIAL. Some ONUs will show no address "
            "that really have one.")
    return table, None
