# Agent Instructions

- Human-facing replies are in Thai by default; keep code, commands and identifiers as-is.
- Never print, commit or copy secrets: database URLs, tokens and keys live in Railway variables only.
- Tests use a throwaway local PostgreSQL (pgserver) and locally signed tokens; they must never reach the hosted
  database or platform-identity.
- Before committing: `uv run pytest -q --cov`, `uv run mypy`, `uv run ruff check src tests && uv run ruff format --check src tests`.
- Migrations are append-only: never edit an applied file, add a new numbered one, and apply it with
  `tracking-db migrate` (migrator URL) BEFORE merging, because a deploy ships code only.
- The only caller is productivity-tracking-web (service client `tracking-web` in platform-identity). Never add a
  route that skips the per-user scoping (`/v1/users/{userId}/...`), and keep 404 (not 403) for another user's item.
- Deploy: `railway up --ci --service tracking-api` from a `git archive` of a CI-green commit; then verify `/health`,
  `/readyz` and one list through `https://gateway.bookchaowalit.com/v1/tracking/v1/users/<id>/tracking`.
- Keep the solo-empire registry entry (`tracking` in `repository-catalog/registries/online-systems.yaml`) in sync.
