from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from wisp.config import CONFIG
from wisp.central import auth, inventory, pki, releasesync
from wisp.central.store import CentralStore

# The SNMP walk dialog was deleted from the dashboard, so THIS is the supported
# way to run a vendor-onboarding walk. Queue and result are two commands because
# they are two round trips: the edge never accepts an inbound connection, so a
# walk rides the next full /report reply and the dump comes back on the one
# after it.
_WALK_DELIVERY = (
    "The edge never accepts inbound connections, so this is not instant: the walk\n"
    "rides the NEXT FULL /report reply and the dump comes back on a later one.\n"
    "Allow roughly one report cycle (a recheck report carries no walks), then:")

_TRUNCATED_WARNING = (
    "*** TRUNCATED — this dump STOPS at the varbind cap and is NOT the whole\n"
    "*** subtree. An OID missing from a truncated walk is NOT absent from the\n"
    "*** device: reading it that way is the false negative this flag exists to\n"
    "*** prevent. Re-run with a NARROWER --root-oid; a bigger cap is not the fix.")

def _password(args) -> str:
    return args.password or getpass.getpass("password: ")

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="WISP central provisioning")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create-superadmin", help="create a cross-org admin account")
    p.add_argument("--username", required=True)
    p.add_argument("--password")

    p = sub.add_parser("create-user", help="create an org-scoped account")
    p.add_argument("--org", required=True)
    p.add_argument("--username", required=True)
    p.add_argument("--password")
    p.add_argument("--role", default="worker", choices=auth.ROLES)

    p = sub.add_parser("passwd", help="reset a user's password")
    p.add_argument("--username", required=True)
    p.add_argument("--password")

    p = sub.add_parser("set-org", help="name an org / set its fleet-watchdog ntfy topic")
    p.add_argument("--org", required=True)
    p.add_argument("--name")
    p.add_argument("--topic")

    sub.add_parser("list-users", help="list all accounts")

    p = sub.add_parser("publish-release", help="register a version + its per-platform artifacts")
    p.add_argument("--version", required=True)
    p.add_argument("--channel", default="stable")
    p.add_argument("--artifact", nargs=3, action="append", default=[],
                   metavar=("PLATFORM", "URL", "SHA256"),
                   help="e.g. --artifact linux-amd64 https://.../wisp-edge <sha256> (repeatable)")

    sub.add_parser("list-releases", help="list published releases (what start-rollout can target)")

    sub.add_parser("sync-releases", help="mirror the latest GitHub release (installers + "
                   "agent binaries) into central and publish it (run on a timer so "
                   "central auto-learns each tag; unauthenticated — the repo is public, "
                   "WISP_GITHUB_TOKEN only needed if it ever goes private)")

    p = sub.add_parser("start-rollout", help="begin a staged rollout to an org")
    p.add_argument("--org", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--canary", default="", help="comma-separated node_ids for the first wave")

    p = sub.add_parser("rollout-status", help="show an org's rollout + node versions")
    p.add_argument("--org", required=True)

    p = sub.add_parser("snmp-walk", help="queue a diagnostic SNMP walk against one "
                       "device (vendor onboarding — the dashboard no longer offers "
                       "this; the result arrives one report cycle later)")
    p.add_argument("--device", required=True, type=int, help="org_devices.id")
    p.add_argument("--root-oid", default="1.3.6.1",
                   help="subtree to dump; keep it NARROW (default 1.3.6.1)")
    p.add_argument("--max-varbinds", type=int, default=None,
                   help=f"cap on varbinds returned (server cap "
                        f"{inventory.WALK_CAP_MAX_VARBINDS})")

    p = sub.add_parser("snmp-walk-result", help="print a queued walk's varbind dump")
    p.add_argument("--id", required=True, type=int, help="walk id from snmp-walk")
    p.add_argument("--out", default=None,
                   help="write the varbinds to this file instead of stdout "
                        "(these run to hundreds of KB)")

    p = sub.add_parser("init-ca", help="create (or reuse) the internal mTLS CA + "
                       "central's own server cert, replacing the bearer-token stopgap")
    p.add_argument("--pki-dir", default=str(CONFIG.central_pki_dir))
    p.add_argument("--host", action="append", default=[],
                   help="hostname/IP central is reachable at (repeatable — becomes the "
                        "server cert's SAN so edges can verify it without disabling "
                        "hostname checking); e.g. --host central.example.net --host 10.0.0.5")

    p = sub.add_parser("enroll-edge", help="issue an mTLS client cert for one edge")
    p.add_argument("--org", required=True)
    p.add_argument("--node", required=True)
    p.add_argument("--pki-dir", default=str(CONFIG.central_pki_dir))
    p.add_argument("--out", default=None,
                   help="directory to write <node>.key/<node>.crt to (default: --pki-dir)")

    args = ap.parse_args(argv)
    if args.cmd in ("init-ca", "enroll-edge"):
        store = None
    elif args.cmd == "sync-releases":
        try:
            store = CentralStore(CONFIG.central_db, migrate=False)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    else:
        store = CentralStore(CONFIG.central_db)

    try:
        if args.cmd == "create-superadmin":
            uid = auth.create_user(store, None, args.username, _password(args))
            print(f"created superadmin {args.username!r} (id={uid})")
        elif args.cmd == "create-user":
            uid = auth.create_user(store, args.org, args.username, _password(args), args.role)
            print(f"created {args.role} {args.username!r} for org {args.org!r} (id={uid})")
        elif args.cmd == "passwd":
            user = store.get_user_by_username(args.username)
            if not user:
                print(f"no such user {args.username!r}", file=sys.stderr)
                return 1
            auth.set_password(store, user["id"], _password(args))
            print(f"password updated for {args.username!r}")
        elif args.cmd == "set-org":
            store.set_org(args.org, name=args.name, ntfy_topic=args.topic)
            print(f"org {args.org!r} updated")
        elif args.cmd == "list-users":
            for u in store.list_users():
                scope = "SUPERADMIN" if u["org_id"] is None else f"{u['org_id']}/{u['role']}"
                active = "" if u["is_active"] else " (inactive)"
                print(f"  {u['id']:>3}  {u['username']:<20} {scope}{active}")
        elif args.cmd == "publish-release":
            artifacts = {plat: {"url": url, "sha256": sha} for plat, url, sha in args.artifact}
            store.set_release(args.version, artifacts, args.channel)
            print(f"published {args.version} ({args.channel}) with "
                  f"{len(artifacts)} artifact(s): {', '.join(artifacts) or '(none)'}")
        elif args.cmd == "list-releases":
            for r in store.list_releases():
                rel = store.get_release(r["version"]) or {"artifacts": {}}
                plats = ", ".join(sorted(rel["artifacts"])) or "(no artifacts)"
                print(f"  {r['version']:<16} {r['channel']:<8} {r['created_at']}  {plats}")
        elif args.cmd == "sync-releases":
            from wisp.egress.notifiers import build_notifier
            try:
                version, n = releasesync.sync_and_record(store, build_notifier(CONFIG))
            except releasesync.ReleaseSyncError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            print(f"synced release {version} with {n} artifact(s); "
                  f"latest is now {store.list_releases()[0]['version']}")
        elif args.cmd == "start-rollout":
            if not store.get_release(args.version):
                print(f"no such release {args.version!r} — publish it first", file=sys.stderr)
                return 1
            canary = [c.strip() for c in args.canary.split(",") if c.strip()]
            store.set_rollout(args.org, args.version, canary, state="canary")
            wave = f"canary {canary}" if canary else "fleet-wide (no canary)"
            print(f"rollout of {args.version} to {args.org!r} started: {wave}")
        elif args.cmd == "rollout-status":
            r = store.get_rollout(args.org)
            if not r:
                print(f"no rollout for {args.org!r}")
            else:
                print(f"rollout -> {r['target_version']}  state={r['state']}  "
                      f"canary={r['canary']}")
            for n in store.node_versions(args.org):
                print(f"  {n['node_id']:<16} version={n['version'] or '?':<10} "
                      f"last_seen={n['last_seen']}")
        elif args.cmd == "snmp-walk":
            # Same gates the deleted dashboard handler applied, in the same
            # order, and through the same store method — so the newest-N
            # retention and the supersede-the-pending-walk behaviour still hold.
            org = store.device_org(args.device)
            device = store.get_org_device(org, args.device) if org else None
            if not device:
                print(f"no device with id={args.device}", file=sys.stderr)
                return 1
            if not device.get("snmp_enabled") or not device.get("snmp_community"):
                print(f"{device['name']!r} has no SNMP community — enable SNMP on "
                      "the device first", file=sys.stderr)
                return 1
            node = device.get("assigned_node_id")
            if not node:
                print(f"{device['name']!r} is not assigned to a probe — the walk "
                      "runs from its assigned node", file=sys.stderr)
                return 1
            clean = inventory.clean_walk_payload(
                {"root_oid": args.root_oid, "max_varbinds": args.max_varbinds})
            wid = store.create_snmp_walk(org, args.device, node,
                                         clean["root_oid"], clean["max_varbinds"],
                                         requested_by="admin-cli")
            print(f"queued walk id={wid} on {org}/{device['name']} "
                  f"({device['ip_address']}) via probe {node!r}")
            print(f"  root OID {clean['root_oid']}, max {clean['max_varbinds']} "
                  f"varbinds")
            print(_WALK_DELIVERY)
            print(f"  PYTHONPATH=src python -m wisp.central.admin "
                  f"snmp-walk-result --id {wid}")
        elif args.cmd == "snmp-walk-result":
            org = store.snmp_walk_org(args.id)
            walk = store.get_snmp_walk(org, args.id) if org else None
            if not walk:
                print(f"no walk with id={args.id}", file=sys.stderr)
                return 1
            print(f"walk {walk['id']}  org={org}  device_id={walk['device_id']}  "
                  f"probe={walk['node_id']}")
            print(f"  root OID {walk['root_oid']}  status={walk['status']}  "
                  f"queued {walk['created_at']}"
                  + (f"  completed {walk['completed_at']}"
                     if walk['completed_at'] else ""))
            if walk["status"] == "pending":
                print("  still queued — it is delivered in the next full /report "
                      "reply, then the edge posts the dump back")
                return 0
            if walk["error"]:
                print(f"  error: {walk['error']}", file=sys.stderr)
                return 1
            rows = walk["result"] or []
            print(f"  {walk['varbind_count']} varbind(s)")
            # Announced BEFORE the dump and repeated after it: a dump this long
            # is read from its tail as often as its head, and a partial one that
            # looks complete is the costly failure.
            if walk["truncated"]:
                print(_TRUNCATED_WARNING)
            if args.out:
                Path(args.out).write_text(
                    "".join(f"{oid}\t{value}\n" for oid, value in rows),
                    encoding="utf-8")
                print(f"  wrote {len(rows)} varbind(s) to {args.out}")
            else:
                for oid, value in rows:
                    print(f"  {oid}\t{value}")
            if walk["truncated"]:
                print(_TRUNCATED_WARNING)
        elif args.cmd == "init-ca":
            pki_dir = Path(args.pki_dir)
            ca_key, ca_cert = pki.ensure_ca(pki_dir)
            server_key, server_cert = pki_dir / "central.key", pki_dir / "central.crt"
            san = [f"IP:{h}" if h.replace(".", "").isdigit() else f"DNS:{h}" for h in args.host]
            pki.issue_cert(pki_dir, "central", server_key, server_cert,
                           san=san or None)
            print(f"CA ready at {ca_cert} (keep {ca_key} secret — it can mint new edge certs)")
            print(f"central server cert: {server_cert} / {server_key}")
            print("point central at them:")
            print(f"  WISP_CENTRAL_TLS_CERT={server_cert} WISP_CENTRAL_TLS_KEY={server_key} "
                  f"WISP_CENTRAL_CLIENT_CA={ca_cert}")
            if not args.host:
                print("no --host given — the server cert has no SAN; edges will need "
                      "WISP_CENTRAL_CA_CERT set without hostname verification, or re-run "
                      "with --host once you know central's address", file=sys.stderr)
        elif args.cmd == "enroll-edge":
            pki_dir = Path(args.pki_dir)
            out_dir = Path(args.out) if args.out else pki_dir
            cn = pki.edge_common_name(args.org, args.node)
            key_path = out_dir / f"{args.node}.key"
            cert_path = out_dir / f"{args.node}.crt"
            pki.issue_cert(pki_dir, cn, key_path, cert_path)
            _, ca_cert = pki.ensure_ca(pki_dir)
            print(f"issued edge cert for {args.org}/{args.node}: {cert_path} / {key_path}")
            print(f"copy {cert_path}, {key_path}, and the CA cert ({ca_cert}) to the edge box, then set:")
            print(f"  WISP_CENTRAL_CLIENT_CERT={cert_path} WISP_CENTRAL_CLIENT_KEY={key_path} "
                  f"WISP_CENTRAL_CA_CERT={ca_cert}")
    except auth.AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except inventory.InventoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except pki.PkiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
