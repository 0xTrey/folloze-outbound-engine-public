"""
DB migration runner.

Tracks applied migrations in the applied_migrations table. Applies any .sql
files in db/migrations/ that haven't been recorded yet, in alphabetical order.

Usage:
    python main.py migrate

    Or programmatically:
        from db.migrations import apply_pending
        from db.connection import db_conn
        with db_conn() as conn:
            apply_pending(conn)
"""
from __future__ import annotations

from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def apply_pending(conn) -> int:
    """
    Apply all unapplied .sql migrations in alphabetical order.
    Returns the number of migrations applied.
    """
    with conn.cursor() as cur:
        # Bootstrap the tracking table if it doesn't exist yet
        cur.execute("""
            CREATE TABLE IF NOT EXISTS applied_migrations (
                filename   TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.commit()

        cur.execute("SELECT filename FROM applied_migrations")
        applied = {row[0] for row in cur.fetchall()}

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    applied_count = 0

    for f in migration_files:
        if f.name in applied:
            continue

        print(f"[migrations] Applying {f.name} ...")
        sql = f.read_text()

        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                "INSERT INTO applied_migrations (filename) VALUES (%s)", (f.name,)
            )
        conn.commit()
        applied_count += 1

    if applied_count:
        print(f"[migrations] Done. {applied_count} migration(s) applied.")
    else:
        print("[migrations] All migrations already applied.")

    return applied_count
