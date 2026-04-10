"""SQLite connection helper.

Phase 1 has no connection pool — each caller (web request, scheduler tick) opens
its own short-lived connection. WAL mode handles concurrent readers and a single
writer; foreign-key enforcement is enabled per-connection because SQLite defaults
it off.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection configured for ArtimesOne.

    - ``row_factory`` is set to :class:`sqlite3.Row` so callers can index columns
      by name.
    - ``PRAGMA journal_mode=WAL`` is enabled (plan §2.3).
    - ``PRAGMA foreign_keys=ON`` is enabled — SQLite defaults this off and we
      need it for the FK constraints in migration 0001.

    The caller is responsible for closing the connection.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn
