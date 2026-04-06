# SaasMint Core

Django 6 SaaS backend. Python 3.12, uv, PostgreSQL (testcontainers), Celery + Redis.

## Architecture

- `core/saasmint_core/` — framework-agnostic domain layer (domain models, services, repositories interfaces).
- `apps/` — Django apps (`users`, `billing`, `orgs`, `dashboard`, `admin_panel`). Each has models, views, serializers, urls, tests/.
- `config/` — Django settings (base/dev/test/prod), root urls, celery.
- `middleware/` — custom middleware (security, etc).
- Django apps implement repository interfaces from core and wire them to DRF views/serializers.
- Supabase: `SUPABASE_JWT_SECRET` is used both for JWT verification and as the service role key for Admin API calls. Do not add a separate `SUPABASE_SERVICE_ROLE_KEY` setting.

## Prism commands

Use Prism for all workflow operations — it is the source of truth for branching, reviews, and shipping:

- `/prism:create-branch` — create feature/fix/hotfix branches from the correct base
- `/prism:ship` — pre-ship checks, conventional commit, and push
- `/prism:review-and-fix` — run code review and fix all findings
- `/prism:review-and-report-only` — run review without fixing
- `/prism:address-pr-review` — address inline PR review comments
- `/prism:open-pr` — sync base, run tests, open PR
- `/prism:release` — open release PR from dev to main

## Pre-push checklist

Before pushing or opening a PR, always run:

```bash
make lint        # ruff check .
make typecheck   # mypy . (django + core)
make test        # pytest
```

Fix any errors before pushing. Do not skip this.

## Commands

```bash
make dev         # docker compose up (Django + Celery + Postgres + Redis)
make test        # pytest -v
make lint        # ruff check
make format      # ruff format
make typecheck   # mypy
make migrate     # run migrations (stack running)
```

## Code style

- Always use type hints in Python.
