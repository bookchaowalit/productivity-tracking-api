"""SPEC-003 scenarios AC-01..AC-06 against a real PostgreSQL."""

from __future__ import annotations

import psycopg
from conftest import USER_A, USER_B

from tracking_api.auth import READ_SCOPE


def _add(as_user, title="Frieren", category="ANIME", user=USER_A, **extra):
    return as_user("POST", "/v1/tracking", user, json={"title": title, "category": category, **extra})


def _activity(url, user):
    with psycopg.connect(url) as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT action FROM tracking.activity_log WHERE user_id=%s ORDER BY created_at", (user,)
            )
        ]


# AC-01: an existing shared item is reused; each user gets one tracking row.
def test_ac01_existing_item_is_shared(as_user, migrated):
    first = _add(as_user)
    assert first.status_code == 201
    assert first.json()["meta"]["source"] == "new_item"
    second = _add(as_user, title="frieren", user=USER_B)  # case-insensitive match, like the old route
    assert second.status_code == 201
    assert second.json()["meta"]["source"] == "existing_item"
    assert second.json()["itemId"] == first.json()["itemId"]
    listing = as_user("GET", "/v1/tracking", USER_B).json()["items"]
    assert len(listing) == 1
    assert listing[0]["_count"] == {"userTrackings": 2}
    assert listing[0]["userTrackings"][0]["status"] == "PLAN_TO_WATCH"
    assert _activity(migrated, USER_A) == ["created_item"]
    assert _activity(migrated, USER_B) == ["added_to_list"]


def test_duplicate_add_is_409(as_user):
    assert _add(as_user).status_code == 201
    again = _add(as_user)
    assert again.status_code == 409


# AC-02: another user's item gives 404 and nothing changes.
def test_ac02_other_users_item_is_404(as_user, migrated):
    item = _add(as_user).json()["itemId"]
    assert as_user("PUT", f"/v1/tracking/{item}", USER_B, json={"status": "COMPLETED"}).status_code == 404
    assert as_user("DELETE", f"/v1/tracking/{item}", USER_B).status_code == 404
    assert as_user("GET", "/v1/tracking", USER_A).json()["items"][0]["userTrackings"][0]["status"] == "PLAN_TO_WATCH"
    assert _activity(migrated, USER_B) == []


def test_update_semantics_and_activity(as_user, migrated):
    item = _add(as_user, progress=3, priority=2).json()["itemId"]
    done = as_user("PUT", f"/v1/tracking/{item}", json={"status": "COMPLETED", "rating": "NINE"})
    assert done.status_code == 200
    meta = done.json()["meta"]
    assert meta["completed_now"] is True
    assert set(meta["fields_changed"]) == {"status", "rating"}
    # omitted fields stay; explicit null clears
    cleared = as_user("PUT", f"/v1/tracking/{item}", json={"progress": None}).json()
    assert cleared["progress"] is None
    assert cleared["priority"] == 2
    rated = as_user("PUT", f"/v1/tracking/{item}", json={"rating": "TEN"})
    assert rated.json()["meta"]["completed_now"] is False
    assert _activity(migrated, USER_A) == ["created_item", "completed", "rated"]


def test_delete_writes_activity(as_user, migrated):
    item = _add(as_user).json()["itemId"]
    gone = as_user("DELETE", f"/v1/tracking/{item}")
    assert gone.status_code == 200
    assert gone.json()["meta"]["category"] == "ANIME"
    assert as_user("GET", "/v1/tracking").json()["items"] == []
    assert _activity(migrated, USER_A)[-1] == "removed_from_list"


# AC-03: a replayed Idempotency-Key returns the first answer and writes nothing new.
def test_ac03_idempotency_key_replay(as_user, migrated):
    headers = {"Idempotency-Key": "add-frieren-0001"}
    first = as_user("POST", "/v1/tracking", json={"title": "Frieren", "category": "ANIME"}, headers=headers)
    again = as_user("POST", "/v1/tracking", json={"title": "Frieren", "category": "ANIME"}, headers=headers)
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json()
    assert _activity(migrated, USER_A) == ["created_item"]


# AC-05: at most 100 rows per page, with a cursor.
def test_ac05_pagination_caps_at_100(as_user):
    for n in range(102):
        assert _add(as_user, title=f"Item {n}", category="BOOK").status_code == 201
    page = as_user("GET", "/v1/tracking?limit=500").json()
    assert len(page["items"]) == 100
    assert page["next_cursor"]
    rest = as_user("GET", f"/v1/tracking?cursor={page['next_cursor']}").json()
    assert len(rest["items"]) == 2
    assert rest["next_cursor"] is None


def test_filters(as_user):
    a = _add(as_user, title="Dune", category="BOOK", author="Herbert").json()["itemId"]
    _add(as_user, title="Akira", category="MANGA")
    as_user("PUT", f"/v1/tracking/{a}", json={"isFavorite": True})
    assert [i["title"] for i in as_user("GET", "/v1/tracking?category=BOOK").json()["items"]] == ["Dune"]
    assert [i["title"] for i in as_user("GET", "/v1/tracking?isFavorite=true").json()["items"]] == ["Dune"]
    assert [i["title"] for i in as_user("GET", "/v1/tracking?search=herb").json()["items"]] == ["Dune"]
    assert as_user("GET", "/v1/tracking?search=%25").json()["items"] == []  # % is literal


# AC-06: PDPA export and erase; shared items stay.
def test_ac06_export_and_erase(as_user, client, token, migrated):
    _add(as_user)
    _add(as_user, user=USER_B)
    auth = {"Authorization": token()}
    exported = client.get(f"/v1/users/{USER_A}/export", headers=auth).json()
    assert len(exported["tracking"]) == 1
    assert exported["activity"][0]["action"] == "created_item"
    erased = client.delete(f"/v1/users/{USER_A}", headers=auth).json()["erased"]
    assert erased["user_tracking"] == 1
    assert as_user("GET", "/v1/tracking", USER_A).json()["items"] == []
    assert len(as_user("GET", "/v1/tracking", USER_B).json()["items"]) == 1  # shared item kept


def test_auth_and_scopes(client, as_user, token):
    assert client.get("/v1/tracking", headers={"X-Tracking-User": USER_A}).status_code == 401
    assert (
        as_user("POST", "/v1/tracking", scopes=READ_SCOPE, json={"title": "x", "category": "BOOK"}).status_code == 403
    )
    assert (
        client.get(
            "/v1/tracking", headers={"Authorization": token(lifetime=7200), "X-Tracking-User": USER_A}
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/v1/tracking", headers={"Authorization": token(audience="other-api"), "X-Tracking-User": USER_A}
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/v1/tracking", headers={"Authorization": token(**{"del": "x"}), "X-Tracking-User": USER_A}
        ).status_code
        == 401
    )
    assert as_user("GET", "/v1/tracking", user="bad user!").status_code == 400


def test_validation_and_limits(as_user, client, token):
    assert _add(as_user, category="NOPE").status_code == 400
    assert as_user("PUT", "/v1/tracking/x", json={"status": "NOPE"}).status_code == 400
    big = {"title": "x", "category": "BOOK", "description": "y" * 20000}
    assert as_user("POST", "/v1/tracking", json=big).status_code == 413
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}
