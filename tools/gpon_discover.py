#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import defaultdict

DBM_LO, DBM_HI = -40.0, -1.0
SCALES = (0.01, 0.1, 0.001, 1.0)
_SERIAL_RE = re.compile(r"^(?:[0-9A-Fa-f]{12,16}|[0-9A-Za-z]{4}[0-9A-Fa-f]{8})$")

def _as_dbm(raw: str) -> float | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        if "." in s:
            v = float(s)
            return v if DBM_LO <= v <= DBM_HI else None
    except ValueError:
        return None
    try:
        i = int(s)
    except ValueError:
        return None
    for sc in SCALES:
        v = i * sc
        if DBM_LO <= v <= DBM_HI:
            return round(v, 2)
    return None

def _best_scale(ints: list[int]) -> float | None:
    for sc in SCALES:
        if all(DBM_LO <= i * sc <= DBM_HI for i in ints):
            return sc
    return None

def _split_col(oid: str) -> tuple[str, str]:
    return oid.rsplit(".", 1) if "." in oid else (oid, "")

def parse_snmpwalk_file(path: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if " = " not in line:
                continue
            oid, rhs = line.split(" = ", 1)
            oid = oid.strip().lstrip(".")
            rhs = rhs.strip()
            val = rhs.split(": ", 1)[1] if ": " in rhs else rhs
            out.append((oid, val.strip().strip('"')))
    return out

async def walk(ip: str, community: str, root: str, timeout: float) -> list[tuple[str, str]]:
    from pysnmp.hlapi.asyncio import (
        SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
        ObjectType, ObjectIdentity, bulk_walk_cmd,
    )
    engine = SnmpEngine()
    transport = await UdpTransportTarget.create((ip, 161), timeout=timeout, retries=1)
    out: list[tuple[str, str]] = []
    async for errInd, errStat, errIdx, binds in bulk_walk_cmd(
        engine, CommunityData(community, mpModel=1), transport, ContextData(),
        0, 40, ObjectType(ObjectIdentity(root)), lexicographicMode=False,
    ):
        if errInd or errStat:
            raise RuntimeError(f"walk failed: {errInd or errStat}")
        for name, val in binds:
            out.append((str(name), val.prettyPrint()))
    engine.close_dispatcher()
    return out

def analyze(varbinds: list[tuple[str, str]]) -> None:
    cols: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for oid, val in varbinds:
        col, idx = _split_col(oid)
        cols[col].append((idx, val))

    optical, state, serial, name = [], [], [], []
    for col, cells in cols.items():
        vals = [v for _, v in cells]
        dbm_hits = [_as_dbm(v) for v in vals]
        n_dbm = sum(1 for d in dbm_hits if d is not None)
        if n_dbm >= max(2, len(vals) // 2):
            ints = []
            for v in vals:
                try:
                    ints.append(int(v.strip()))
                except (ValueError, AttributeError):
                    ints = []
                    break
            optical.append((col, len(cells), _best_scale(ints) if ints else "string", cells[:4]))
            continue
        smallints = [v for v in vals if v.strip().lstrip("-").isdigit() and 0 <= int(v) <= 6]
        if len(smallints) >= max(2, len(vals) // 2) and len(set(vals)) <= 8:
            state.append((col, len(cells), cells[:4]))
        if sum(1 for v in vals if _SERIAL_RE.match(v.strip())) >= max(2, len(vals) // 2):
            serial.append((col, len(cells), cells[:4]))
        if sum(1 for v in vals if re.search(r"[A-Za-z]", v) and " " not in v.strip()
               and len(v.strip()) >= 3) >= max(2, len(vals) // 2) and not _SERIAL_RE.match(vals[0].strip()):
            name.append((col, len(cells), cells[:4]))

    def dump(title: str, rows, scale_col: bool = False) -> None:
        print(f"\n=== {title} ({len(rows)} candidate column(s)) ===")
        for row in sorted(rows, key=lambda r: -r[1])[:12]:
            col, count = row[0], row[1]
            extra = f"  scale={row[2]}" if scale_col else ""
            samples = row[-1]
            print(f"  {col}   rows={count}{extra}")
            for idx, v in samples:
                print(f"      .{idx} -> {v!r}")

    print(f"\nWalked {len(varbinds)} varbinds across {len(cols)} columns.")
    dump("OPTICAL / Rx-power candidates (-> oid_rx / oid_tx / olt_rx)", optical, scale_col=True)
    dump("STATE candidates (-> oid_state)", state)
    dump("SERIAL candidates (-> oid_serial, the onu_key)", serial)
    dump("NAME/description candidates (-> oid_name)", name)
    if not optical:
        print("\n!! No optical column found. This OLT may not expose per-ONU Rx power over")
        print("   SNMP (many budget OLTs are CLI/telnet-only). Confirm on the box's CLI.")
    print("\nNext: paste the OPTICAL row(s) + a matching STATE/SERIAL row and I'll write")
    print("the GponProfile. The Rx column with rows == your ONU count is the one.")

def main() -> int:
    ap = argparse.ArgumentParser(description="Discover a GPON OLT's per-ONU optical OIDs.")
    ap.add_argument("ip", nargs="?", help="OLT IP (omit when using --file)")
    ap.add_argument("community", nargs="?", help="SNMP community (omit when using --file)")
    ap.add_argument("--file", help="analyze a saved `snmpwalk` dump instead of walking live")
    ap.add_argument("--root", default="1.3.6.1.4.1",
                    help="subtree to walk (default: all enterprises)")
    ap.add_argument("--timeout", type=float, default=5.0)
    args = ap.parse_args()
    if args.file:
        vbs = parse_snmpwalk_file(args.file)
    else:
        if not (args.ip and args.community):
            print("give <ip> <community> for a live walk, or --file <dump>", file=sys.stderr)
            return 2
        try:
            import pysnmp
        except ImportError:
            print("live walk needs pysnmp (run under an edge venv), or use snmpwalk + --file",
                  file=sys.stderr)
            return 2
        try:
            vbs = asyncio.run(walk(args.ip, args.community, args.root, args.timeout))
        except Exception as exc:
            print(f"walk of {args.ip} failed: {exc}", file=sys.stderr)
            return 1
    if not vbs:
        print(f"no varbinds under {args.root} — wrong community, or SNMP is off/filtered.",
              file=sys.stderr)
        return 1
    analyze(vbs)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
