"""Tracking cell HTTP API (SPEC-003).

The rules are the ones the web's former ``/api/internal/tracking`` route ran in Next.js: items are
shared between users, what a user owns is their ``user_tracking`` row, and every change writes one
activity-log row. Here the write and its activity row commit in one transaction, and a replayed
``Idempotency-Key`` returns the first answer without writing again.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import psycopg
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field

from tracking_api.auth import READ_SCOPE, WRITE_SCOPE, Forbidden, Principal, TokenVerifier, Unauthenticated

CATEGORIES = ("ANIME", "MANGA", "MOVIE", "TV_SHOW", "BOOK", "WEBSITE", "PODCAST", "GAME", "OTHER")
STATUSES = ("PLAN_TO_WATCH", "WATCHING", "COMPLETED", "ON_HOLD", "DROPPED")
RATINGS = ("ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE", "TEN")
UPDATABLE = ("status", "progress", "rating", "is_favorite", "priority", "start_date", "finish_date")
CAMEL = {
    "is_favorite": "isFavorite",
    "start_date": "startDate",
    "finish_date": "finishDate",
    "image_url": "imageUrl",
    "external_url": "externalUrl",
    "total_episodes": "totalEpisodes",
    "total_chapters": "totalChapters",
    "total_books": "totalBooks",
    "created_at": "createdAt",
    "updated_at": "updatedAt",
    "user_id": "userId",
    "item_id": "itemId",
    "item_title": "itemTitle",
    "is_private": "isPrivate",
}
USER_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
KEY_RE = re.compile(r"[A-Za-z0-9_.:-]{8,128}")
MAX_BODY_BYTES = 16 * 1024
MAX_LIMIT = 100
log = logging.getLogger("tracking_api.access")


class ItemIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str = Field(min_length=1, max_length=500)
    category: str
    description: str | None = Field(default=None, max_length=10000)
    imageUrl: str | None = Field(default=None, max_length=2000)
    externalUrl: str | None = Field(default=None, max_length=2000)
    author: str | None = Field(default=None, max_length=500)
    year: int | None = None
    genres: list[str] = Field(default_factory=list, max_length=50)
    totalEpisodes: int | None = None
    totalChapters: int | None = None
    totalBooks: int | None = None
    status: str | None = None
    rating: str | None = None
    progress: int | None = None
    priority: int | None = None
    isFavorite: bool | None = None


def _camel(row: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            value = value.isoformat().replace("+00:00", "Z")
        out[CAMEL.get(key, key)] = value
    return out


def _new_id() -> str:
    return "c" + uuid.uuid4().hex


def _user(value: str | None) -> str:
    if not value or not USER_RE.fullmatch(value):
        raise HTTPException(400, {"error": "invalid_user", "message": "the user id in the path is invalid"})
    return value


def _check(value: str | None, allowed: tuple[str, ...], name: str) -> str | None:
    if value is None or value == "":
        return None
    if value not in allowed:
        raise HTTPException(400, {"error": "invalid_field", "message": f"unknown {name}"})
    return value


def _parse_date(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(400, {"error": "invalid_field", "message": "bad date"}) from exc


def _cursor(value: str | None) -> int:
    if not value:
        return 0
    try:
        offset = int(base64.urlsafe_b64decode(value + "==").decode())
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, {"error": "invalid_cursor"}) from exc
    if offset < 0:
        raise HTTPException(400, {"error": "invalid_cursor"})
    return offset


def create_app(pool: ConnectionPool, verifier: TokenVerifier) -> FastAPI:
    app = FastAPI(title="Tracking cell", version="1.0.0", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        started = time.monotonic()
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        length = request.headers.get("content-length")
        if request.method in ("POST", "PUT", "PATCH") and (
            request.headers.get("transfer-encoding") or (length and int(length) > MAX_BODY_BYTES)
        ):
            response: Response = JSONResponse({"error": "body_too_large"}, status_code=413)
        else:
            response = await call_next(request)
        response.headers["x-request-id"] = request_id
        family = "/".join(request.url.path.split("/")[:3])[:64]
        print(
            json.dumps(
                {
                    "event": "request",
                    "method": request.method,
                    "path": family,
                    "status": response.status_code,
                    "ms": round((time.monotonic() - started) * 1000),
                    "request_id": request_id,
                }
            ),
            file=sys.stdout,
            flush=True,
        )
        return response

    def principal(authorization: str | None, scope: str) -> Principal:
        try:
            caller = verifier.verify(authorization)
            caller.require(scope)
        except Unauthenticated as exc:
            raise HTTPException(401, {"error": "unauthenticated"}) from exc
        except Forbidden as exc:
            raise HTTPException(403, {"error": "forbidden"}) from exc
        return caller

    @contextmanager
    def tx() -> Iterator[psycopg.Connection[dict[str, Any]]]:
        try:
            with pool.connection() as conn:
                conn.row_factory = dict_row  # type: ignore[assignment]
                with conn.transaction():
                    yield conn  # type: ignore[misc]
        except psycopg.OperationalError as exc:
            raise HTTPException(503, {"error": "unavailable"}) from exc

    def replayed(conn: psycopg.Connection[Any], user: str, key: str | None) -> JSONResponse | None:
        if key is None:
            return None
        if not KEY_RE.fullmatch(key):
            raise HTTPException(400, {"error": "invalid_idempotency_key"})
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{user}:{key}",))
        row = conn.execute(
            "SELECT status, response FROM tracking.idempotency WHERE user_id=%s AND key=%s", (user, key)
        ).fetchone()
        return JSONResponse(row["response"], status_code=row["status"]) if row else None

    def remember(
        conn: psycopg.Connection[Any], user: str, key: str | None, method: str, status: int, body: dict[str, Any]
    ) -> JSONResponse:
        if key is not None:
            conn.execute(
                "INSERT INTO tracking.idempotency (user_id, key, method, status, response) VALUES (%s,%s,%s,%s,%s)",
                (user, key, method, status, json.dumps(body)),
            )
        return JSONResponse(body, status_code=status)

    def activity(
        conn: psycopg.Connection[Any], user: str, action: str, title: str, category: str, details: str | None = None
    ) -> None:
        conn.execute(
            "INSERT INTO tracking.activity_log (id, user_id, action, item_title, category, details) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (_new_id(), user, action, title, category, details),
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, str]:
        with tx() as conn:
            conn.execute("SELECT 1")
        return {"status": "ready"}

    @app.get("/v1/users/{user_id}/tracking")
    def list_tracking(
        user_id: str,
        authorization: str | None = Header(default=None),
        category: str | None = None,
        status: str | None = None,
        isFavorite: bool = False,
        search: str | None = Query(default=None, max_length=200),
        limit: int = Query(default=MAX_LIMIT, ge=1),
        cursor: str | None = None,
    ) -> dict[str, Any]:
        principal(authorization, READ_SCOPE)
        user = _user(user_id)
        _check(category, CATEGORIES, "category")
        _check(status, STATUSES, "status")
        limit = min(limit, MAX_LIMIT)
        offset = _cursor(cursor)
        # Same filter precedence as the former route: favourites replace the status filter.
        where, params = ["t.user_id = %s"], [user]
        if isFavorite:
            where.append("t.is_favorite")
        elif status:
            where.append("t.status = %s")
            params.append(status)
        if category:
            where.append("i.category = %s")
            params.append(category)
        if search:
            where.append("(i.title ILIKE %s OR i.description ILIKE %s OR i.author ILIKE %s)")
            pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params += [pattern, pattern, pattern]
        with tx() as conn:
            rows = conn.execute(
                "SELECT i.*, t.status, t.rating, t.progress, t.start_date, t.finish_date, t.priority, "
                "t.is_favorite, (SELECT count(*) FROM tracking.user_tracking c WHERE c.item_id = i.id) AS trackers "
                "FROM tracking.items i JOIN tracking.user_tracking t ON t.item_id = i.id "
                f"WHERE {' AND '.join(where)} ORDER BY i.updated_at DESC, i.id LIMIT %s OFFSET %s",
                [*params, limit + 1, offset],
            ).fetchall()
        items = []
        for row in rows[:limit]:
            tracking = {
                k: row.pop(k)
                for k in ("status", "rating", "progress", "start_date", "finish_date", "priority", "is_favorite")
            }
            trackers = row.pop("trackers")
            item = _camel(row)
            item["userTrackings"] = [_camel(tracking)]
            item["_count"] = {"userTrackings": trackers}
            items.append(item)
        next_cursor = (
            base64.urlsafe_b64encode(str(offset + limit).encode()).decode().rstrip("=") if len(rows) > limit else None
        )
        return {"items": items, "next_cursor": next_cursor}

    @app.post("/v1/users/{user_id}/tracking", status_code=201)
    def add_tracking(
        user_id: str,
        body: ItemIn,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        principal(authorization, WRITE_SCOPE)
        user = _user(user_id)
        category = _check(body.category, CATEGORIES, "category")
        status = _check(body.status, STATUSES, "status") or "PLAN_TO_WATCH"
        rating = _check(body.rating, RATINGS, "rating")
        try:
            with tx() as conn:
                if (hit := replayed(conn, user, idempotency_key)) is not None:
                    return hit
                existing = conn.execute(
                    "SELECT * FROM tracking.items WHERE lower(title) = lower(%s) AND category = %s "
                    "ORDER BY created_at LIMIT 1",
                    (body.title, category),
                ).fetchone()
                item: dict[str, Any] | None
                if existing:
                    item, source = existing, "existing_item"
                else:
                    item = conn.execute(
                        "INSERT INTO tracking.items (id, title, description, image_url, external_url, category, "
                        "author, year, genres, total_episodes, total_chapters, total_books) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                        (
                            _new_id(),
                            body.title,
                            body.description,
                            body.imageUrl,
                            body.externalUrl,
                            category,
                            body.author,
                            body.year,
                            body.genres,
                            body.totalEpisodes,
                            body.totalChapters,
                            body.totalBooks,
                        ),
                    ).fetchone()
                    source = "new_item"
                assert item is not None
                tracking = conn.execute(
                    "INSERT INTO tracking.user_tracking (id, user_id, item_id, status, rating, progress, priority, "
                    "is_favorite) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                    (_new_id(), user, item["id"], status, rating, body.progress, body.priority, bool(body.isFavorite)),
                ).fetchone()
                assert tracking is not None
                activity(conn, user, "added_to_list" if existing else "created_item", item["title"], item["category"])
                result = {
                    **_camel(tracking),
                    "item": _camel(item),
                    "meta": {"source": source, "category": item["category"], "status": tracking["status"]},
                }
                return remember(conn, user, idempotency_key, "POST", 201, result)
        except psycopg.errors.UniqueViolation as exc:
            raise HTTPException(
                409, {"error": "already_tracked", "message": "This item is already on your list"}
            ) from exc

    @app.put("/v1/users/{user_id}/tracking/{item_id}")
    async def update_tracking(
        user_id: str,
        item_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        principal(authorization, WRITE_SCOPE)
        user = _user(user_id)
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(400, {"error": "invalid_json"}) from exc
        if not isinstance(body, dict):
            raise HTTPException(400, {"error": "invalid_json"})
        changes: dict[str, Any] = {}
        # Omitted fields stay as they are; an explicit null clears the nullable ones.
        if body.get("status"):
            changes["status"] = _check(body["status"], STATUSES, "status")
        if "rating" in body:
            changes["rating"] = None if body["rating"] is None else _check(body["rating"], RATINGS, "rating")
        for src, col in (("progress", "progress"), ("priority", "priority")):
            if src in body:
                value = body[src]
                if value is not None and not isinstance(value, int):
                    raise HTTPException(400, {"error": "invalid_field", "message": f"bad {src}"})
                changes[col] = value
        for src, col in (("startDate", "start_date"), ("finishDate", "finish_date")):
            if src in body:
                changes[col] = _parse_date(body[src]) if body[src] else None
        if body.get("isFavorite") is not None:
            changes["is_favorite"] = bool(body["isFavorite"])
        with tx() as conn:
            if (hit := replayed(conn, user, idempotency_key)) is not None:
                return hit
            before = conn.execute(
                "SELECT t.*, i.title, i.category FROM tracking.user_tracking t JOIN tracking.items i "
                "ON i.id = t.item_id WHERE t.user_id = %s AND t.item_id = %s FOR UPDATE OF t",
                (user, item_id),
            ).fetchone()
            if before is None:
                raise HTTPException(404, {"error": "not_found", "message": "Tracking item not found"})
            after: dict[str, Any] | None = before
            if changes:
                sets = ", ".join(f"{col} = %s" for col in changes)
                after = conn.execute(
                    f"UPDATE tracking.user_tracking SET {sets}, updated_at = now() "
                    "WHERE user_id = %s AND item_id = %s RETURNING *",
                    [*changes.values(), user, item_id],
                ).fetchone()
            assert after is not None
            fields_changed = [CAMEL.get(f, f) for f in UPDATABLE if before[f] != after[f]]
            completed_now = after["status"] == "COMPLETED" and before["status"] != "COMPLETED"
            rating = body.get("rating")
            if body.get("status") == "COMPLETED" and before["status"] != "COMPLETED":
                activity(conn, user, "completed", before["title"], before["category"], f"Rated {rating or 'unrated'}")
            elif rating and rating != before["rating"]:
                activity(conn, user, "rated", before["title"], before["category"], f"Rated {rating}/10")
            result = {
                **_camel(after),
                "meta": {
                    "fields_changed": fields_changed,
                    "completed_now": completed_now,
                    "category": before["category"],
                    "status": after["status"],
                },
            }
            return remember(conn, user, idempotency_key, "PUT", 200, result)

    @app.delete("/v1/users/{user_id}/tracking/{item_id}")
    def remove_tracking(
        user_id: str,
        item_id: str,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        principal(authorization, WRITE_SCOPE)
        user = _user(user_id)
        with tx() as conn:
            if (hit := replayed(conn, user, idempotency_key)) is not None:
                return hit
            gone = conn.execute(
                "DELETE FROM tracking.user_tracking t USING tracking.items i WHERE i.id = t.item_id "
                "AND t.user_id = %s AND t.item_id = %s RETURNING i.title, i.category",
                (user, item_id),
            ).fetchone()
            if gone is None:
                raise HTTPException(404, {"error": "not_found", "message": "Tracking item not found"})
            activity(conn, user, "removed_from_list", gone["title"], gone["category"])
            result = {"message": "Tracking item removed successfully", "meta": {"category": gone["category"]}}
            return remember(conn, user, idempotency_key, "DELETE", 200, result)

    @app.get("/v1/users/{user_id}/export")
    def export_user(user_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """PDPA access: everything this cell holds about one user (AC-06)."""
        principal(authorization, READ_SCOPE)
        user = _user(user_id)
        with tx() as conn:
            tracking = conn.execute(
                "SELECT t.*, i.title, i.category FROM tracking.user_tracking t JOIN tracking.items i "
                "ON i.id = t.item_id WHERE t.user_id = %s ORDER BY t.created_at",
                (user,),
            ).fetchall()
            notes = conn.execute(
                "SELECT * FROM tracking.notes WHERE user_id = %s ORDER BY created_at", (user,)
            ).fetchall()
            acts = conn.execute(
                "SELECT * FROM tracking.activity_log WHERE user_id = %s ORDER BY created_at", (user,)
            ).fetchall()
        return {
            "userId": user,
            "tracking": [_camel(r) for r in tracking],
            "notes": [_camel(r) for r in notes],
            "activity": [_camel(r) for r in acts],
        }

    @app.delete("/v1/users/{user_id}")
    def erase_user(user_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """PDPA erasure (AC-06): the user's rows go; shared items stay (they carry no user id)."""
        principal(authorization, WRITE_SCOPE)
        user = _user(user_id)
        counts = {}
        with tx() as conn:
            for table in ("user_tracking", "notes", "activity_log", "idempotency"):
                cur = conn.execute(f"DELETE FROM tracking.{table} WHERE user_id = %s", (user,))
                counts[table] = cur.rowcount
        return {"erased": counts}

    return app
