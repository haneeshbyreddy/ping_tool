#!/usr/bin/env python3


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

_SECRETS = [
    ("data/secret.key", "device web-UI credential vault key"),
    ("data/central_session_secret", "dashboard session signing key"),
    ("deploy/central.env", "server config + WhatsApp/GitHub tokens"),
]

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
    conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30.0)
    try:
        conn.execute("VACUUM INTO ?", (str(dest),))
    finally:
        conn.close()

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
                counts[t] = None
        user_version = check.execute("PRAGMA user_version").fetchone()[0]
    finally:
        check.close()
    return {"integrity": "ok", "row_counts": counts, "user_version": user_version}


def _dump_precious(db: Path, dest: Path) -> int:
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


def _dirty_paths() -> list[str]:
    rels = set()
    for cmd in (("git", "diff", "HEAD", "--name-only", "-z"),
                ("git", "ls-files", "-o", "--exclude-standard", "-z")):
        rels.update(p for p in _run(*cmd).split("\0") if p)
    return sorted(rels)


def _dirty_source(dest: Path) -> dict:
    kept: list[str] = []
    vanished: list[str] = []
    total = 0
    with tarfile.open(dest, "w:gz", compresslevel=9) as tar:
        for rel in _dirty_paths():
            src = REPO / rel
            try:
                if not (src.is_symlink() or src.is_file()):
                    vanished.append(rel)
                    continue
                size = 0 if src.is_symlink() else src.stat().st_size
                tar.add(src, arcname=rel)
            except OSError:
                vanished.append(rel)
                continue
            kept.append(rel)
            total += size

    if not kept:
        dest.unlink()
        return {"present": False, "files": 0, "vanished": vanished}
    return {"present": True, "files": len(kept), "paths": kept,
            "vanished": vanished, "bytes_raw": total,
            "bytes_gz": dest.stat().st_size, "sha256": _sha256(dest)}


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

        status = _run("git", "status", "--porcelain")
        dirty_state = (_dirty_source(tmp / "dirty-source.tar.gz") if status
                       else {"present": False, "files": 0})

        manifest = {
            "taken_at": datetime.now(timezone.utc).isoformat(),
            "host": os.uname().nodename,
            "source_db": str(db_path),
            "db_bytes_raw": raw.stat().st_size,
            "db_bytes_gz": gz.stat().st_size,
            "db_sha256": _sha256(raw),
            "precious_rows": rows,
            "git_commit": _run("git", "rev-parse", "HEAD"),
            "git_dirty": bool(status),
            "git_status": status,
            "dirty_source": dirty_state,
            "secrets": secret_state,
            **meta,
        }
        (tmp / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
        raw.unlink()

        with tarfile.open(bundle, "w:gz") as tar:
            for item in sorted(tmp.iterdir()):
                tar.add(item, arcname=item.name)

    os.chmod(bundle, 0o600)
    _prune(out_dir, keep)
    return bundle


def _prune(out_dir: Path, keep: int) -> list[Path]:
    bundles = sorted(out_dir.glob("wisp-backup-*.tar.gz"))
    doomed = bundles[:-keep] if keep > 0 and len(bundles) > keep else []
    for b in doomed:
        b.unlink()
    return doomed


def verify(bundle: Path) -> dict:

    with tempfile.TemporaryDirectory(prefix="wisp-verify-") as tmpd:
        tmp = Path(tmpd)
        with tarfile.open(bundle, "r:gz") as tar:
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

        dirty = man.get("dirty_source") or {"present": False, "files": 0}
        if dirty.get("present"):
            src = tmp / "dirty-source.tar.gz"
            if not src.exists():
                raise RuntimeError("manifest claims dirty source, bundle has no tar")
            if _sha256(src) != dirty["sha256"]:
                raise RuntimeError("dirty-source.tar.gz sha256 mismatch — bundle is corrupt")
            with tarfile.open(src, "r:gz") as srctar:
                if len(srctar.getnames()) != dirty["files"]:
                    raise RuntimeError("dirty-source.tar.gz is short of its manifest")

        missing = [s["path"] for s in man["secrets"] if not s["present"]]
        return {"bundle": bundle.name, "integrity": "ok", "rows": live,
                "missing_secrets": missing, "taken_at": man["taken_at"],
                "dirty_files": dirty.get("files", 0),
                "git_commit": man.get("git_commit", "")}


def main() -> int:
    ap = argparse.ArgumentParser(description='Consistent, restorable snapshots of everything that only exists on this disk.',
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
        if r["dirty_files"]:
            print(f"    dirty source: {r['dirty_files']} files on top of "
                  f"{r['git_commit'][:12] or 'an unknown commit'}")
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
