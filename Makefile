# make up | make up env=staging | make help
#
# `env` selects the compose files. Production is absent on purpose: it is built
# by CI and run by an orchestrator.

env ?= dev

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

VALID_ENVS := dev staging
ifeq ($(filter $(env),$(VALID_ENVS)),)
$(error unknown env '$(env)'. Use one of: $(VALID_ENVS))
endif

# Read by docker-compose.yml's env_file, so one compose file serves both envs.
export ENV_FILE := $(env)

COMPOSE := docker compose -f docker/docker-compose.yml
ifeq ($(env),staging)
COMPOSE := $(COMPOSE) -f docker/docker-compose.staging.yml
endif

# One env file per app: env/<app>/env.<stage>.
ENV_PATHS := env/student/env.$(env) env/faculty/env.$(env) env/admin/env.$(env)

APPS := api-student api-faculty api-admin

# `run --rm` rather than `exec`, so these work whether or not the stack is up.
# Alembic runs in the one-shot `migrate` service, which waits only for the
# database. Scripts run in api-admin: seeding IAM and managing accounts are
# administrative work, and the admin image is the one that carries scripts/.
ALEMBIC := $(COMPOSE) run --rm migrate alembic
RUN_ADMIN := $(COMPOSE) run --rm api-admin

.DEFAULT_GOAL := help
.PHONY: help build up down restart logs shell ps \
        migrate migrate-status migrate-create migrate-down \
        seed seed-e2e digests prune worker create-admin create-user \
        test test-docker e2e lint fmt check install hooks setup clean

# --- lifecycle --------------------------------------------------------------

build:  ## Build the image for $(env)
	$(COMPOSE) build

up:  ## Start the stack in the background ($(env))
	@missing=0; for f in $(ENV_PATHS); do \
	  if [ ! -f "$$f" ]; then \
	    echo "Missing $$f. Copy the template and fill it in:"; \
	    echo "  mkdir -p $$(dirname $$f) && cp env.example $$f"; \
	    missing=1; \
	  fi; \
	done; exit $$missing
	$(COMPOSE) up -d
	@echo "Student  http://localhost:8001  docs /scalar$(if $(filter staging,$(env)),  (closed in staging),)"
	@echo "Faculty  http://localhost:8002  docs /scalar$(if $(filter staging,$(env)),  (closed in staging),)"
	@echo "Admin    http://localhost:8003  docs /scalar, panel /admin$(if $(filter staging,$(env)),  (closed in staging),  (sign in with an account holding ManageIAM))"
	@echo "Logs     make logs env=$(env)"

down:  ## Stop the stack. Add wipe=1 to also delete the database volume
	$(COMPOSE) down $(if $(wipe),--volumes,)

restart:  ## Restart the three app containers
	$(COMPOSE) restart $(APPS)

logs:  ## Follow logs (service=api-student to narrow)
	$(COMPOSE) logs -f $(service)

ps:  ## Show container status
	$(COMPOSE) ps

shell:  ## Open a shell in an app container (default api-admin; service= to pick)
	$(COMPOSE) exec $(or $(service),api-admin) sh

# --- database ---------------------------------------------------------------

migrate:  ## Apply all pending migrations
	@if ! find packages/migrations/versions -name '*.py' -type f | grep -q .; then \
	  echo "No migrations exist yet. Generating the initial revision..."; \
	  $(ALEMBIC) revision --autogenerate -m "init"; \
	fi
	$(ALEMBIC) upgrade head

migrate-status:  ## Show the current revision and what is pending
	@echo "--- current ---"
	@$(ALEMBIC) current --verbose
	@echo "--- history (newest first) ---"
	@$(ALEMBIC) history --indicate-current

migrate-create:  ## Autogenerate a migration: make migrate-create MSG="add x"
ifndef MSG
	$(error MSG is required. Usage: make migrate-create MSG="add items table")
endif
	$(ALEMBIC) revision --autogenerate -m "$(MSG)"
	@echo
	@echo "Review it before applying — autogenerate emits a column RENAME as"
	@echo "drop-then-add, which is data loss."

migrate-down:  ## Roll back one migration
	$(ALEMBIC) downgrade -1

seed:  ## Seed permissions, policies, and groups (idempotent)
	$(RUN_ADMIN) python -m scripts.seed_iam

digests:  ## Stage the periodic digests (what the scheduler runs)
	$(RUN_ADMIN) python -m scripts.send_digests

prune:  ## Delete rows past their retention window (what the scheduler runs)
	$(RUN_ADMIN) python -m scripts.prune_retention

worker:  ## Run the outbox worker in the foreground, for debugging
	$(COMPOSE) run --rm worker

seed-e2e:  ## Seed the verified accounts the HTTP suite logs in as (idempotent)
	$(RUN_ADMIN) python -m scripts.seed_e2e $(if $(PASSWORD),--password $(PASSWORD),)

# --- users ------------------------------------------------------------------

create-admin:  ## Promote a registered user to Admins: make create-admin EMAIL=you@example.com
ifndef EMAIL
	$(error EMAIL is required. Usage: make create-admin EMAIL=you@example.com)
endif
	$(RUN_ADMIN) python -m scripts.bootstrap_admin $(EMAIL)

create-user:  ## Create a user: make create-user EMAIL=a@b.com GROUP=Members
ifndef EMAIL
	$(error EMAIL is required. Usage: make create-user EMAIL=a@b.com GROUP=Members)
endif
	$(RUN_ADMIN) python -m scripts.create_user $(EMAIL) \
	  $(foreach g,$(GROUP),--group $(g)) \
	  $(if $(PASSWORD),--password $(PASSWORD),)

# --- quality ----------------------------------------------------------------
# Host-side by default: the suite is in-memory SQLite and needs no services, so
# a container round-trip only costs time. `test-docker` runs it on Linux in a
# throwaway uv container: the suite imports every app, and each app image holds
# only its own.

test:  ## Run the test suite (host)
	uv run pytest -q

test-docker:  ## Run the test suite on Linux, in a throwaway container
	docker run --rm -v "$(CURDIR)":/src -w /src -e UV_PROJECT_ENVIRONMENT=/tmp/venv \
	  ghcr.io/astral-sh/uv:0.9.9-python3.12-bookworm-slim uv run --frozen pytest -q

# The second suite: real HTTP against a RUNNING server, so unlike `test` it needs
# the stack up and the e2e accounts seeded. Run on the host because the Bruno CLI
# is an npm package and the API image carries no Node — adding one to a Python
# image to run tests against it from the inside would be the wrong trade.
#
# RATE LIMITS: the suite registers accounts and logs in repeatedly, and the
# shipped limits are production-realistic (5/hour register, 10/minute login).
# One run fits; two back to back do not, and the second reports failures that
# look like broken auth but are 429s. Raise the limits in env/<app>/env.$(env) while
# working on the suite — do NOT switch limiting off, or 05-contract's own
# rate-limit test has nothing to trip:
#
#     RATE_LIMIT_REGISTER=100/hour
#     RATE_LIMIT_LOGIN=100/minute
#
# Counters live in each app's process (memory://), so each app limits on its
# own, and `make restart env=$(env)` clears them instantly if you would rather
# not wait out a window.
e2e:  ## Run the HTTP suite (needs: make up, make seed, make seed-e2e)
	@if [ ! -d bruno/node_modules ]; then \
	  echo "Installing the Bruno CLI (first run only)..."; \
	  npm install --prefix bruno; \
	fi
	@for port in 8001 8002 8003; do \
	  if ! curl -sf http://localhost:$$port/health >/dev/null; then \
	    echo "No app is answering on :$$port. Start the stack first:"; \
	    echo "  make up env=$(env) && make seed env=$(env) && make seed-e2e env=$(env)"; \
	    exit 1; \
	  fi; \
	done
	cd bruno && E2E_PASSWORD=$${E2E_PASSWORD:-E2ETestPassword123!} npm test

lint:  ## Check formatting and lint (host)
	uv run ruff check .

fmt:  ## Fix what can be fixed automatically (host)
	uv run ruff check --fix .
	uv run ruff format .

check: lint test  ## Lint and test — run before opening a PR

# --- setup ------------------------------------------------------------------

install:  ## Create the local virtualenv for the editor, hooks, and fast tests
	uv sync
	uv run pre-commit install

hooks:  ## Run every pre-commit hook over the whole repo
	uv run pre-commit run --all-files

setup:  ## First run on a fresh clone: build, start, migrate, seed
	@$(MAKE) --no-print-directory build env=$(env)
	@$(MAKE) --no-print-directory up env=$(env)
	@$(MAKE) --no-print-directory migrate env=$(env)
	@$(MAKE) --no-print-directory seed env=$(env)
	@echo
	@echo "Ready. Next:"
	@echo "  1. Register at http://localhost:8001/scalar (POST /api/v1/auth/register)"
	@echo "  2. make create-admin EMAIL=you@example.com"

clean:  ## Remove caches and stopped containers
	$(COMPOSE) down --remove-orphans
	rm -rf .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} +

# --- help -------------------------------------------------------------------

help:  ## Show this help
	@echo "Usage: make <target> [env=dev|staging]"
	@echo
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "env is currently '$(env)'."
