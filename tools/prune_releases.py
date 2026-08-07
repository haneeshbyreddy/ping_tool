#!/usr/bin/env python3
"""Bound `data/releases/`, which nothing else prunes (~174 MB per version, forever).

The rule is derived from LIVE FLEET STATE, not a fixed count. "Keep current + previous"
sounds equivalent and is not: a node that has not updated in a month can be running
something older than "previous", and deleting the build a probe is actually running turns
its next self-update health-check rollback into a 404. So a version is kept when ANY of:

  * a node in `nodes` reports running it            — deleting it breaks that node's rollback
  * it is the rollback floor (v0.14.0)              — CLAUDE.md: no artifact exists below it
  * it is one of the newest --keep versions         — what a fresh install / rollout needs
  * it is not a version at all (`app/`)             — the field-app APK mirror, fixed path

Everything else is a cached copy of a GitHub artifact that can be re-fetched by
`wisp-release-sync`, so deleting it costs a re-download and nothing more.

    .venv/bin/python tools/prune_releases.py            # report only
    .venv/bin/python tools/prune_releases.py --apply
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# CLAUDE.md: "v0.14.0 is the rollback floor — there is no artifact below it, so an edge on
# an older build can only roll forward." Deleting it removes the only floor a bad rollout
# has to land on.
ROLLBACK_FLOOR = "0.14.0"

# Not a version — the store-less APK mirror serves from this fixed path.
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
    ap = argparse.ArgumentParser(description=__doc__,
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
