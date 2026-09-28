# Docker

Nine images (three apps, three stages each), one compose stack.

| File | For | Notes |
|---|---|---|
| `<app>/Dockerfile.dev` | local development | `--reload`, source bind-mounted, runs as root |
| `<app>/Dockerfile.staging` | pre-production | **identical build to prod**; only config and a git-sha label differ |
| `<app>/Dockerfile.prod` | production | multi-stage, non-root, no dev deps, healthcheck |

`<app>` is `student`, `faculty`, or `admin`. Each image contains only its own app
under `apps/` and the kernel packages under `packages/`, and listens on its own
port:

| App | Port | Serves |
|---|---|---|
| student | 8001 | auth, users, notifications, items |
| faculty | 8002 | auth, users, notifications |
| admin | 8003 | auth, users, notifications, iam, audit, and the `/admin` panel (development only) |

The admin images also carry `packages/migrations` (with Alembic) and `scripts/`.
The one-shot `migrate` service, the outbox worker, and the seed scripts all run
from the admin image of the matching stage, because each is administrative work
over the shared database, and reusing that image avoids a fourth image per stage.

## Local development

```bash
make up                                  # everything, in the background
docker compose -f docker/docker-compose.yml logs -f api-student
docker compose -f docker/docker-compose.yml exec api-admin sh
make down                                # stop (add wipe=1 to drop the database)
```

Compose starts, in order: `db`; then `migrate`, which runs `alembic upgrade head`
once and exits; then `api-student`, `api-faculty`, `api-admin`, and `worker`,
which wait for `migrate` to finish successfully. Three app containers never race
each other to migrate the same database. Source is bind-mounted: an edit on the
host reloads the apps in a few seconds.

First run on a new project:

```bash
docker compose -f docker/docker-compose.yml run --rm migrate alembic revision --autogenerate -m "init"
docker compose -f docker/docker-compose.yml run --rm migrate
docker compose -f docker/docker-compose.yml run --rm api-admin python -m scripts.seed_iam
# register a user through POST /api/v1/auth/register on any app, then:
docker compose -f docker/docker-compose.yml run --rm api-admin python -m scripts.bootstrap_admin you@example.com
```

## Why `.venv` exists when everything runs in Docker

The local virtualenv is the **development** environment; Docker is the
**deployment** environment. They never mix: `.dockerignore` excludes `.venv/`,
and each image builds its own inside the container.

Keep it because:

- Your editor resolves imports through it. Without it, every import shows as
  unresolved in Pylance/mypy.
- Git hooks and editor integrations run `uv run ruff` and `uv run pytest` through
  it. Round-tripping those through Docker on every file save would cost seconds
  instead of milliseconds.
- The suite is in-memory SQLite and needs no services.

To run the tests on Linux, `make test-docker` runs them in a throwaway uv
container. No app image can run them: the suite imports all three apps.

## Building for deployment

Build from the **project root**, not from `docker/`: the build context must
include `pyproject.toml`, `uv.lock`, `packages/`, and `apps/`:

```bash
docker build -f docker/student/Dockerfile.prod -t api-student:latest .
docker build -f docker/faculty/Dockerfile.prod -t api-faculty:latest .
docker build -f docker/admin/Dockerfile.prod -t api-admin:latest .
docker build -f docker/student/Dockerfile.staging --build-arg GIT_SHA=$(git rev-parse --short HEAD) -t api-student:staging .
```

Run one:

```bash
docker run -p 8001:8001 \
  -e DATABASE_URL="postgresql://..." \
  -e JWT_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  -e ENVIRONMENT=production \
  -e LOG_JSON=true \
  api-student:latest
```

All three apps must share `DATABASE_URL` and `JWT_SECRET`: a token issued by one
app is accepted by the others, because they verify it with the same secret and
resolve permissions from the same database.

**Secrets are injected at runtime, never baked in.** Anyone who can pull an image
can read every layer of it.

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | yes | pooled URL for the app |
| `DIRECT_DATABASE_URL` | for migrations | non-pooled; Neon's pooler breaks Alembic |
| `JWT_SECRET` | yes | at least 32 chars, or the app refuses to start; the same value in every app |
| `ENVIRONMENT` | yes | `production` closes `/docs`, `/scalar`, and `/openapi.json` |
| `LOG_JSON` | no | `true` in deployed environments |
| `CORS_ORIGINS` | no | JSON array of browser origins |

## Migrations in deployment

Run them as a **separate step before** rolling out new images, not from any
app's `CMD`. Several containers starting at once would all run migrations, and a
failed migration inside `CMD` produces a crash-looping deployment rather than a
clear failure. The admin image carries Alembic and sets `ALEMBIC_CONFIG`, so:

```bash
docker run --rm -e DIRECT_DATABASE_URL="..." -e DATABASE_URL="..." -e JWT_SECRET="..." \
  api-admin:latest alembic upgrade head
```

## Notes

- `.dockerignore` lives at the **project root**, not in `docker/`, because that
  is the build context and Docker reads it only from there. The `.venv/`
  exclusion matters most: a host venv copied into an image has wrong platform
  binaries and a broken interpreter path.
- The healthcheck hits `/health` on the image's own port, which deliberately
  does not touch the database: a check that queried Postgres would get a healthy
  container killed whenever the database was briefly slow.
- No `--workers` in the production CMD: scale by running more containers, so each
  gets its own health check and rolling restart.
