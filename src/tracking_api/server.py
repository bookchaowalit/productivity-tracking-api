"""Entry points: ``tracking-api`` (serve) and ``tracking-db`` (migrate, import).

Configuration comes from the environment (Railway); secrets are never printed.

| Variable | Meaning |
|---|---|
| ``TRACKING_DB_URL_API`` | runtime role on the Neon ``-pooler`` host |
| ``TRACKING_DB_URL_MIGRATOR`` | migrator (owner) URL, direct host; only for ``tracking-db`` |
| ``TRACKING_JWT_ISSUER`` | platform-identity issuer URL |
| ``TRACKING_JWT_JWKS_URL`` | platform-identity JWK Set URL |
| ``GATEWAY_ASSERTION_MODE`` / ``GATEWAY_ASSERTION_KEY`` | origin lock (``report`` then ``enforce``) |
| ``SENTRY_DSN`` | optional error reporting (scrubbed) |
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse

from tracking_api._sentry import capture, init_sentry

AUDIENCE = "tracking-api"
SYSTEM = "tracking"  # gateway route key


def _https(url: str, name: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1"):
        raise SystemExit(f"{name} must be https")
    return url


def api() -> None:
    init_sentry()
    import uvicorn

    from tracking_api import gateway
    from tracking_api.app import create_app
    from tracking_api.auth import JwksKeys, TokenVerifier
    from tracking_api.db import make_pool

    url = os.environ.get("TRACKING_DB_URL_API", "")
    issuer = os.environ.get("TRACKING_JWT_ISSUER", "")
    jwks_url = os.environ.get("TRACKING_JWT_JWKS_URL", "")
    if not (url and issuer and jwks_url):
        raise SystemExit("set TRACKING_DB_URL_API, TRACKING_JWT_ISSUER and TRACKING_JWT_JWKS_URL")
    jwks = JwksKeys(_https(jwks_url, "TRACKING_JWT_JWKS_URL"))
    pool = make_pool(url, max_size=int(os.environ.get("TRACKING_DB_POOL_MAX", "4")))
    pool.open(wait=False)
    app = create_app(pool, TokenVerifier(issuer, AUDIENCE, jwks.keys_for))
    gateway.install(app, gateway.GatewayCheck.from_env(SYSTEM))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        proxy_headers=False,
        server_header=False,
        limit_concurrency=64,
        timeout_keep_alive=5,
    )


def db() -> None:
    init_sentry()
    from tracking_api.db import migrate
    from tracking_api.importer import import_from

    parser = argparse.ArgumentParser(prog="tracking-db")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply pending migrations (migrator URL)")
    imp = sub.add_parser("import", help="copy rows from the web's former Prisma database")
    imp.add_argument(
        "--source-env",
        default="TRACKING_IMPORT_SOURCE_URL",
        help="name of the env var holding the source URL (never pass the URL itself)",
    )
    imp.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    target = os.environ.get("TRACKING_DB_URL_MIGRATOR", "")
    if not target:
        raise SystemExit("set TRACKING_DB_URL_MIGRATOR")
    try:
        if args.command == "migrate":
            print(json.dumps({"applied": migrate(target)}))
        else:
            source = os.environ.get(args.source_env, "")
            if not source:
                raise SystemExit(f"set {args.source_env}")
            print(json.dumps(import_from(source, target, dry_run=args.dry_run)))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - only the error class leaves the process
        capture(exc)
        print(json.dumps({"status": "error", "error": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(2) from None
