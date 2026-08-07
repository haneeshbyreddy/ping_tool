#!/usr/bin/env python3
"""Consistent, restorable snapshots of everything that only exists on this disk.

Run it any time — the server keeps serving. `VACUUM INTO` is SQLite's own hot-backup:
it takes a read lock, writes a fresh defragmented copy, and cannot observe a half-applied
transaction. Copying `central.db` with `cp` while central is running does NOT give you
that (the WAL holds committed pages the main file doesn't have yet), which is the usual
way a "backup" turns out to be unrestorable on the day it's needed.

WHAT GOES IN THE BUNDLE, and why each part is there:

  central.db.gz      the whole database, vacuumed and verified.
  secrets/           `data/secret.key`, `data/central_session_secret`, `deploy/central.env`.
                     THESE ARE THE POINT. They are git-ignored, 2.5 KB together, and exist
                     on exactly one disk. `secret.key` decrypts the stored device web-UI
                     passwords: restore the DB without it and the credential vault is
                     permanently unreadable — the rows survive and decode to nothing.
  precious.sql       a plain-text dump of the config/customer tables ONLY (see _PRECIOUS).
                     Redundant with central.db.gz on purpose: it is ~150 KB, it is readable
                     with `less`, and it restores into a *newer* schema that a binary DB
                     from an older build might not survive. It is the copy you can still
                     use in two years.
  MANIFEST.json      sizes, row counts, sha256 of the DB, the git commit, schema version.
                     A backup you cannot identify is a backup you will not trust enough to
                     restore from.

Usage:
    .venv/bin/python tools/backup.py                 # snapshot + prune to --keep
    .venv/bin/python tools/backup.py --verify LATEST # prove the newest bundle restores
    .venv/bin/python tools/backup.py --list
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Files that exist on this disk and nowhere else. Missing ones are recorded in the
# manifest rather than skipped silently — "the backup ran fine" must never be how you
# find out a secret was not in it.
_SECRETS = [
    ("data/secret.key", "device web-UI credential vault key"),
    ("data/central_session_secret", "dashboard session signing key"),
    ("deploy/central.env", "server config + WhatsApp/GitHub tokens"),
]

# The tables the operator cannot re-derive from anything. Ping history, rollups, alert
# logs and proxy audit are deliberately NOT here: they are 97% of the bytes and they
# regenerate themselves within a poll cycle.
_PRECIOUS = [
    "orgs", "users", "app_settings",
    "org_devices", "org_device_links", "org_device_workers", "org_colors",
    "onu_places", "onu_drops", "link_routes",
    "nodes", "node_tokens",
    "snmp_profiles", "gpon_profiles", "web_optics_profiles",
    "org_billing_months", "switch_ports",
]


def _run(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except Exception:
        return ""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot_db(src: Path, dest: Path) -> dict:
    """Hot, consistent copy + integrity check. Raises if the copy is not sound."""
    conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30.0)
    try:
        # VACUUM INTO refuses to overwrite, so hand it a path that does not exist.
        conn.execute("VACUUM INTO ?", (str(dest),))
    finally:
        conn.close()

    # Verify the COPY, not the original: a snapshot nobody checked is a coin flip.
    check = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    try:
        ok = check.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"snapshot failed integrity_check: {ok}")
        counts = {}
        for t in _PRECIOUS:
            try:
                counts[t] = check.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                counts[t] = None  # table not in this schema version yet
        user_version = check.execute("PRAGMA user_version").fetchone()[0]
    finally:
        check.close()
    return {"integrity": "ok", "row_counts": counts, "user_version": user_version}


def _dump_precious(db: Path, dest: Path) -> int:
    """Plain-text dump of the config/customer tables, restorable into a newer schema."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.text_factory = str
    written = 0
    try:
        present = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        with dest.open("w", encoding="utf-8") as fh:
            fh.write("-- WISP central: config + customer tables only.\n")
            fh.write(f"-- taken {datetime.now(timezone.utc).isoformat()}\n")
            fh.write("-- restore:  sqlite3 new.db < precious.sql\n")
            fh.write("PRAGMA foreign_keys=OFF;\nBEGIN;\n")
            for table in _PRECIOUS:
                if table not in present:
                    fh.write(f"-- (absent in this schema: {table})\n")
                    continue
                ddl = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,)).fetchone()
                if ddl and ddl[0]:
                    fh.write(f"{ddl[0]};\n")
                cur = conn.execute(f"SELECT * FROM {table}")
                cols = [d[0] for d in cur.description]
                collist = ", ".join(f'"{c}"' for c in cols)
                for row in cur:
                    vals = ", ".join(_sqlval(v) for v in row)
                    fh.write(f'INSERT INTO "{table}" ({collist}) VALUES ({vals});\n')
                    written += 1
            fh.write("COMMIT;\n")
    finally:
        conn.close()
    return written


def _sqlval(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, bytes):
        return "X'" + v.hex() + "'"
    return "'" + str(v).replace("'", "''") + "'"


def backup(out_dir: Path, db_path: Path, keep: int) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    bundle = out_dir / f"wisp-backup-{stamp}.tar.gz"

    with tempfile.TemporaryDirectory(prefix="wisp-backup-") as tmpd:
        tmp = Path(tmpd)
        raw = tmp / "central.db"
        meta = _snapshot_db(db_path, raw)

        gz = tmp / "central.db.gz"
        with raw.open("rb") as fi, gzip.open(gz, "wb", compresslevel=9) as fo:
            shutil.copyfileobj(fi, fo, length=1 << 20)

        sql = tmp / "precious.sql"
        rows = _dump_precious(raw, sql)

        secrets_dir = tmp / "secrets"
        secrets_dir.mkdir()
        secret_state = []
        for rel, why in _SECRETS:
            src = REPO / rel
            entry = {"path": rel, "purpose": why, "present": src.exists()}
            if src.exists():
                dst = secrets_dir / Path(rel).name
                shutil.copy2(src, dst)
                os.chmod(dst, 0o600)
                entry["bytes"] = src.stat().st_size
                entry["sha256"] = _sha256(src)
            secret_state.append(entry)

        manifest = {
            "taken_at": datetime.now(timezone.utc).isoformat(),
            "host": os.uname().nodename,
            "source_db": str(db_path),
            "db_bytes_raw": raw.stat().st_size,
            "db_bytes_gz": gz.stat().st_size,
            "db_sha256": _sha256(raw),
            "precious_rows": rows,
            "git_commit": _run("git", "rev-parse", "HEAD"),
            "git_dirty": bool(_run("git", "status", "--porcelain")),
            "secrets": secret_state,
            **meta,
        }
        (tmp / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
        raw.unlink()  # ship only the compressed copy

        with tarfile.open(bundle, "w:gz") as tar:
            for item in sorted(tmp.iterdir()):
                tar.add(item, arcname=item.name)

    os.chmod(bundle, 0o600)  # it contains secrets and subscriber PII
    _prune(out_dir, keep)
    return bundle


def _prune(out_dir: Path, keep: int) -> list[Path]:
    bundles = sorted(out_dir.glob("wisp-backup-*.tar.gz"))
    doomed = bundles[:-keep] if keep > 0 and len(bundles) > keep else []
    for b in doomed:
        b.unlink()
    return doomed


def verify(bundle: Path) -> dict:
    """Prove a bundle restores: unpack it, open the DB, integrity-check, count rows.

    A backup is a claim until something has actually read it back. This is the only
    part of the system that turns the claim into a fact, so run it after any schema
    change and don't let it become the step nobody runs.
    """
    with tempfile.TemporaryDirectory(prefix="wisp-verify-") as tmpd:
        tmp = Path(tmpd)
        with tarfile.open(bundle, "r:gz") as tar:
            # `data` filter: Python 3.14 rejects unfiltered extraction outright, and we
            # are unpacking an archive to verify it, not to trust it.
            tar.extractall(tmp, filter="data")
        man = json.loads((tmp / "MANIFEST.json").read_text())

        db = tmp / "central.db"
        with gzip.open(tmp / "central.db.gz", "rb") as fi, db.open("wb") as fo:
            shutil.copyfileobj(fi, fo, length=1 << 20)

        if _sha256(db) != man["db_sha256"]:
            raise RuntimeError("sha256 mismatch — bundle is corrupt")

        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if ok != "ok":
                raise RuntimeError(f"integrity_check: {ok}")
            live = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in _PRECIOUS
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                                    " AND name=?", (t,)).fetchone()}
        finally:
            conn.close()

        missing = [s["path"] for s in man["secrets"] if not s["present"]]
        return {"bundle": bundle.name, "integrity": "ok", "rows": live,
                "missing_secrets": missing, "taken_at": man["taken_at"]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(REPO / "data" / "central.db"))
    ap.add_argument("--out", default=str(REPO / "data" / "backups"))
    ap.add_argument("--keep", type=int, default=14,
                    help="daily bundles to retain (default 14)")
    ap.add_argument("--verify", metavar="BUNDLE|LATEST", nargs="?", const="LATEST")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    out = Path(args.out)

    if args.list:
        bundles = sorted(out.glob("wisp-backup-*.tar.gz"))
        if not bundles:
            print(f"no bundles in {out}")
            return 1
        for b in bundles:
            age = (time.time() - b.stat().st_mtime) / 3600
            print(f"{b.name}  {b.stat().st_size/1e6:6.2f} MB  {age:6.1f}h old")
        return 0

    if args.verify:
        target = (sorted(out.glob("wisp-backup-*.tar.gz"))[-1]
                  if args.verify == "LATEST" else Path(args.verify))
        r = verify(target)
        print(f"OK  {r['bundle']}  taken {r['taken_at']}")
        print(f"    integrity_check=ok, sha256 matches")
        for t, n in sorted(r["rows"].items()):
            if n:
                print(f"    {t:24} {n:>7,}")
        if r["missing_secrets"]:
            print(f"    WARNING missing secrets: {', '.join(r['missing_secrets'])}")
        return 0

    t0 = time.time()
    bundle = backup(out, Path(args.db), args.keep)
    size = bundle.stat().st_size
    print(f"backup ok: {bundle.name}  {size/1e6:.2f} MB  in {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
