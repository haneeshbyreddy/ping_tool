"""Acknowledge an outage from the terminal — the dev stand-in for the Telegram
/ack button. Acking stops the escalation ladder (no re-alert, no owner escalation).

    python ack.py                 # list currently-open outages
    python ack.py <outage_id> <your name>
"""
import sys

from db import connect
from notifiers import acknowledge_outage


def _list_open() -> None:
    rows = connect().execute(
        "SELECT o.id, d.name, d.region, o.final_state, o.inferred_cause, o.started_at,"
        " o.acknowledged_by FROM outages o JOIN devices d ON d.id = o.device_id"
        " WHERE o.resolved_at IS NULL ORDER BY o.id"
    ).fetchall()
    if not rows:
        print("no open outages.")
        return
    print("open outages:")
    for r in rows:
        ack = f" [acked by {r['acknowledged_by']}]" if r["acknowledged_by"] else ""
        cause = r["inferred_cause"] or "-"
        print(f"  #{r['id']}  {r['name']} ({r['region']})  {r['final_state']}  {cause}{ack}")


def main() -> None:
    if len(sys.argv) < 3:
        _list_open()
        if len(sys.argv) == 1:
            print("\nto acknowledge:  python ack.py <outage_id> <your name>")
        return
    outage_id = int(sys.argv[1])
    by = " ".join(sys.argv[2:])
    if acknowledge_outage(outage_id, by):
        print(f"outage #{outage_id} acknowledged by {by}. Escalation stopped.")
    else:
        print(f"outage #{outage_id} not found or already resolved.")


if __name__ == "__main__":
    main()
