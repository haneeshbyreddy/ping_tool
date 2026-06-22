"""Acknowledge an outage from the terminal — the dev stand-in for an in-app
/ack action. Acking records who owns the outage (named in the recurring hourly
all-hands re-page); it does NOT stop the re-pages — only recovery does.

    PYTHONPATH=src python -m wisp.egress.ack                 # list currently-open outages
    PYTHONPATH=src python -m wisp.egress.ack <outage_id> <your name>
"""
import sys

from wisp.database.client import connect
from wisp.egress.notifiers import acknowledge_outage


def _list_open() -> None:
    rows = connect().execute(
        "SELECT o.id, d.name, d.region, o.final_state, o.started_at,"
        " o.acknowledged_by FROM outages o JOIN devices d ON d.id = o.device_id"
        " WHERE o.resolved_at IS NULL ORDER BY o.id"
    ).fetchall()
    if not rows:
        print("no open outages.")
        return
    print("open outages:")
    for r in rows:
        ack = f" [acked by {r['acknowledged_by']}]" if r["acknowledged_by"] else ""
        print(f"  #{r['id']}  {r['name']} ({r['region']})  {r['final_state']}{ack}")


def main() -> None:
    if len(sys.argv) < 3:
        _list_open()
        if len(sys.argv) == 1:
            print("\nto acknowledge:  python ack.py <outage_id> <your name>")
        return
    outage_id = int(sys.argv[1])
    by = " ".join(sys.argv[2:])
    if acknowledge_outage(outage_id, by):
        print(f"outage #{outage_id} acknowledged by {by}. "
              f"You'll be named in the hourly re-page until it recovers.")
    else:
        print(f"outage #{outage_id} not found or already resolved.")


if __name__ == "__main__":
    main()
