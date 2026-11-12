"""A real PostgreSQL (pgserver) per test session, a fresh schema per test, and signed test tokens."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jwt
import psycopg
import psycopg.conninfo
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from tracking_api.app import create_app
from tracking_api.auth import READ_SCOPE, WRITE_SCOPE, TokenVerifier
from tracking_api.db import make_pool, migrate

ISSUER = "https://identity.test"
USER_A = "useraaaaaaaa1"
USER_B = "userbbbbbbbb2"


@pytest.fixture(scope="session")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    pgserver = pytest.importorskip("pgserver")
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("pg")), cleanup_mode="stop")
    yield srv
    srv.cleanup()


@pytest.fixture
def db_url(server: Any) -> Iterator[str]:
    name = "t_" + uuid.uuid4().hex[:12]
    admin = server.get_uri()
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    yield psycopg.conninfo.make_conninfo(admin, dbname=name)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
def migrated(db_url: str) -> str:
    migrate(db_url, migrations=Path(__file__).resolve().parents[1] / "migrations", runtime_role=None)
    return db_url


@pytest.fixture(scope="session")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def token(key: rsa.RSAPrivateKey):
    def make(
        scopes: str = f"{READ_SCOPE} {WRITE_SCOPE}",
        *,
        lifetime: int = 300,
        audience: str = "tracking-api",
        issuer: str = ISSUER,
        **extra: Any,
    ) -> str:
        now = int(time.time())
        claims = {
            "iss": issuer,
            "aud": audience,
            "sub": str(uuid.uuid4()),
            "iat": now,
            "exp": now + lifetime,
            "jti": uuid.uuid4().hex,
            "scope": scopes,
            **extra,
        }
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        return "Bearer " + jwt.encode(claims, pem, algorithm="RS256")

    return make


@pytest.fixture
def client(migrated: str, key: rsa.RSAPrivateKey) -> Iterator[TestClient]:
    pool = make_pool(migrated)
    pool.open()
    public = key.public_key()
    app = create_app(pool, TokenVerifier(ISSUER, "tracking-api", lambda _t: [public]))
    with TestClient(app) as c:
        yield c
    pool.close()


@pytest.fixture
def as_user(client: TestClient, token):
    def call(method: str, path: str, user: str = USER_A, *, scopes: str | None = None, **kw: Any):
        headers = {
            "Authorization": token(scopes) if scopes is not None else token(),
            **kw.pop("headers", {}),
        }
        return client.request(
            method, path.replace("/v1/tracking", f"/v1/users/{user}/tracking", 1), headers=headers, **kw
        )

    return call
