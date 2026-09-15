# Docker

Three images, one compose stack.

| File | For | Notes |
|---|---|---|
| `Dockerfile.dev` | local development | dev deps, `--reload`, source bind-mounted, runs as root |
| `Dockerfile.staging` | pre-production | **identical build to prod**; only config and a git-sha label differ |
| `Dockerfile.prod` | production | multi-stage, non-root, no dev deps, healthcheck |

## Local development

```bash
docker compose up            # API on :8000, Postgres on :5432
docker compose logs -f api   # follow logs
docker compose exec api sh   # shell inside the container
docker compose down          # stop (add -v to drop the database)
```

Compose runs `alembic upgrade head` before starting the server, so a fresh clone
comes up with a migrated database. Source is bind-mounted: an edit on the host
reloads the container in a few seconds.

First run on a new project:

```bash
docker compose exec api alembic revision --autogenerate -m "init"
docker compose exec api alembic upgrade head
docker compose exec api python -m scripts.seed_iam
# register a user through /api/v1/auth/register, then:
docker compose exec api python -m scripts.bootstrap_admin you@example.com
```

## Why `.venv` exists when everything runs in Docker

The local virtualenv is the **development** environment; Docker is the
**deployment** environment. They never mix — `.dockerignore` excludes `.venv/`,
and each image builds its own inside the container.

Keep it because:

- Your editor resolves imports through it. Without it, every import shows as
  unresolved in Pylance/mypy.
- Git hooks and editor integrations run `uv run ruff` and `uv run pytest` through it. Round-
  tripping those through Docker on every file save would cost seconds instead of
  milliseconds.
- The suite is in-memory SQLite and needs no services: `uv run pytest` is ~9s
  locally versus a build-and-run cycle.

You can run tests either way — `docker compose exec api pytest` works too.

## Building for deployment

Build from the **project root**, not from `docker/` — the build context must
include `pyproject.toml` and `app/`:

```bash
docker build -f docker/Dockerfile.prod -t api:latest .
docker build -f docker/Dockerfile.staging --build-arg GIT_SHA=$(git rev-parse --short HEAD) -t api:staging .
```

Run it:

```bash
docker run -p 8000:8000 \
  -e DATABASE_URL="postgresql://..." \
  -e JWT_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  -e ENVIRONMENT=production \
  -e LOG_JSON=true \
  api:latest
```

**Secrets are injected at runtime, never baked in.** Anyone who can pull an image
can read every layer of it.

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | yes | pooled URL for the app |
| `DIRECT_DATABASE_URL` | for migrations | non-pooled; Neon's pooler breaks Alembic |
| `JWT_SECRET` | yes | ≥32 chars, or the app refuses to start |
| `ENVIRONMENT` | yes | `production` closes `/docs`, `/scalar`, and `/openapi.json` |
| `LOG_JSON` | no | `true` in deployed environments |
| `CORS_ORIGINS` | no | JSON array of browser origins |

## Migrations in deployment

Run them as a **separate step before** rolling out the new image, not from the
container's `CMD`. Two containers starting at once would both run migrations,
and a failed migration inside `CMD` produces a crash-looping deployment rather
than a clear failure:

```bash
docker run --rm -e DIRECT_DATABASE_URL="..." api:latest alembic upgrade head
```

## Notes

- `.dockerignore` lives at the **project root**, not in `docker/`, because that
  is the build context. The `.venv/` exclusion matters most: a host venv copied
  into an image has wrong platform binaries and a broken interpreter path.
- The healthcheck hits `/health`, which deliberately does not touch the database
  — a check that queried Postgres would get a healthy container killed whenever
  the database was briefly slow.
- No `--workers` in the production CMD: scale by running more containers, so each
  gets its own health check and rolling restart.
