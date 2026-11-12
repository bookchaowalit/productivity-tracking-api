# productivity-tracking-api — the Tracking cell

Owns the tracking data of [productivity-tracking-web](https://github.com/bookchaowalit-website/productivity-tracking-web).
That covers shared items (anime, manga, books…), each user's tracking row, notes and the activity log.
It replaces the Prisma tables and the 366-line Next.js route the web used to run
(SPEC-003 in the web repository, approved by the owner on 2026-11-12).

- **Runtime:** Railway project `productivity-tracking-api`, service `tracking-api`, Serverless (sleeps when idle).
- **Data:** its own Neon project `productivity-tracking-api` (schema `tracking`). The runtime role `tracking_api`
  connects through the `-pooler` host. Migrations run separately with the owner URL.
- **Ingress:** `https://gateway.bookchaowalit.com/v1/tracking/...` (route key `tracking`), with the origin lock
  `GATEWAY_ASSERTION_MODE` set to `report` first and then `enforce`.
- **Auth:** platform-identity RS256 tokens. The audience is `tracking-api` and the scopes are `tracking.items:read` and
  `tracking.items:write`. The web gets them with `client_credentials` (`private_key_jwt`) as service client
  `tracking-web`, and names the signed-in NextAuth user in the path (`/v1/users/{userId}/...`; the gateway forwards only
  standard headers). Every query is scoped to that user.

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/v1/users/{userId}/tracking` | the user's list; filters `category`, `status`, `isFavorite`, `search`; `limit` ≤ 100 with `next_cursor` |
| POST | `/v1/users/{userId}/tracking` | add; reuses an existing shared item (case-insensitive title + category); 409 if already tracked |
| PUT | `/v1/users/{userId}/tracking/{itemId}` | update; omitted fields stay, explicit `null` clears; 404 if not the user's |
| DELETE | `/v1/users/{userId}/tracking/{itemId}` | remove from the user's list; 404 if not the user's |
| GET | `/v1/users/{userId}/export` | PDPA access export |
| DELETE | `/v1/users/{userId}` | PDPA erasure; shared items stay (they carry no user id) |
| GET | `/health`, `/readyz` | liveness; readiness checks the database |

- Every write and its activity row commit in one transaction.
- Writes accept `Idempotency-Key`: a replay returns the first answer and writes nothing new.
- Bodies are capped at 16 KiB.
- Each request logs one JSON line (method, route family, status, ms, request id) and returns `x-request-id`.

## Run and test

```bash
uv sync --extra dev
uv run pytest --cov      # real PostgreSQL via pgserver; Python 3.12 for its wheels
uv run ruff check src tests && uv run mypy
```

## Operate

- **Migrate:** `TRACKING_DB_URL_MIGRATOR=… uv run tracking-db migrate`. Run it before merging a schema change,
  because a deploy does not migrate.
- **Import from the web's former tables:** `TRACKING_IMPORT_SOURCE_URL=… uv run tracking-db import [--dry-run]`.
  Ids are kept, re-runs skip existing ids, and the report includes per-user parity.
- **Deploy:** `railway up --ci --service tracking-api` from a `git archive` of the CI-green commit.
  Verify `/health`, `/readyz` and one list through the gateway.
- **Configuration** (names only; values live in Railway): `TRACKING_DB_URL_API`, `TRACKING_JWT_ISSUER`,
  `TRACKING_JWT_JWKS_URL`, `TRACKING_DB_POOL_MAX`, `GATEWAY_ASSERTION_MODE`, `GATEWAY_ASSERTION_KEY`, `SENTRY_DSN`.

`gateway.py` and `_sentry.py` are copies of the cellkit references; cells own their code. Keep the pinned
gateway test vector in step with platform-api-gateway.
