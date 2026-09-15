# CBPUPSIS API

Backend service for **CBPUPSIS**, the Cloud-Based PUP Student Information System of the
Polytechnic University of the Philippines. It serves student self-service (profile,
enrollment, schedules, grades, balances, payments), faculty grade submission, and
administration and reporting.

| | |
|---|---|
| Requirements | [docs/project-specs/SRS.md](docs/project-specs/SRS.md) |
| Board | [CBPUPSIS API](https://github.com/orgs/pup-cs-projects/projects/2) |
| Web client | [cbpupsis-client](https://github.com/pup-cs-projects/cbpupsis-client) |
| How to contribute | [CONTRIBUTING.md](CONTRIBUTING.md) |

## Stack

| Concern | Choice |
|---|---|
| Language | Python 3.12 |
| Framework | FastAPI, Pydantic v2 |
| Data | SQLAlchemy 2.0 (async), Alembic, PostgreSQL 16 |
| Auth | JWT with rotating refresh tokens; IAM-style RBAC stored in the database |
| Quality | ruff, bandit, pre-commit, pytest (in-memory SQLite), Bruno (HTTP) |
| Packaging | uv, Docker |
| Hosting | AWS, ap-southeast-1 (compute choice pending; see `docs/tech-book/CONVENTIONS.md`) |

## Quick start

```bash
git clone https://github.com/pup-cs-projects/cbpupsis-api.git
cd cbpupsis-api
git checkout dev

uv sync                            # virtualenv with the app and dev tools
uv run pre-commit install          # commit and commit-message hooks
mkdir -p env && cp env.example env/env.dev
# fill in JWT_SECRET and the two database URLs (see CONTRIBUTING.md)

uv run pytest -q                   # should pass before you change anything
make setup                         # build, start, migrate, and seed with Docker
```

The API reference is at <http://localhost:8000/scalar> in development only. No `make` on
Windows? `CONTRIBUTING.md` lists the plain commands.

## Common commands

| Task | Command |
|---|---|
| Run the tests covering a domain | `uv run pytest tests/domains/<name> -q` |
| Run every test | `uv run pytest -q` |
| Lint and format everything | `uv run pre-commit run --all-files` |
| Start or stop the stack | `make up` / `make down` |
| Follow logs | `make logs` |
| Apply migrations | `make migrate` |
| Create a migration | `make migrate-create MSG="add enrollments table"` |
| Seed permissions and groups | `make seed` |
| Run the HTTP suite | `make seed-e2e && make e2e` |
| List every target | `make help` |

## Layout

```
app/
  api/v1/router.py     mounts every domain router
  core/                logging, errors, middleware, email, outbox
  domains/<name>/      models, schemas, repository, service, router, client, exceptions
  shared/              mixins and pagination
  worker.py            outbox worker (its own process)
tests/                 mirrors app/: tests/domains/<name>/, tests/core/
bruno/                 end-to-end HTTP tests
migrations/            Alembic revisions
scripts/               seeds and scheduled jobs
docker/                Dockerfiles and compose
docs/                  specifications and the tech book
```

## Documentation

| Document | Read it for |
|---|---|
| [docs/tech-book/CONVENTIONS.md](docs/tech-book/CONVENTIONS.md) | the conventions, and the known gaps between the SRS and the scaffold |
| [docs/tech-book/WRITING-ISSUES.md](docs/tech-book/WRITING-ISSUES.md) | how to write epics, stories, tasks, bugs, and decisions |
| [CONTRIBUTING.md](CONTRIBUTING.md) | setup, branches, commits, hooks, CI, reviews |
| [docs/project-specs/SRS.md](docs/project-specs/SRS.md) | the requirements |
| [docs/project-specs/PRD.md](docs/project-specs/PRD.md) | scope, users, access, open questions |
| [docs/project-specs/BACKLOG.md](docs/project-specs/BACKLOG.md) | epics mapped to requirements and issues |
| [docs/project-specs/US-ACS.md](docs/project-specs/US-ACS.md) | user stories and acceptance criteria |
| [docs/project-specs/DESIGN.md](docs/project-specs/DESIGN.md) | the template for a feature's design notes |
| [docs/tech-book/API-GUIDE.md](docs/tech-book/API-GUIDE.md) | how the scaffold works: auth, RBAC, outbox, rate limiting, Docker |
| [docs/tech-book/ERROR-CODES.md](docs/tech-book/ERROR-CODES.md) | the error contract the client depends on |
| [docs/tech-book/TEST-NOTES.md](docs/tech-book/TEST-NOTES.md) | test conventions and traceability |

## Contributing

Branch from `dev`, follow Conventional Commits, and open a PR against `dev`. The full process,
including the definition of done, is in [CONTRIBUTING.md](CONTRIBUTING.md).
