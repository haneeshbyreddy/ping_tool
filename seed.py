"""Seed a realistic demo network so the system is runnable end-to-end with no
hardware. The topology mirrors a small rural WISP: a core gateway feeding two
main towers, each with sectors, and one relay reached over a backhaul link.

Run:  python seed.py            # seed only if empty
      python seed.py --reset    # wipe demo data and reseed

The IPs are documentation/TEST-NET-1 (192.0.2.0/24) addresses — never routed —
so a real ICMP prober won't accidentally hit anything. The SimulatedProber
(Phase 2) keys its scripted outages off these.
"""
from __future__ import annotations

import sys

from db import connect, migrate, transaction

# id, name, ip, type, criticality, region, parent_id, power_ref_ip, tech_phone, customers, rev/hr
DEVICES = [
    (1, "Core Gateway",        "192.0.2.1",  "core",     5, "HQ",      None, None,         "+910000000001",   0,    0),
    (2, "Rampur Main Tower",   "192.0.2.10", "tower",    4, "Rampur",  1,    "192.0.2.11", "+910000000002", 140,  350),
    (3, "Rampur Sector A",     "192.0.2.12", "sector",   2, "Rampur",  2,    "192.0.2.11", "+910000000002",  60,  150),
    (4, "Rampur Sector B",     "192.0.2.13", "sector",   2, "Rampur",  2,    "192.0.2.11", "+910000000002",  80,  200),
    (5, "Sohna Relay",         "192.0.2.20", "relay",    4, "Sohna",   2,    "192.0.2.21", "+910000000003", 110,  275),
    (6, "Sohna Sector A",      "192.0.2.22", "sector",   2, "Sohna",   5,    "192.0.2.21", "+910000000003", 110,  275),
    (7, "Bhondsi Tower",       "192.0.2.30", "tower",    4, "Bhondsi", 1,    "192.0.2.31", "+910000000004",  95,  240),
    (8, "Bhondsi AP",          "192.0.2.32", "sector",   3, "Bhondsi", 7,    "192.0.2.31", "+910000000004",  95,  240),
]

# A few customer rows for the reserved (future) customer-comms layer.
CUSTOMERS = [
    ("+919900000001", 3, "Rampur"),
    ("+919900000002", 4, "Rampur"),
    ("+919900000003", 6, "Sohna"),
    ("+919900000004", 8, "Bhondsi"),
]

_SEED_TABLES = ("customer_mappings", "escalations", "alert_log", "outages", "poll_results", "devices")


def _wipe(conn) -> None:
    # Children before parents to satisfy foreign keys. Devices use explicit ids,
    # so there is no autoincrement sequence to reset.
    for table in _SEED_TABLES:
        conn.execute(f"DELETE FROM {table}")


def seed(reset: bool = False) -> None:
    migrate()
    with connect() as conn:
        existing = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        if existing and not reset:
            print(f"devices already present ({existing}); use --reset to reseed. Skipping.")
            return
        with transaction(conn):
            if reset:
                _wipe(conn)
            conn.executemany(
                "INSERT INTO devices (id, name, ip_address, device_type, criticality, region,"
                " parent_device_id, power_ref_ip, technician_phone, customer_count,"
                " base_revenue_impact) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                DEVICES,
            )
            conn.executemany(
                "INSERT INTO customer_mappings (customer_phone, device_id, region)"
                " VALUES (?,?,?)",
                CUSTOMERS,
            )
        total_cust = sum(d[9] for d in DEVICES)
        print(f"seeded {len(DEVICES)} devices, {len(CUSTOMERS)} customer rows.")
        print(f"total customers behind infra: {total_cust}")
        print("topology:")
        for row in conn.execute(
            "SELECT d.id, d.name, d.device_type, d.criticality, d.customer_count, p.name parent"
            " FROM devices d LEFT JOIN devices p ON d.parent_device_id = p.id ORDER BY d.id"
        ):
            parent = f"  ↳ under {row['parent']}" if row["parent"] else "  (root)"
            print(
                f"  [{row['id']}] {row['name']:<20} {row['device_type']:<8}"
                f" crit={row['criticality']} cust={row['customer_count']:<4}{parent}"
            )


if __name__ == "__main__":
    seed(reset="--reset" in sys.argv)
