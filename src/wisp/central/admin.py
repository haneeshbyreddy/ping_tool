"""Central provisioning CLI (Phase 10 Part C).

Central is *central-provisioned*: the platform operator onboards each ISP and seeds accounts
from here, rather than a public signup. Bootstrap the first superadmin with this, then do the
rest (org users, team, attendance) from the console.

    PYTHONPATH=src python -m wisp.central.admin create-superadmin --username you --password ...
    PYTHONPATH=src python -m wisp.central.admin create-user --tenant ispA --username a --password ... --role owner
    PYTHONPATH=src python -m wisp.central.admin set-org   --tenant ispA --name "ISP A" --topic ispA-ops
    PYTHONPATH=src python -m wisp.central.admin passwd     --username a --password ...
    PYTHONPATH=src python -m wisp.central.admin list-users

A password may be passed with --password or, more safely, omitted to be prompted (no shell
history). The central DB is WISP_CENTRAL_DB (env), same as the server.
"""
from __future__ import annotations

import argparse
import getpass
import sys

from wisp.config import CONFIG
from wisp.central import auth
from wisp.central.store import CentralStore


def _password(args) -> str:
    return args.password or getpass.getpass("password: ")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="WISP central provisioning")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create-superadmin", help="create a cross-tenant admin account")
    p.add_argument("--username", required=True)
    p.add_argument("--password")

    p = sub.add_parser("create-user", help="create an org-scoped account")
    p.add_argument("--tenant", required=True)
    p.add_argument("--username", required=True)
    p.add_argument("--password")
    p.add_argument("--role", default="operator", choices=auth.ROLES)

    p = sub.add_parser("passwd", help="reset a user's password")
    p.add_argument("--username", required=True)
    p.add_argument("--password")

    p = sub.add_parser("set-org", help="name an org / set its fleet-watchdog ntfy topic")
    p.add_argument("--tenant", required=True)
    p.add_argument("--name")
    p.add_argument("--topic")

    sub.add_parser("list-users", help="list all accounts")

    p = sub.add_parser("publish-release", help="register a version + its per-platform artifacts")
    p.add_argument("--version", required=True)
    p.add_argument("--channel", default="stable")
    p.add_argument("--artifact", nargs=3, action="append", default=[],
                   metavar=("PLATFORM", "URL", "SHA256"),
                   help="e.g. --artifact linux-amd64 https://.../wisp-edge <sha256> (repeatable)")

    p = sub.add_parser("start-rollout", help="begin a staged rollout to an org")
    p.add_argument("--tenant", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--canary", default="", help="comma-separated node_ids for the first wave")

    p = sub.add_parser("rollout-status", help="show an org's rollout + node versions")
    p.add_argument("--tenant", required=True)

    args = ap.parse_args(argv)
    store = CentralStore(CONFIG.central_db)

    try:
        if args.cmd == "create-superadmin":
            uid = auth.create_user(store, None, args.username, _password(args))
            print(f"created superadmin {args.username!r} (id={uid})")
        elif args.cmd == "create-user":
            uid = auth.create_user(store, args.tenant, args.username, _password(args), args.role)
            print(f"created {args.role} {args.username!r} for org {args.tenant!r} (id={uid})")
        elif args.cmd == "passwd":
            user = store.get_user_by_username(args.username)
            if not user:
                print(f"no such user {args.username!r}", file=sys.stderr)
                return 1
            auth.set_password(store, user["id"], _password(args))
            print(f"password updated for {args.username!r}")
        elif args.cmd == "set-org":
            store.set_org(args.tenant, name=args.name, ntfy_topic=args.topic)
            print(f"org {args.tenant!r} updated")
        elif args.cmd == "list-users":
            for u in store.list_users():
                scope = "SUPERADMIN" if u["tenant_id"] is None else f"{u['tenant_id']}/{u['role']}"
                active = "" if u["is_active"] else " (inactive)"
                print(f"  {u['id']:>3}  {u['username']:<20} {scope}{active}")
        elif args.cmd == "publish-release":
            artifacts = {plat: {"url": url, "sha256": sha} for plat, url, sha in args.artifact}
            store.set_release(args.version, artifacts, args.channel)
            print(f"published {args.version} ({args.channel}) with "
                  f"{len(artifacts)} artifact(s): {', '.join(artifacts) or '(none)'}")
        elif args.cmd == "start-rollout":
            if not store.get_release(args.version):
                print(f"no such release {args.version!r} — publish it first", file=sys.stderr)
                return 1
            canary = [c.strip() for c in args.canary.split(",") if c.strip()]
            store.set_rollout(args.tenant, args.version, canary, state="canary")
            wave = f"canary {canary}" if canary else "fleet-wide (no canary)"
            print(f"rollout of {args.version} to {args.tenant!r} started: {wave}")
        elif args.cmd == "rollout-status":
            r = store.get_rollout(args.tenant)
            if not r:
                print(f"no rollout for {args.tenant!r}")
            else:
                print(f"rollout -> {r['target_version']}  state={r['state']}  "
                      f"canary={r['canary']}")
            for n in store.node_versions(args.tenant):
                print(f"  {n['node_id']:<16} version={n['version'] or '?':<10} "
                      f"last_seen={n['last_seen']}")
    except auth.AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
