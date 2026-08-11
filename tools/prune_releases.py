#!/usr/bin/env python3


from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

ROLLBACK_FLOOR = "0.14.0"

NON_VERSION_DIRS = {"app"}


def _vkey(name: str) -> tuple:
    try:
        return tuple(int(p) for p in name.split("."))
    except ValueError:
        return (-1,)


def _dir_bytes(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def plan(releases: Path, db: Path, keep: int) -> tuple[list, list]:
    in_use: set[str] = set()
    if db.exists():
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            in_use = {r[0] for r in conn.execute(
                "SELECT DISTINCT version FROM nodes WHERE version IS NOT NULL") if r[0]}
        finally:
            conn.close()

    dirs = [d for d in releases.iterdir() if d.is_dir()]
    versions = sorted((d for d in dirs if d.name not in NON_VERSION_DIRS),
                      key=lambda d: _vkey(d.name), reverse=True)
    newest = {d.name for d in versions[:keep]}

    keepers, doomed = [], []
    for d in dirs:
        if d.name in NON_VERSION_DIRS:
            keepers.append((d, "APK mirror (fixed path)"))
        elif d.name in in_use:
            keepers.append((d, "a node is running it"))
        elif d.name == ROLLBACK_FLOOR:
            keepers.append((d, "rollback floor"))
        elif d.name in newest:
            keepers.append((d, f"newest {keep}"))
        else:
            doomed.append((d, _dir_bytes(d)))
    return keepers, doomed


def main() -> int:
    ap = argparse.ArgumentParser(description='Bound data/releases/, which nothing else prunes.',
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--releases", default=str(REPO / "data" / "releases"))
    ap.add_argument("--db", default=str(REPO / "data" / "central.db"))
    ap.add_argument("--keep", type=int, default=2,
                    help="newest versions to keep regardless of fleet use (default 2)")
    ap.add_argument("--apply", action="store_true", help="actually delete")
    args = ap.parse_args()

    releases = Path(args.releases)
    if not releases.exists():
        print(f"no releases dir at {releases}")
        return 0

    keepers, doomed = plan(releases, Path(args.db), args.keep)

    for d, why in sorted(keepers, key=lambda k: k[0].name):
        print(f"  KEEP   {d.name:12} {why}")
    if not doomed:
        print("\nnothing to prune.")
        return 0

    freed = sum(b for _, b in doomed)
    for d, b in sorted(doomed, key=lambda k: k[0].name):
        print(f"  DELETE {d.name:12} {b/1e6:.0f} MB")

    if not args.apply:
        print(f"\nwould free {freed/1e6:.0f} MB — re-run with --apply")
        return 0

    for d, _ in doomed:
        shutil.rmtree(d)
    print(f"\nfreed {freed/1e6:.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
