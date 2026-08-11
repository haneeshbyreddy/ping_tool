"""The route tables, checked for the mistake a dict literal makes silently.

A DUPLICATE KEY IN A DICT LITERAL DOES NOT RAISE — the later one wins and the
earlier one is simply gone. So a route can be added, reviewed, typechecked and
shipped while answering somebody else's handler, and there is nothing to see in
the diff. It happened on 2026-08-11: `/api/inventory/fibre/ports` was first
written as `/api/inventory/ports`, which had been the per-device SNMP port list
for weeks. The new handler was never reached, and the symptom the operator
reported was "ports are not being detected" — three layers away from the cause.

The tables are already built, so by the time they can be imported the duplicate
has collapsed. This reads the SOURCE instead, the same way the theme allowlist
and the map-detail defaults are pinned to their TypeScript mirrors.
"""
from __future__ import annotations

import ast
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

from wisp.central.api import GET, POST

_SOURCE = pathlib.Path(__file__).resolve().parents[2] \
    / "src" / "wisp" / "central" / "api" / "__init__.py"


def _literal_routes() -> dict[str, list[str]]:
    """Every route string as WRITTEN, per table, before the dict collapses it."""
    tree = ast.parse(_SOURCE.read_text())
    out: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not isinstance(node.value, ast.Dict):
            continue
        out[target.id] = [k.value for k in node.value.keys
                          if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    return out


class RouteTableTest(unittest.TestCase):

    def test_no_route_is_registered_twice(self):
        for table, routes in _literal_routes().items():
            seen: set[str] = set()
            dupes: list[str] = []
            for r in routes:
                if r in seen:
                    dupes.append(r)
                seen.add(r)
            self.assertEqual(
                dupes, [],
                f"{table} registers {dupes} more than once — the LAST one wins and"
                f" the others are silently unreachable")

    def test_the_source_and_the_built_tables_agree(self):
        # If they ever disagree, a key collapsed — the same failure from the
        # other side, and the check that keeps this test honest if the parser
        # above ever stops matching the file's shape.
        literals = _literal_routes()
        self.assertEqual(len(literals.get("GET", [])), len(GET))
        self.assertEqual(len(literals.get("POST", [])), len(POST))

    def test_the_parser_actually_found_the_tables(self):
        # A test that silently parses nothing would pass forever.
        literals = _literal_routes()
        self.assertIn("/api/me", literals.get("GET", []))
        self.assertIn("/api/inventory/fibre/ports", literals.get("GET", []))


if __name__ == "__main__":
    unittest.main()
