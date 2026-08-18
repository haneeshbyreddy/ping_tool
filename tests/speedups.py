"""Test-only performance shims. Same code paths, less work per test.

Three costs dominated a 948 s suite run, and none of them is what any test is
about:

  * Building the schema from DDL for every fresh `CentralStore` — 86 tables and
    48 indexes, 116 ms a time, paid by ~2 800 tests. Here a fresh DB is seeded
    from a TEMPLATE FILE built once per process instead. `__init__` still runs
    in full against the seeded file, so `_SCHEMA`, `_ensure_columns` and every
    data migration still execute — exactly the path a production restart takes
    against an existing DB. A test that prepares its own DB file first (the
    migration tests) is untouched: seeding only ever happens when the file does
    not exist yet.
  * `BaseServer.shutdown()` waiting out the 0.5 s `serve_forever` poll interval
    in every tearDown that ran a real HTTP server. A cProfile of one 29 s module
    put 28.9 s of it inside that one wait. The interval only decides how often
    the accept loop looks at the shutdown flag, so the default is dropped to
    10 ms for tests that do not name one themselves.
  * scrypt at the production work factor — 63 ms per hash, paid on every user
    created and every login. Lowered here only. Production reads `_SCRYPT_N`
    from `auth.py` and never imports this module, so there is no knob a deploy
    can get wrong. The stored hash still carries its own parameters, so the
    format, the verify path and the rehash-on-login upgrade are all exercised
    unchanged.

Installed from `tests/unit/__init__.py` and `tests/integration/__init__.py` so
`unittest discover` picks it up with no per-file opt-in. Import it directly if
you run a module some other way.
"""

from __future__ import annotations

import atexit
import shutil
import socketserver
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wisp.central import auth
from wisp.central.store import CentralStore

_installed = False
_lock = threading.Lock()
_template: Path | None = None
_building = False


def _template_db(original_init) -> Path:
    """Build the schema once and hand back a file to copy for every store."""
    global _template, _building
    if _template is not None:
        return _template
    with _lock:
        if _template is not None:
            return _template
        tmp = Path(tempfile.mkdtemp(prefix="wisp-schema-template-"))
        atexit.register(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "template.db"
        _building = True
        try:
            store = CentralStore.__new__(CentralStore)
            original_init(store, path)
        finally:
            _building = False
        # Fold the WAL back in so the file copies as a self-contained DB.
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        _template = path
    return _template


def install() -> None:
    """Idempotent — the two test packages both call it."""
    global _installed
    if _installed:
        return
    _installed = True

    original_init = CentralStore.__init__

    def __init__(self, db_path, *, migrate: bool = True) -> None:
        path = Path(db_path)
        if migrate and not _building and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(_template_db(original_init), path)
        original_init(self, db_path, migrate=migrate)

    CentralStore.__init__ = __init__

    _serve_forever = socketserver.BaseServer.serve_forever

    def serve_forever(self, poll_interval: float = 0.01):
        return _serve_forever(self, poll_interval)

    socketserver.BaseServer.serve_forever = serve_forever

    # 2**4 keeps scrypt's shape (salt, parameters, verify, upgrade-on-login)
    # while costing microseconds. Nothing asserts on the work factor.
    auth._SCRYPT_N = 2 ** 4
