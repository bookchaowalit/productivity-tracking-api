"""One-off copy from the web's former Prisma tables (AC-07).

Ids are kept, so links and the web's cached state stay valid. Users stay in the web (NextAuth);
only their ids travel. Re-running is safe: existing ids are skipped.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

TABLES = (
    (
        "TrackingItem",
        "items",
        {
            "id": "id",
            "title": "title",
            "description": "description",
            "imageUrl": "image_url",
            "externalUrl": "external_url",
            "category": "category",
            "author": "author",
            "year": "year",
            "genres": "genres",
            "totalEpisodes": "total_episodes",
            "totalChapters": "total_chapters",
            "totalBooks": "total_books",
            "createdAt": "created_at",
            "updatedAt": "updated_at",
        },
    ),
    (
        "UserTracking",
        "user_tracking",
        {
            "id": "id",
            "userId": "user_id",
            "itemId": "item_id",
            "status": "status",
            "rating": "rating",
            "progress": "progress",
            "startDate": "start_date",
            "finishDate": "finish_date",
            "priority": "priority",
            "isFavorite": "is_favorite",
            "createdAt": "created_at",
            "updatedAt": "updated_at",
        },
    ),
    (
        "TrackingNote",
        "notes",
        {
            "id": "id",
            "userId": "user_id",
            "itemId": "item_id",
            "content": "content",
            "isPrivate": "is_private",
            "createdAt": "created_at",
            "updatedAt": "updated_at",
        },
    ),
    (
        "ActivityLog",
        "activity_log",
        {
            "id": "id",
            "userId": "user_id",
            "action": "action",
            "itemTitle": "item_title",
            "category": "category",
            "details": "details",
            "createdAt": "created_at",
        },
    ),
)


def _counts_by_user(conn: psycopg.Connection[Any], sql: str) -> dict[str, int]:
    return {r["u"]: r["n"] for r in conn.execute(sql).fetchall()}


class _Rollback(Exception):
    """Raised inside the target transaction to undo a dry run."""


def import_from(source_url: str, target_url: str, *, dry_run: bool = False) -> dict[str, Any]:
    report: dict[str, Any] = {"dry_run": dry_run, "tables": {}}
    try:
        _copy(source_url, target_url, dry_run, report)
    except _Rollback:
        report["note"] = "dry run: rolled back"
    return report


def _copy(source_url: str, target_url: str, dry_run: bool, report: dict[str, Any]) -> None:
    with (
        psycopg.connect(source_url, row_factory=dict_row, connect_timeout=30) as src,
        psycopg.connect(target_url, row_factory=dict_row, connect_timeout=30) as dst,
        dst.transaction(),
    ):
        for src_table, dst_table, columns in TABLES:
            cols_src = ", ".join(f'"{c}"' for c in columns)
            rows = src.execute(f'SELECT {cols_src} FROM "{src_table}"').fetchall()
            cols_dst = ", ".join(columns.values())
            marks = ", ".join(["%s"] * len(columns))
            inserted = 0
            for row in rows:
                values = [
                    str(v) if k in ("category", "status", "rating") and v is not None else v for k, v in row.items()
                ]
                cur = dst.execute(
                    f"INSERT INTO tracking.{dst_table} ({cols_dst}) VALUES ({marks}) ON CONFLICT (id) DO NOTHING",
                    values,
                )
                inserted += cur.rowcount
            report["tables"][dst_table] = {"source": len(rows), "inserted": inserted}
        # Parity per user (AC-07): the same number of tracking rows for every user id.
        src_counts = _counts_by_user(src, 'SELECT "userId" AS u, count(*)::int AS n FROM "UserTracking" GROUP BY 1')
        dst_counts = _counts_by_user(
            dst, "SELECT user_id AS u, count(*)::int AS n FROM tracking.user_tracking GROUP BY 1"
        )
        report["parity_per_user"] = src_counts == dst_counts
        report["users"] = len(src_counts)
        if dry_run:
            raise _Rollback
