"""Verify platform-identity bearer tokens (RS256) for the ``tracking-api`` audience.

The tracking web is the only caller: it gets a short-lived service token from platform-identity
(``client_credentials`` with ``private_key_jwt``) and names the signed-in user in
``X-Tracking-User``. The token proves the caller is that web; the cell then scopes every query to
the named user, so one user can never read or change another user's rows.

``JwksKeys`` is a copy of the cellkit reference (cells own their code).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt

MAX_LIFETIME_SECONDS = 3600
READ_SCOPE = "tracking.items:read"
WRITE_SCOPE = "tracking.items:write"


class Unauthenticated(Exception):
    pass


class Forbidden(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    subject: str
    scopes: frozenset[str]

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise Forbidden(f"missing scope {scope}")


class JwksKeys:
    """RS256 keys from a JWK Set, cached; an unknown ``kid`` refetches at most once a minute.

    A fetch failure keeps the last good keys, so an identity outage never empties the cache.
    """

    def __init__(
        self,
        url: str,
        *,
        ttl: float = 600,
        min_refresh: float = 60,
        timeout: float = 5.0,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url
        self.ttl, self.min_refresh = ttl, min_refresh
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)
        self.clock = clock
        self._keys: dict[str, Any] = {}
        self._fetched = float("-inf")
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        self._fetched = self.clock()
        try:
            response = self.client.get(self.url)
            response.raise_for_status()
            keys = {}
            for jwk in response.json().get("keys", []):
                if jwk.get("kty") == "RSA" and jwk.get("use", "sig") == "sig" and jwk.get("alg", "RS256") == "RS256":
                    keys[str(jwk.get("kid", ""))] = jwt.PyJWK(jwk, algorithm="RS256").key
        except (httpx.HTTPError, ValueError, TypeError, AttributeError, jwt.PyJWKError):
            return
        if keys:
            self._keys = keys

    def keys_for(self, token: str) -> list[Any]:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError:
            return []
        with self._lock:
            age = self.clock() - self._fetched
            if age >= self.ttl or (kid is not None and kid not in self._keys and age >= self.min_refresh):
                self._refresh()
            if kid is not None:
                return [self._keys[kid]] if kid in self._keys else []
            return list(self._keys.values())


@dataclass(frozen=True)
class TokenVerifier:
    issuer: str
    audience: str
    keys: Callable[[str], list[Any]] = field(compare=False)

    def verify(self, authorization: str | None) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise Unauthenticated("missing bearer token")
        token = authorization.removeprefix("Bearer ")
        try:
            candidates = self.keys(token)
            if not candidates:
                raise Unauthenticated("no verification key")
            claims: dict[str, Any] | None = None
            for index, key in enumerate(candidates):
                try:
                    claims = jwt.decode(
                        token,
                        key,
                        algorithms=["RS256"],
                        audience=self.audience,
                        issuer=self.issuer,
                        options={"require": ["exp", "iat", "sub", "jti"]},
                    )
                    break
                except jwt.InvalidSignatureError:
                    if index == len(candidates) - 1:
                        raise
            assert claims is not None
            if claims["exp"] - claims["iat"] > MAX_LIFETIME_SECONDS:
                raise Unauthenticated("token lifetime too long")
            if any(name in claims for name in ("del", "obo", "act")):
                raise Unauthenticated("delegated tokens are not accepted")
            scopes = frozenset(str(claims.get("scope", "")).split())
            subject = str(claims["sub"])
        except Unauthenticated:
            raise
        except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
            raise Unauthenticated("invalid token") from exc
        return Principal(subject=subject, scopes=scopes)
