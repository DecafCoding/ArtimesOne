"""SQL migration runner.

Plan §11.5 algorithm: hand-rolled SQL files in this directory, applied in
filename order, tracked in a ``schema_migrations`` table. No Alembic, no
auto-generation, no down migrations.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).parent


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename   TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )


def _applied_filenames(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
    return {row[0] for row in rows}


def _list_migration_files() -> list[Path]:
    return sorted(_MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """Apply any migration files not yet recorded in ``schema_migrations``.

    Returns the list of newly applied filenames (in apply order). Re-running
    against a fully-migrated database is a no-op and returns ``[]``.
    """
    _ensure_migrations_table(conn)
    already_applied = _applied_filenames(conn)
    newly_applied: list[str] = []

    for sql_file in _list_migration_files():
        if sql_file.name in already_applied:
            continue
        sql = sql_file.read_text(encoding="utf-8")
        applied_at = datetime.now(UTC).isoformat()
        # executescript implicitly commits any in-progress transaction; wrap the
        # whole apply-and-record step in BEGIN ... COMMIT so a partial failure
        # doesn't leave the migration half-applied.
        try:
            conn.execute("BEGIN")
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, ?)",
                (sql_file.name, applied_at),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        newly_applied.append(sql_file.name)

    return newly_applied
