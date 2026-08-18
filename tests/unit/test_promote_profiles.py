"""tools/promote_global_profiles.py — the equivalence check and idempotence.

Everything runs against a throwaway DB built by CentralStore, so the schema
under test is the real one and never data/central.db.
"""

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import promote_global_profiles as promote  # noqa: E402

from wisp.central.store import CentralStore  # noqa: E402

SNMP = promote.TABLES[0]
GPON = promote.TABLES[1]
WEBOPTICS = next(t for t in promote.TABLES if t.table == "web_optics_profiles")

# The live web-optics recipe, trimmed: form FIELD NAMES, not credentials.
WEB_SPEC = {"login_page_path": "/action/login.html",
            "optics_path": "/action/onuopmdiag.html",
            "username_field": "user", "password_field": "pass",
            "session": "rotating-key",
            "columns": {"rx_dbm": "RX Power", "serial": "MAC Address"}}

CDATA = {"cpu_pct": {"oid": "1.3.6.1.4.1.37950.1.1.5.10.12.3.0",
                     "decode": "as_is", "select": "first"},
         "temp_c": {"oid": "1.3.6.1.4.1.37950.1.1.5.10.12.5.9.0",
                    "decode": "as_is", "select": "first"}}
CDATA_PREFIX = "1.3.6.1.4.1.37950.1.1.5.10.14.1"


def _row(**kw) -> sqlite3.Row:
    """A stand-in profile row, so the pure checks need no DB at all."""
    base = {"id": 1, "org_id": "a", "name": "n", "match_sysobjectid": "",
            "metrics": "{}", "spec": "{}", "enabled": 1,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00"}
    base.update(kw)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ",".join(base)
    conn.execute(f"CREATE TABLE t ({cols})")
    conn.execute(f"INSERT INTO t VALUES ({','.join('?' * len(base))})",
                 tuple(base.values()))
    return conn.execute("SELECT * FROM t").fetchone()


class EquivalenceTest(unittest.TestCase):
    def test_key_order_and_whitespace_are_not_substance(self):
        a = _row(metrics=json.dumps(CDATA))
        b = _row(metrics=json.dumps(dict(reversed(list(CDATA.items()))),
                                    indent=2))
        self.assertTrue(promote.equivalent(SNMP, [a, b]))

    def test_a_different_OID_is_a_different_recipe(self):
        moved = json.loads(json.dumps(CDATA))
        moved["cpu_pct"]["oid"] = "1.3.6.1.4.1.37950.1.1.5.10.12.9.0"
        self.assertFalse(promote.equivalent(
            SNMP, [_row(metrics=json.dumps(CDATA)),
                   _row(metrics=json.dumps(moved))]))

    def test_a_different_DECODE_is_a_different_recipe(self):
        other = json.loads(json.dumps(CDATA))
        other["temp_c"]["decode"] = "milli"
        self.assertFalse(promote.equivalent(
            SNMP, [_row(metrics=json.dumps(CDATA)),
                   _row(metrics=json.dumps(other))]))

    def test_a_DISABLED_copy_is_not_the_same_recipe(self):
        # A disabled row switches the vendor OFF for that org; folding it into
        # an enabled global would switch it back on behind their back.
        self.assertFalse(promote.equivalent(
            SNMP, [_row(metrics=json.dumps(CDATA), enabled=1),
                   _row(metrics=json.dumps(CDATA), enabled=0)]))

    def test_the_NAME_is_a_label_on_snmp_and_identity_on_gpon(self):
        pair = [_row(name="C-Data/DBC EPON OLT", metrics=json.dumps(CDATA),
                     spec=json.dumps(CDATA)),
                _row(name="C-Data/DBC V1600D EPON OLT",
                     metrics=json.dumps(CDATA), spec=json.dumps(CDATA))]
        self.assertTrue(promote.equivalent(SNMP, pair))
        self.assertFalse(promote.equivalent(GPON, pair))

    def test_unparsable_JSON_never_matches_another_unparsable_row(self):
        # The store's readers turn a broken body into {} silently; two rows
        # must not look equivalent because both failed to parse.
        self.assertFalse(promote.equivalent(
            SNMP, [_row(metrics="{oops"), _row(metrics="{also broken")]))

    def test_a_blank_prefix_selects_by_NAME_not_by_the_blank(self):
        # A blank prefix matches nothing on the edge, so it can never be what
        # makes two rows compete.
        a = _row(name="syrotech_gpon", match_sysobjectid="")
        b = _row(name="cdata_54824", match_sysobjectid="")
        self.assertNotEqual(promote.selector(a), promote.selector(b))
        self.assertEqual(promote.selector(a), ("name", "syrotech_gpon"))
        self.assertEqual(
            promote.selector(_row(match_sysobjectid=".1.2.3. ")),
            ("sysObjectID", "1.2.3"))


class BoundaryTest(unittest.TestCase):
    """RECIPES only — never an account, a host or a credential."""

    def test_the_table_list_can_never_reach_an_account_or_a_secret(self):
        self.assertFalse({t.table for t in promote.TABLES}
                         & promote._FORBIDDEN_TABLES)
        for name in ("radius_accounts", "device_webui_credentials",
                     "org_devices", "node_tokens", "users"):
            self.assertIn(name, promote._FORBIDDEN_TABLES)

    def test_a_form_FIELD_NAME_is_not_a_credential(self):
        # `password_field: "pass"` is what the vendor calls its input box.
        self.assertEqual(promote.tripwire(json.dumps(WEB_SPEC)), [])

    def test_a_host_a_password_and_an_absolute_URL_all_trip_it(self):
        self.assertEqual(promote.tripwire(json.dumps({"base_url": "olt.lan"})),
                         ["base_url (host)"])
        self.assertEqual(
            promote.tripwire(json.dumps({"login_static": {"password": "x"}})),
            ["login_static.password (secret)"])
        self.assertEqual(
            promote.tripwire(json.dumps({"p": ["http://10.0.0.1/x.html"]})),
            ["p[0] (absolute URL)"])

    def test_an_OID_is_not_mistaken_for_a_host(self):
        self.assertEqual(
            promote.tripwire(json.dumps({"oids": {"rx": "1.3.6.1.4.1.37950"}})),
            [])


class ToolRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "central.db"
        CentralStore(self.path)          # the real schema, never the live DB
        with sqlite3.connect(self.path) as conn:
            for org in ("alpha", "beta", "gamma"):
                conn.execute("INSERT INTO orgs (org_id, name, created_at)"
                             " VALUES (?,?,?)", (org, org, "2026-01-01"))

    def add(self, table, org, name, match, body, *, enabled=1, created):
        col = "metrics" if table == "snmp_profiles" else "spec"
        cols = ["org_id", "name", col, "enabled", "created_at", "updated_at"]
        vals = [org, name, json.dumps(body), enabled, created, created]
        with sqlite3.connect(self.path) as conn:
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if "match_sysobjectid" in have:   # the web/RADIUS/NVR tables have none
                cols.insert(2, "match_sysobjectid")
                vals.insert(2, match)
            cur = conn.execute(
                f"INSERT INTO {table} ({','.join(cols)})"
                f" VALUES ({','.join('?' * len(cols))})", tuple(vals))
            return int(cur.lastrowid)

    def rows(self, table="snmp_profiles"):
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                f"SELECT * FROM {table} ORDER BY id")]

    def run_tool(self, *args) -> str:
        argv, buf = sys.argv, io.StringIO()
        sys.argv = ["promote_global_profiles.py", "--db", str(self.path), *args]
        try:
            with contextlib.redirect_stdout(buf):
                promote.main()
        finally:
            sys.argv = argv
        return buf.getvalue()

    def seed_duplicates(self):
        return [
            self.add("snmp_profiles", "alpha", "C-Data/DBC EPON OLT",
                     CDATA_PREFIX, CDATA, created="2026-02-01T00:00:00+00:00"),
            self.add("snmp_profiles", "beta", "C-Data/DBC V1600D EPON OLT",
                     CDATA_PREFIX, CDATA, created="2026-01-01T00:00:00+00:00"),
            self.add("snmp_profiles", "gamma", "C-Data/DBC EPON OLT",
                     CDATA_PREFIX, CDATA, created="2026-03-01T00:00:00+00:00"),
        ]

    def test_a_dry_run_writes_nothing(self):
        self.seed_duplicates()
        before = self.rows()
        out = self.run_tool()
        self.assertIn("DRY RUN", out)
        self.assertIn("COLLAPSE 3 -> 1 global", out)
        self.assertEqual(self.rows(), before)

    def test_the_OLDEST_row_survives_and_keeps_its_id(self):
        _alpha, beta, _gamma = self.seed_duplicates()
        self.run_tool("--apply")
        rows = self.rows()
        self.assertEqual([r["id"] for r in rows], [beta])
        self.assertIsNone(rows[0]["org_id"])
        self.assertEqual(rows[0]["created_at"], "2026-01-01T00:00:00+00:00")

    def test_keep_overrides_which_row_survives(self):
        alpha, _beta, _gamma = self.seed_duplicates()
        self.run_tool("--keep", str(alpha), "--apply")
        self.assertEqual([r["id"] for r in self.rows()], [alpha])

    def test_a_name_disagreement_is_NAMED_not_silently_dropped(self):
        self.seed_duplicates()
        out = self.run_tool()
        self.assertIn("NAME:", out)
        self.assertIn("C-Data/DBC EPON OLT", out)
        self.assertIn("C-Data/DBC V1600D EPON OLT", out)

    def test_rows_that_DISAGREE_are_refused_whole(self):
        moved = json.loads(json.dumps(CDATA))
        moved["cpu_pct"]["oid"] = "1.3.6.1.4.1.37950.9.9.9.0"
        self.add("snmp_profiles", "alpha", "C-Data", CDATA_PREFIX, CDATA,
                 created="2026-01-01T00:00:00+00:00")
        self.add("snmp_profiles", "beta", "C-Data", CDATA_PREFIX, moved,
                 created="2026-02-01T00:00:00+00:00")
        out = self.run_tool("--apply")
        self.assertIn("REFUSED", out)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual([r["org_id"] for r in self.rows()],
                         ["alpha", "beta"])

    def test_a_singleton_is_left_alone_unless_asked(self):
        self.add("snmp_profiles", "alpha", "unitech", "1.3.6.1.4.1.12170.2.3",
                 CDATA, created="2026-01-01T00:00:00+00:00")
        self.run_tool("--apply")
        self.assertEqual(self.rows()[0]["org_id"], "alpha")
        self.run_tool("--promote-singletons", "--apply")
        self.assertIsNone(self.rows()[0]["org_id"])

    def test_two_blank_prefix_gpon_vendors_are_never_merged(self):
        self.add("gpon_profiles", "alpha", "syrotech_gpon", "",
                 {"oids": {"rx": "1.3.6.1.4.1.37950.1.1.6.1.1.3.1.7"}},
                 created="2026-01-01T00:00:00+00:00")
        self.add("gpon_profiles", "beta", "cdata_54824", "",
                 {"oids": {"ident_key": "1.3.6.1.4.1.54824.1.1.5.12.1.12.1.6"}},
                 created="2026-02-01T00:00:00+00:00")
        out = self.run_tool("--apply")
        self.assertIn("2 selector group(s)", out)
        self.assertEqual(len(self.rows("gpon_profiles")), 2)
        self.assertEqual([r["org_id"] for r in self.rows("gpon_profiles")],
                         ["alpha", "beta"])

    def test_a_gpon_name_difference_under_one_prefix_is_two_vendors(self):
        spec = {"oids": {"rx": "1.3.6.1.4.1.50224.1"}}
        self.add("gpon_profiles", "alpha", "stgp08x", "1.3.6.1.4.1.50224",
                 spec, created="2026-01-01T00:00:00+00:00")
        self.add("gpon_profiles", "beta", "stelfiber", "1.3.6.1.4.1.50224",
                 spec, created="2026-02-01T00:00:00+00:00")
        out = self.run_tool("--apply")
        self.assertIn("REFUSED", out)
        self.assertEqual(len(self.rows("gpon_profiles")), 2)

    def test_running_it_TWICE_is_a_no_op_the_second_time(self):
        self.seed_duplicates()
        self.add("snmp_profiles", "alpha", "TP-Link", "1.3.6.1.4.1.11863.5",
                 {"cpu_pct": {"oid": "1.2.3", "decode": "as_is"}},
                 created="2026-01-01T00:00:00+00:00")
        self.add("snmp_profiles", "beta", "TP-Link", "1.3.6.1.4.1.11863.5",
                 {"cpu_pct": {"oid": "1.2.3", "decode": "as_is"}},
                 created="2026-02-01T00:00:00+00:00")
        self.run_tool("--apply")
        after_first = self.rows()
        out = self.run_tool("--apply")
        self.assertEqual(self.rows(), after_first)
        self.assertIn("0 group(s) to collapse, 0 row(s) deleted", out)
        self.assertIn("promoted 0 row(s) to global, deleted 0 copy/copies", out)

    def test_a_copy_of_an_already_global_recipe_is_pruned(self):
        # The SPA keeps posting the device's org, so a new local copy of a
        # global recipe is the state this tool has to keep cleaning up.
        gid = self.add("snmp_profiles", None, "C-Data/DBC EPON OLT",
                       CDATA_PREFIX, CDATA, created="2026-01-01T00:00:00+00:00")
        self.add("snmp_profiles", "alpha", "C-Data/DBC EPON OLT",
                 CDATA_PREFIX, CDATA, created="2026-04-01T00:00:00+00:00")
        out = self.run_tool("--apply")
        self.assertIn("PRUNE", out)
        self.assertEqual([r["id"] for r in self.rows()], [gid])

    def test_a_recipe_carrying_a_host_is_REFUSED_however_duplicated(self):
        leaky = dict(WEB_SPEC, base_url="https://olt.example.com")
        self.add("web_optics_profiles", "alpha", "cdata", "", leaky,
                 created="2026-01-01T00:00:00+00:00")
        self.add("web_optics_profiles", "beta", "cdata", "", leaky,
                 created="2026-02-01T00:00:00+00:00")
        out = self.run_tool("--promote-singletons", "--apply")
        self.assertIn("carries a host or a credential", out)
        self.assertIn("base_url (host)", out)
        self.assertEqual([r["org_id"] for r in self.rows("web_optics_profiles")],
                         ["alpha", "beta"])

    def test_a_clean_web_recipe_collapses_by_NAME_with_no_prefix_column(self):
        self.add("web_optics_profiles", "alpha", "cdata_54824", "", WEB_SPEC,
                 created="2026-01-01T00:00:00+00:00")
        self.add("web_optics_profiles", "beta", "cdata_54824", "", WEB_SPEC,
                 created="2026-02-01T00:00:00+00:00")
        out = self.run_tool("--apply")
        self.assertIn("name 'cdata_54824'", out)
        self.assertEqual([r["org_id"] for r in self.rows("web_optics_profiles")],
                         [None])

    def test_every_recipe_table_is_walked_and_an_empty_one_says_so(self):
        out = self.run_tool()
        for spec in promote.TABLES:
            self.assertIn(spec.table, out)
        self.assertIn("web_mac_profiles · empty — nothing to do", out)
        self.assertIn("radius_profiles · empty — nothing to do", out)
        self.assertIn("nvr_profiles · empty — nothing to do", out)

    def test_a_table_this_DB_predates_is_reported_not_crashed_on(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("DROP TABLE nvr_profiles")
        out = self.run_tool("--apply")
        self.assertIn("nvr_profiles · no such table in this DB", out)

    def test_a_local_row_that_DIFFERS_from_the_global_is_kept(self):
        moved = json.loads(json.dumps(CDATA))
        moved["temp_c"]["oid"] = "1.3.6.1.4.1.37950.7.7.7.0"
        self.add("snmp_profiles", None, "C-Data", CDATA_PREFIX, CDATA,
                 created="2026-01-01T00:00:00+00:00")
        self.add("snmp_profiles", "alpha", "C-Data local", CDATA_PREFIX, moved,
                 created="2026-04-01T00:00:00+00:00")
        out = self.run_tool("--apply")
        self.assertIn("REFUSED", out)
        self.assertEqual(len(self.rows()), 2)


if __name__ == "__main__":
    unittest.main()
