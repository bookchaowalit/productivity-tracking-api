"""Connection pool and the migration runner.

The runtime connects through the Neon ``-pooler`` host with a small bounded pool; migrations use
the separate migrator URL and run only from ``tracking-db migrate`` (never at app start).
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg_pool import ConnectionPool

MIGRATIONS = Path(os.environ.get("TRACKING_APP_ROOT") or Path(__file__).resolve().parents[2]) / "migrations"
RUNTIME_ROLE = "tracking_api"


def make_pool(url: str, *, max_size: int = 4) -> ConnectionPool:
    return ConnectionPool(
        url,
        min_size=0,
        max_size=max_size,
        timeout=5,
        max_idle=300,
        max_lifetime=1800,
        # No startup "options": the Neon pooler (PgBouncer) refuses them. Timeouts are role
        # defaults set by grant_runtime, so every pooled backend has them.
        kwargs={"connect_timeout": 10},
        open=False,
    )


def migrate(url: str, *, migrations: Path = MIGRATIONS, runtime_role: str | None = RUNTIME_ROLE) -> list[str]:
    """Apply pending ``NNNN_*.sql`` files in order; returns the versions applied."""
    applied: list[str] = []
    with psycopg.connect(url, autocommit=False, connect_timeout=30) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS tracking")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tracking.schema_migrations "
            "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        conn.commit()
        done = {row[0] for row in conn.execute("SELECT version FROM tracking.schema_migrations")}
        for path in sorted(migrations.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            version = path.stem
            if version in done:
                continue
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO tracking.schema_migrations (version) VALUES (%s)", (version,))
            conn.commit()
            applied.append(version)
        if runtime_role:
            grant_runtime(conn, runtime_role)
            conn.commit()
    return applied


def grant_runtime(conn: psycopg.Connection, role: str) -> None:
    """Least privilege for the API role: data rights on the tracking tables, nothing else."""
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    if not exists:
        return
    ident = sql.Identifier(role)
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA tracking TO {}").format(ident))
    conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA tracking TO {}").format(ident))
    conn.execute(sql.SQL("REVOKE ALL ON tracking.schema_migrations FROM {}").format(ident))
    db = conn.execute("SELECT current_database()").fetchone()
    assert db is not None
    for setting, value in (
        ("statement_timeout", "10s"),
        ("lock_timeout", "5s"),
        ("idle_in_transaction_session_timeout", "30s"),
        ("timezone", "UTC"),
    ):
        conn.execute(
            sql.SQL("ALTER ROLE {} IN DATABASE {} SET {} = {}").format(
                ident, sql.Identifier(db[0]), sql.Identifier(setting), sql.Literal(value)
            )
        )
