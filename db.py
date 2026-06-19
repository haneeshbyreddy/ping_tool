"""Database access layer: WAL-mode connections, a tiny migration runner, and a
write-retry helper so transient "database is locked" errors don't corrupt state.

Design notes
------------
* WAL journal mode lets the polling writer and dashboard readers work concurrently.
* Every connection sets `foreign_keys=ON` and `busy_timeout` (PRAGMAs are per-connection
  in SQLite, so this must happen on each connect, not once).
* Migrations are plain `.sql` files in `migrations/`, applied in filename order and
  recorded in `schema_migrations`, so startup is idempotent and forward-only.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from config import CONFIG, Config


def connect(cfg: Config = CONFIG) -> sqlite3.Connection:
    """Open a tuned connection. WAL is persistent on the file, but the other
    PRAGMAs are per-connection and so are (re)applied every time."""
    conn = sqlite3.connect(cfg.db_path, timeout=cfg.busy_timeout_ms / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA synchronous=NORMAL;")  # safe with WAL, far fewer fsyncs
    conn.execute(f"PRAGMA busy_timeout={cfg.busy_timeout_ms};")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Commit on success, roll back on any exception."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def write_with_retry(fn, *, attempts: int = 5, base_delay: float = 0.1):
    """Run a write callable, retrying on transient lock errors with backoff.

    `fn` receives no args; it should do its own connect/transaction. Only
    "database is locked"/"busy" are retried — real errors propagate immediately.
    """
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if ("locked" in msg or "busy" in msg) and attempt < attempts:
                time.sleep(base_delay * (2 ** (attempt - 1)))
                continue
            raise


# --- Migrations -------------------------------------------------------------

def _discover_migrations(cfg: Config) -> list[Path]:
    return sorted(cfg.migrations_dir.glob("*.sql"))


def _applied_versions(conn: sqlite3.Connection) -> set[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    return {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}


def migrate(cfg: Config = CONFIG) -> list[str]:
    """Apply any not-yet-applied migrations in order. Returns versions applied now."""
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    applied_now: list[str] = []
    with connect(cfg) as conn:
        with transaction(conn):
            done = _applied_versions(conn)
            for path in _discover_migrations(cfg):
                version = path.stem  # e.g. '0001_init'
                if version in done:
                    continue
                conn.executescript(path.read_text())
                conn.execute(
                    "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
                )
                applied_now.append(version)
    return applied_now


def integrity_report(cfg: Config = CONFIG) -> dict:
    """Quick self-check used by `python db.py`: confirms WAL + expected indexes."""
    with connect(cfg) as conn:
        journal = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        indexes = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
            )
        ]
        tables = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
    return {"journal_mode": journal, "indexes": indexes, "tables": tables}


if __name__ == "__main__":
    applied = migrate()
    report = integrity_report()
    print(f"migrations applied this run: {applied or '(none, already up to date)'}")
    print(f"journal_mode: {report['journal_mode']}")
    print(f"tables: {', '.join(report['tables'])}")
    print(f"indexes: {', '.join(report['indexes'])}")
