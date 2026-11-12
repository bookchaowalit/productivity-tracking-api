"""Import (AC-07), migrations and grants, the JWK Set cache and the server's config checks."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from tracking_api import server
from tracking_api.auth import JwksKeys, TokenVerifier, Unauthenticated
from tracking_api.db import migrate
from tracking_api.importer import import_from

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
PRISMA = """
CREATE TABLE "TrackingItem" (id text PRIMARY KEY, title text, description text, "imageUrl" text,
  "externalUrl" text, category text, author text, year int, genres text[], "totalEpisodes" int,
  "totalChapters" int, "totalBooks" int, "createdAt" timestamptz DEFAULT now(), "updatedAt" timestamptz DEFAULT now());
CREATE TABLE "UserTracking" (id text PRIMARY KEY, "userId" text, "itemId" text, status text, rating text,
  progress int, "startDate" timestamptz, "finishDate" timestamptz, priority int, "isFavorite" bool,
  "createdAt" timestamptz DEFAULT now(), "updatedAt" timestamptz DEFAULT now());
CREATE TABLE "TrackingNote" (id text PRIMARY KEY, "userId" text, "itemId" text, content text, "isPrivate" bool,
  "createdAt" timestamptz DEFAULT now(), "updatedAt" timestamptz DEFAULT now());
CREATE TABLE "ActivityLog" (id text PRIMARY KEY, "userId" text, action text, "itemTitle" text, category text,
  details text, "createdAt" timestamptz DEFAULT now());
INSERT INTO "TrackingItem" (id, title, category, genres) VALUES ('ci1','Frieren','ANIME','{fantasy}'),('ci2','Dune','BOOK','{}');
INSERT INTO "UserTracking" (id,"userId","itemId",status,rating,"isFavorite") VALUES
  ('ut1','useraaaaaaaa1','ci1','COMPLETED','NINE',true),('ut2','userbbbbbbbb2','ci1','WATCHING',NULL,false),
  ('ut3','useraaaaaaaa1','ci2','PLAN_TO_WATCH',NULL,false);
INSERT INTO "TrackingNote" (id,"userId","itemId",content,"isPrivate") VALUES ('n1','useraaaaaaaa1','ci1','great',true);
INSERT INTO "ActivityLog" (id,"userId",action,"itemTitle",category) VALUES ('a1','useraaaaaaaa1','completed','Frieren','ANIME');
"""


@pytest.fixture
def source(server_db_factory):
    url = server_db_factory()
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(PRISMA)
    return url


@pytest.fixture
def server_db_factory(server):
    made = []

    def make() -> str:
        name = "s_" + uuid.uuid4().hex[:12]
        with psycopg.connect(server.get_uri(), autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{name}"')
        made.append(name)
        return psycopg.conninfo.make_conninfo(server.get_uri(), dbname=name)

    yield make
    with psycopg.connect(server.get_uri(), autocommit=True) as conn:
        for name in made:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def test_ac07_import_keeps_ids_and_parity(source, migrated):
    dry = import_from(source, migrated, dry_run=True)
    assert dry["note"] == "dry run: rolled back"
    with psycopg.connect(migrated) as conn:
        assert conn.execute("SELECT count(*) FROM tracking.items").fetchone()[0] == 0
    report = import_from(source, migrated)
    assert report["parity_per_user"] is True
    assert report["users"] == 2
    assert report["tables"]["user_tracking"] == {"source": 3, "inserted": 3}
    again = import_from(source, migrated)  # re-running skips existing ids
    assert again["tables"]["items"]["inserted"] == 0
    with psycopg.connect(migrated) as conn:
        row = conn.execute("SELECT status, rating, is_favorite FROM tracking.user_tracking WHERE id='ut1'").fetchone()
    assert row == ("COMPLETED", "NINE", True)


def test_migrate_is_idempotent_and_grants_runtime(db_url):
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='tracking_api') "
            "THEN CREATE ROLE tracking_api LOGIN; END IF; END $$"
        )
    assert migrate(db_url, migrations=MIGRATIONS) == ["0001_init"]
    assert migrate(db_url, migrations=MIGRATIONS) == []
    with psycopg.connect(db_url) as conn:
        can = conn.execute(
            "SELECT has_table_privilege('tracking_api','tracking.items','INSERT'), "
            "has_table_privilege('tracking_api','tracking.schema_migrations','SELECT')"
        ).fetchone()
        settings = conn.execute(
            "SELECT s.setconfig FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole "
            "WHERE r.rolname='tracking_api'"
        ).fetchone()[0]
    assert can == (True, False)
    assert "statement_timeout=10s" in settings


def _jwks(key: rsa.RSAPrivateKey, kid: str = "k1") -> dict:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {"keys": [{**jwk, "kid": kid, "use": "sig", "alg": "RS256"}]}


def test_jwks_cache_refreshes_and_survives_outage(key):
    calls = {"n": 0, "fail": False}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500) if calls["fail"] else httpx.Response(200, json=_jwks(key))

    now = {"t": 0.0}
    keys = JwksKeys(
        "https://identity.test/jwks",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: now["t"],
    )
    token = jwt.encode({"sub": "x"}, key, algorithm="RS256", headers={"kid": "k1"})
    assert len(keys.keys_for(token)) == 1
    assert calls["n"] == 1
    calls["fail"] = True
    now["t"] = 10_000  # past the TTL: refetch fails, last good keys stay
    assert len(keys.keys_for(token)) == 1
    unknown = jwt.encode({"sub": "x"}, key, algorithm="RS256", headers={"kid": "zz"})
    assert keys.keys_for(unknown) == []
    assert keys.keys_for("not-a-jwt") == []


def test_verifier_rejects_bad_tokens(token, key):
    verifier = TokenVerifier("https://identity.test", "tracking-api", lambda _t: [key.public_key()])
    with pytest.raises(Unauthenticated):
        verifier.verify(None)
    with pytest.raises(Unauthenticated):
        verifier.verify("Bearer garbage")
    with pytest.raises(Unauthenticated):
        TokenVerifier("https://identity.test", "tracking-api", lambda _t: []).verify(token())
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    two = TokenVerifier("https://identity.test", "tracking-api", lambda _t: [other.public_key(), key.public_key()])
    assert two.verify(token()).scopes  # second key verifies after the first fails
    with pytest.raises(Unauthenticated):
        TokenVerifier("https://identity.test", "tracking-api", lambda _t: [other.public_key()]).verify(token())


def test_server_requires_config(monkeypatch):
    for name in ("TRACKING_DB_URL_API", "TRACKING_JWT_ISSUER", "TRACKING_JWT_JWKS_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit, match="TRACKING_DB_URL_API"):
        server.api()
    with pytest.raises(SystemExit, match="https"):
        server._https("http://example.com/jwks", "X")
    assert server._https("http://localhost/jwks", "X") == "http://localhost/jwks"


def test_db_cli(monkeypatch, migrated, source, capsys):
    monkeypatch.setenv("TRACKING_DB_URL_MIGRATOR", migrated)
    monkeypatch.setattr("sys.argv", ["tracking-db", "migrate"])
    server.db()
    assert json.loads(capsys.readouterr().out) == {"applied": []}
    monkeypatch.setenv("TRACKING_IMPORT_SOURCE_URL", source)
    monkeypatch.setattr("sys.argv", ["tracking-db", "import"])
    server.db()
    assert json.loads(capsys.readouterr().out)["parity_per_user"] is True
    monkeypatch.delenv("TRACKING_DB_URL_MIGRATOR")
    with pytest.raises(SystemExit):
        server.db()
