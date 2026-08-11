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
        literals = _literal_routes()
        self.assertEqual(len(literals.get("GET", [])), len(GET))
        self.assertEqual(len(literals.get("POST", [])), len(POST))

    def test_the_parser_actually_found_the_tables(self):
        literals = _literal_routes()
        self.assertIn("/api/me", literals.get("GET", []))
        self.assertIn("/api/inventory/fibre/ports", literals.get("GET", []))


if __name__ == "__main__":
    unittest.main()
