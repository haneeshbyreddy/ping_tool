"""PROMOTE duplicated vendor recipes to ONE global row, drop the copies.

The six recipe tables are vendor knowledge as DATA, and the schema already says
so: `org_id NULL` = global, served to every org. But the SPA always posted the
device's org, so the same recipe was re-entered once per ISP — three orgs carry
byte-identical C-Data health maps, two carry identical TP-Link ones. Vendor
knowledge does not belong to an ISP; the duplicates are an artefact of the
write path, not a per-org decision.

    snmp_profiles         vendor health metric maps
    gpon_profiles         ONU optics / roster recipes
    web_optics_profiles   per-ONU Rx off the OLT's own web UI
    web_mac_profiles      the subscriber-MAC page recipe
    radius_profiles       the billing panel's export recipe
    nvr_profiles          camera-state recipes

RECIPES ONLY — the hard boundary. This tool must never reach an ACCOUNT or a
CREDENTIAL: `radius_accounts` (it carries `base_url` and `password_enc`),
`device_webui_credentials`, `org_devices.snmp_community`, `node_tokens`,
`field_tokens`, `users.totp_secret`. Those are per-org by construction and
making one global would be a cross-tenant breach. CLAUDE.md states the rule the
other way round: a profile may never carry a host, the ACCOUNT does. So the
table list is closed (`_FORBIDDEN_TABLES` is asserted at import), and on top of
that every row is run past a host/secret TRIPWIRE before it can be widened —
a hit refuses the group. The tripwire refuses; it never certifies. A miss is
"nothing credential-shaped was found", not "proven safe".

WHAT IT DOES

    Groups rows by the thing that SELECTS them, then collapses a group whose
    rows are the same recipe: one survivor moves to org_id NULL, the redundant
    copies are deleted. Nothing is recreated, so the survivor keeps its id.

WHAT DECIDES A GROUP

    The sysObjectID prefix where the table HAS one and the row fills it in;
    otherwise the NAME. A blank prefix matches NOTHING on the edge
    (`ingress/health.match_profile` and `ingress/gpon.match_profile` both skip
    a blank prefix), so it can never be what makes two rows compete — which is
    why gpon_profiles' two blank-prefix rows are two groups and not a duplicate
    set. The web/RADIUS/NVR recipes carry no prefix at all and are bound by
    name, which is also what their UNIQUE(org, name) index says.

WHAT DECIDES "THE SAME RECIPE"

    The SUBSTANCE: the mapped OIDs / paths / heading columns / decode rules
    (canonical JSON of the whole body), the match prefix, and `enabled` — a
    disabled row is a tombstone that switches the vendor OFF for that org, so
    folding it into an enabled global would switch it back on. The display
    NAME is NOT substance for snmp_profiles (the edge matches on the prefix and
    only echoes the name back as `profile_name`) but IS identity everywhere
    else, where the name is the token a device or an account is bound to.

    A group whose rows disagree is REFUSED and printed for a human. Merging
    two recipes that differ is exactly the confident wrong answer this codebase
    refuses; a wrong OID map reads as a fabricated sensor value.

WHICH ROW SURVIVES

    The OLDEST row in the group, by (created_at, id) — the one that was
    entered first and got copied. An existing global always survives (a global
    is never deleted to mint another one). `--keep <id>` overrides it, because
    when the names differ the label everyone ends up seeing is a human's call,
    not a rule's; the tool never rewrites a name.

WHAT IT WILL NOT DO

    Promote a SINGLETON — one org's row with no duplicate. Duplicates are
    self-evidencing: N ISPs independently entered the same recipe, so it is
    vendor truth. One org having entered a recipe is not evidence it is right
    for another org's box, and a global row is served to every edge. That is a
    judgment call, so it is opt-in: `--promote-singletons`.

Idempotent: after a run the group holds one global and no org rows, which is
the "nothing to do" state. Re-running changes nothing. It also cleans up the
next copy the SPA makes of an already-global recipe. Tables that are empty (or
that this DB predates) are still walked and reported, so the tool keeps working
as rows appear.

    PYTHONPATH=src .venv/bin/python tools/promote_global_profiles.py    # dry run
    PYTHONPATH=src .venv/bin/python tools/promote_global_profiles.py --apply
    PYTHONPATH=src .venv/bin/python tools/promote_global_profiles.py \
        --table snmp_profiles --keep 3 --apply

The dry run opens the DB READ-ONLY (sqlite URI mode=ro) and no run ever opens
it through CentralStore, so this script cannot migrate the live schema on the
way past. BACK UP FIRST and RESTART CENTRAL in the same breath as --apply: the
running process serves `/edge/devices` from these rows and holds its own
connection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wisp.config import CONFIG, Config  # noqa: E402


@dataclass(frozen=True)
class ProfileTable:
    """One recipe table and the rules that make its rows comparable."""
    table: str
    what: str                 # what the recipe is, in the operator's words
    name_is_identity: bool    # is the name a selector, or just a label?
    name_note: str


TABLES: tuple[ProfileTable, ...] = (
    ProfileTable(
        table="snmp_profiles", what="health metric map",
        name_is_identity=False,
        name_note="the name is a LABEL here: the edge matches on the "
                  "sysObjectID prefix and only echoes the name back as "
                  "`profile_name`.",
    ),
    ProfileTable(
        table="gpon_profiles", what="ONU optics recipe",
        name_is_identity=True,
        name_note="the name IS identity here: it is the token "
                  "`org_devices.gpon_vendor` is validated against, so two "
                  "names are two vendors and never two copies.",
    ),
    ProfileTable(
        table="web_optics_profiles", what="web-UI optics recipe",
        name_is_identity=True,
        name_note="the name IS identity here: deliberately the SAME token as "
                  "gpon_profiles.name / org_devices.gpon_vendor.",
    ),
    ProfileTable(
        table="web_mac_profiles", what="subscriber-MAC page recipe",
        name_is_identity=True,
        name_note="the name IS identity here: it binds the device the same "
                  "way the optics recipe does.",
    ),
    ProfileTable(
        table="radius_profiles", what="billing-panel export recipe",
        name_is_identity=True,
        name_note="the name IS identity here: `radius_accounts.profile` "
                  "names it, and the ACCOUNT — never the profile — carries "
                  "the host and the password.",
    ),
    ProfileTable(
        table="nvr_profiles", what="camera-state recipe",
        name_is_identity=True,
        name_note="the name IS identity here: it is the declared "
                  "`org_devices.nvr_vendor`.",
    ),
)

# Tables that hold a HOST or a SECRET. Per-org by construction; globalizing one
# would be a cross-tenant breach. They are named here so the boundary is a
# statement in the code and not a thing somebody remembers.
_FORBIDDEN_TABLES = frozenset({
    "radius_accounts", "device_webui_credentials", "org_devices",
    "node_tokens", "field_tokens", "users", "app_settings", "orgs",
})
assert not {t.table for t in TABLES} & _FORBIDDEN_TABLES, \
    "this tool promotes RECIPES; it may never touch accounts or credentials"

# Columns every recipe table must have for the shape below to make sense.
_REQUIRED = ("id", "org_id", "name", "enabled", "created_at", "updated_at")
# The JSON body, in the order we look for it (snmp_profiles calls it metrics).
_BODY_COLUMNS = ("metrics", "spec")

# The host/secret tripwire. A key that HOLDS a location or a credential — not
# one that NAMES a form field: `password_field: "pass"` is the vendor's input
# name and is a fact about the page, while `password: "..."` would be a secret.
_HOST_KEYS = frozenset({"host", "hostname", "server", "url", "base_url",
                        "baseurl", "endpoint", "address", "ip", "origin",
                        "domain"})
_SECRET_KEYS = frozenset({"password", "passwd", "pass", "secret", "token",
                          "api_key", "apikey", "key_secret", "community",
                          "credential", "credentials", "auth",
                          "authorization"})
_SCHEME = re.compile(r"[a-z][a-z0-9+.\-]*://", re.I)


def norm_oid(raw: object) -> str:
    """The prefix as the edge's matchers read it (strip, then strip dots)."""
    return str(raw or "").strip().strip(".")


def cell(row: sqlite3.Row, column: str, default: str = "") -> object:
    """A column that may not exist on this table's shape."""
    return row[column] if column in row.keys() else default


def canonical_body(raw: object) -> str:
    """The recipe JSON with key order removed, so formatting is not substance.

    Unparsable JSON keeps its RAW text: the store's readers turn a broken body
    into `{}` silently, and two rows must never look equivalent because both
    failed to parse.
    """
    text = str(raw or "")
    try:
        return "json:" + json.dumps(json.loads(text), sort_keys=True,
                                    separators=(",", ":"))
    except (TypeError, ValueError):
        return "raw:" + text


def tripwire(raw: object) -> list[str]:
    """Anything in this recipe that looks like a host or a credential.

    REFUSES on a hit; never certifies on a miss. A vendor recipe is paths,
    headings and OIDs — a location or a password in one is either a mistake or
    a boundary violation, and either way not something to hand every org.
    """
    try:
        loaded = json.loads(str(raw or ""))
    except (TypeError, ValueError):
        return []
    found: list[str] = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                key = str(k)
                here = f"{path}.{key}" if path else key
                low = key.strip().lower()
                if low in _HOST_KEYS and str(v or "").strip():
                    found.append(f"{here} (host)")
                elif low in _SECRET_KEYS and str(v or "").strip():
                    found.append(f"{here} (secret)")
                walk(v, here)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str) and _SCHEME.search(node):
            found.append(f"{path} (absolute URL)")

    walk(loaded, "")
    return found


@dataclass(frozen=True)
class Shape:
    """What this DB actually says the table looks like."""
    body: str
    has_prefix: bool


def discover(conn: sqlite3.Connection, spec: ProfileTable) -> Shape | str:
    """The table's shape, or a sentence saying why it cannot be processed."""
    cols = [r["name"] for r in
            conn.execute(f"PRAGMA table_info({spec.table})")]
    if not cols:
        return "no such table in this DB — nothing to do"
    missing = [c for c in _REQUIRED if c not in cols]
    if missing:
        return f"unexpected shape (no {', '.join(missing)}) — skipped"
    body = next((c for c in _BODY_COLUMNS if c in cols), None)
    if body is None:
        return (f"no recipe column (looked for "
                f"{'/'.join(_BODY_COLUMNS)}) — skipped")
    return Shape(body=body, has_prefix="match_sysobjectid" in cols)


def selector(row: sqlite3.Row) -> tuple[str, str]:
    """What makes two rows compete for the same device."""
    prefix = norm_oid(cell(row, "match_sysobjectid"))
    if prefix:
        return ("sysObjectID", prefix)
    return ("name", str(row["name"]))


def fingerprint(spec: ProfileTable, shape: Shape, row: sqlite3.Row) -> str:
    """The whole substance of a row, as one comparable string."""
    parts = [norm_oid(cell(row, "match_sysobjectid")),
             canonical_body(row[shape.body]),
             "enabled" if row["enabled"] else "disabled"]
    if spec.name_is_identity:
        parts.append(str(row["name"]))
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def equivalent(spec: ProfileTable, rows, shape: Shape | None = None) -> bool:
    """Do these rows say the same thing? (the check the collapse rides on)"""
    shape = shape or Shape(body="metrics" if spec.table == "snmp_profiles"
                           else "spec", has_prefix=True)
    return len({fingerprint(spec, shape, r) for r in rows}) <= 1


@dataclass
class Group:
    spec: ProfileTable
    shape: Shape
    key: tuple[str, str]
    rows: list[sqlite3.Row]                    # every row in the group
    verdict: str = "done"                      # collapse|prune|promote|
    #                                            singleton|conflict|unsafe|done
    survivor: sqlite3.Row | None = None
    drop: list[sqlite3.Row] = field(default_factory=list)
    variants: list[list[sqlite3.Row]] = field(default_factory=list)
    gained_by: list[str] = field(default_factory=list)
    leaks: list[tuple[sqlite3.Row, list[str]]] = field(default_factory=list)


def _oldest(rows: list[sqlite3.Row]) -> sqlite3.Row:
    return min(rows, key=lambda r: (str(r["created_at"] or ""), int(r["id"])))


def plan_table(conn: sqlite3.Connection, spec: ProfileTable, shape: Shape, *,
               org_ids: list[str], keep: set[int],
               promote_singletons: bool) -> list[Group]:
    rows = conn.execute(f"SELECT * FROM {spec.table} ORDER BY id").fetchall()
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(selector(row), []).append(row)

    out: list[Group] = []
    for key in sorted(groups):
        members = groups[key]
        g = Group(spec=spec, shape=shape, key=key, rows=members)
        globals_ = [r for r in members if r["org_id"] is None]
        locals_ = [r for r in members if r["org_id"] is not None]

        by_fp: dict[str, list[sqlite3.Row]] = {}
        for row in members:
            by_fp.setdefault(fingerprint(spec, shape, row), []).append(row)
        g.variants = [by_fp[fp] for fp in sorted(by_fp)]
        g.leaks = [(r, hits) for r in members
                   if (hits := tripwire(r[shape.body]))]

        if g.leaks:
            # A recipe carrying a host or a credential is never widened, and
            # the finding matters more than the duplicate.
            g.verdict = "unsafe"
        elif len(by_fp) > 1:
            # Two recipes claiming one selector. Refused whole: which one is
            # right is a fact about hardware, and the tool has no way to know.
            g.verdict = "conflict"
        elif globals_:
            # Already global. Any org row here is a redundant copy the SPA
            # made; the global survives so no id moves.
            g.survivor = globals_[0]
            g.drop = locals_ + globals_[1:]
            g.verdict = "prune" if g.drop else "done"
        elif len(locals_) >= 2:
            g.survivor = next((r for r in locals_ if int(r["id"]) in keep),
                              _oldest(locals_))
            g.drop = [r for r in locals_ if r["id"] != g.survivor["id"]]
            g.verdict = "collapse"
        elif locals_ and promote_singletons:
            g.survivor = locals_[0]
            g.verdict = "promote"
        elif locals_:
            g.verdict = "singleton"

        if g.verdict in ("collapse", "promote"):
            # A recipe that was already global is gained by nobody; only a
            # promotion widens who matches it, and that IS a behaviour change.
            held = {r["org_id"] for r in members}
            g.gained_by = [o for o in org_ids if o not in held]
        out.append(g)
    return out


def _row_line(row: sqlite3.Row, tag: str) -> str:
    scope = row["org_id"] if row["org_id"] is not None else "(global)"
    flag = "" if row["enabled"] else "  [disabled]"
    return (f"      {tag:<6} #{int(row['id']):<3} {str(scope):<17}"
            f"{row['name']!r}{flag}")


def _describe(spec: ProfileTable, shape: Shape, row: sqlite3.Row) -> str:
    body = row[shape.body]
    try:
        loaded = json.loads(str(body or ""))
    except (TypeError, ValueError):
        return f"{spec.what}: UNPARSABLE JSON ({len(str(body or ''))} bytes)"
    if not isinstance(loaded, dict):
        return f"{spec.what}: {type(loaded).__name__}"
    if isinstance(loaded.get("oids"), dict):
        oids = loaded["oids"]
        return f"{spec.what}: {len(oids)} OID(s) — {', '.join(sorted(oids))}"
    if loaded and all(isinstance(v, dict) and "oid" in v
                      for v in loaded.values()):
        return (f"{spec.what}: {len(loaded)} metric(s) — "
                f"{', '.join(sorted(loaded))}")
    keys = sorted(loaded)
    shown = ", ".join(keys[:6]) + (", …" if len(keys) > 6 else "")
    return f"{spec.what}: {len(keys)} key(s) — {shown or 'none'}"


def print_group(g: Group) -> None:
    kind, value = g.key
    head = f"{value}" if kind == "sysObjectID" else f"name {value!r}"
    label = {"collapse": f"COLLAPSE {len(g.rows)} -> 1 global",
             "prune": f"PRUNE {len(g.drop)} redundant copy/copies",
             "promote": "PROMOTE singleton -> global",
             "singleton": "skipped · single org row, no duplicate",
             "conflict": f"REFUSED · {len(g.variants)} different recipes",
             "unsafe": "REFUSED · carries a host or a credential",
             "done": "nothing to do"}[g.verdict]
    print(f"  {head:<34} {label}")

    if g.verdict == "unsafe":
        for row, hits in g.leaks:
            print(_row_line(row, "!"))
            print(f"      {', '.join(hits)}")
        print("      a recipe is paths, headings and OIDs. A host or a "
              "password in one belongs to the ACCOUNT,")
        print("      and widening it to every org would be a cross-tenant "
              "leak. Fix the row, then re-run.")
        return

    if g.verdict == "conflict":
        for n, variant in enumerate(g.variants, 1):
            print(f"      variant {n}: {_describe(g.spec, g.shape, variant[0])}")
            for row in variant:
                print(_row_line(row, "·"))
        print(f"      these rows are selected by the same {kind} but do not "
              f"say the same thing.")
        print(f"      a human decides which is right — {g.spec.name_note}")
        return

    if g.verdict == "singleton":
        for row in g.rows:
            print(_row_line(row, "·"))
        print("      one org entered it; that is not evidence it is right for "
              "another org's box.")
        print("      --promote-singletons makes it global.")
        return

    if g.verdict == "done":
        for row in g.rows:
            print(_row_line(row, "·"))
        return

    print(_row_line(g.survivor, "keep")
          + ("  -> org_id NULL" if g.survivor["org_id"] is not None else ""))
    for row in g.drop:
        print(_row_line(row, "drop"))
    print(f"      identical {_describe(g.spec, g.shape, g.survivor)}")

    names = {str(r["name"]) for r in g.rows}
    if len(names) > 1:
        counts: dict[str, int] = {}
        for row in g.rows:
            counts[str(row["name"])] = counts.get(str(row["name"]), 0) + 1
        most = max(counts, key=lambda n: (counts[n], n))
        if most != str(g.survivor["name"]):
            print(f"      NAME: {counts[most]} of {len(g.rows)} rows are "
                  f"named {most!r}; the survivor is named "
                  f"{str(g.survivor['name'])!r}")
            print(f"      and that is the label every org will see. "
                  f"{g.spec.name_note}")
            print(f"      This tool never rewrites a name: pass "
                  f"--keep <id> to survive a different row, or rename it "
                  f"in Settings after.")
    if g.gained_by:
        print(f"      after: {len(g.gained_by)} org(s) that had no row here "
              f"will now match it ({', '.join(g.gained_by)})")


def apply_group(conn: sqlite3.Connection, g: Group, now: str) -> tuple[int, int]:
    promoted = dropped = 0
    if g.survivor is not None and g.survivor["org_id"] is not None:
        # UPDATE, never INSERT: the row keeps its id, its name and its
        # created_at. Only its SCOPE moved, and updated_at says when.
        conn.execute(f"UPDATE {g.spec.table} SET org_id=NULL, updated_at=?"
                     " WHERE id=?", (now, int(g.survivor["id"])))
        promoted = 1
    for row in g.drop:
        conn.execute(f"DELETE FROM {g.spec.table} WHERE id=?", (int(row["id"]),))
        dropped += 1
    return promoted, dropped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=None, help="central DB (default: config)")
    ap.add_argument("--table", default=None,
                    choices=[t.table for t in TABLES],
                    help="one recipe table (default: every one)")
    ap.add_argument("--keep", type=int, action="append", default=[],
                    metavar="ID", help="this row survives its group "
                                       "(repeatable); default is the oldest")
    ap.add_argument("--promote-singletons", action="store_true",
                    help="also promote a lone org row with no duplicate. "
                         "A judgment call — read the report first.")
    ap.add_argument("--apply", action="store_true",
                    help="write the change. Without this it only reports.")
    args = ap.parse_args()

    cfg = Config(central_db=Path(args.db)) if args.db else CONFIG
    # PRINT THE RESOLVED PATH BEFORE OPENING IT. Every store-touching script
    # here defaults to data/central.db, which IS production; a rehearsal once
    # migrated the live DB because nothing said which file it had picked.
    path = Path(cfg.central_db).resolve()
    if not path.exists():
        print(f"db: {path}\nno such database")
        raise SystemExit(1)
    mode = "read-write" if args.apply else "READ-ONLY (mode=ro)"
    print(f"db: {path}  [{mode}]")

    uri = f"file:{path}" + ("" if args.apply else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None   # transactions are explicit here, or the
    #                               plan and the write could straddle two
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    tables = [t for t in TABLES if not args.table or t.table == args.table]
    print(f"promote vendor recipes to global · "
          f"{'APPLY' if args.apply else 'DRY RUN'}"
          + ("  · singletons included" if args.promote_singletons else ""))

    # One transaction for the whole run, opened BEFORE the plan is read, so an
    # --apply can never act on a plan the SPA moved out from under it.
    if args.apply:
        conn.execute("BEGIN IMMEDIATE")

    org_ids = [r["org_id"] for r in
               conn.execute("SELECT org_id FROM orgs ORDER BY org_id")]
    plans: list[Group] = []
    for spec in tables:
        shape = discover(conn, spec)
        if isinstance(shape, str):
            print(f"\n{spec.table} · {shape}")
            continue
        groups = plan_table(conn, spec, shape, org_ids=org_ids,
                            keep=set(args.keep),
                            promote_singletons=args.promote_singletons)
        rows = sum(len(g.rows) for g in groups)
        if not rows:
            print(f"\n{spec.table} · empty — nothing to do")
            continue
        print(f"\n{spec.table} · {rows} rows in {len(groups)} selector "
              f"group(s) · {spec.name_note}\n")
        for g in groups:
            print_group(g)
        plans.extend(groups)

    acting = [g for g in plans if g.verdict in ("collapse", "prune", "promote")]
    refused = [g for g in plans if g.verdict in ("conflict", "unsafe")]
    unsafe = [g for g in plans if g.verdict == "unsafe"]
    skipped = [g for g in plans if g.verdict == "singleton"]
    to_drop = sum(len(g.drop) for g in acting)

    print(f"\n  {len(acting)} group(s) to collapse, {to_drop} row(s) deleted, "
          f"{len(refused)} refused, {len(skipped)} singleton(s) left alone")
    if unsafe:
        print(f"  ! {len(unsafe)} group(s) carry a host or a credential — "
              f"read those lines before anything else")
    unused = sorted(set(args.keep) - {int(g.survivor["id"]) for g in acting
                                      if g.survivor is not None})
    if unused:
        print(f"  ! --keep {unused} named no row that survives a collapse")

    if not args.apply:
        conn.close()
        print("  dry run. Back up, then re-run with --apply, and restart "
              "central in the same breath.")
        return

    promoted = dropped = 0
    try:
        for g in acting:
            p, d = apply_group(conn, g, now)
            promoted += p
            dropped += d
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        conn.close()
        print(f"  ABORTED, nothing written: {exc}")
        raise SystemExit(1)
    conn.close()
    print(f"  promoted {promoted} row(s) to global, deleted {dropped} copy/copies")
    print("  RESTART CENTRAL NOW — the running process serves /edge/devices "
          "from these rows.")


if __name__ == "__main__":
    main()
