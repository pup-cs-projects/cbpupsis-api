# Contributing to cbpupsis-api

This guide is for the backend team of CBPUPSIS. Read it once, end to end, before your first
pull request. The frontend team has its own guide in
[cbpupsis-client](https://github.com/pup-cs-projects/cbpupsis-client/blob/dev/CONTRIBUTING.md).

| Who | Owns |
|---|---|
| Backend developers (3) | API code, database, migrations, jobs, API tests |
| Frontend developers (2) | The web client, in `cbpupsis-client` |
| Product owner (@JpCurada) | Backlog, priorities, final review, merges to `main` |

Work is tracked on the [CBPUPSIS API board](https://github.com/orgs/pup-cs-projects/projects/2).

## Contents

1. [Prerequisites](#1-prerequisites)
2. [First-time setup](#2-first-time-setup)
3. [Daily workflow](#3-daily-workflow)
4. [Branches and commits](#4-branches-and-commits)
5. [What the hooks check](#5-what-the-hooks-check)
6. [What CI checks](#6-what-ci-checks)
7. [Coding standards](#7-coding-standards)
8. [Writing issues](#8-writing-issues)
9. [Definition of ready and done](#9-definition-of-ready-and-done)
10. [Troubleshooting](#10-troubleshooting)

## 1. Prerequisites

| Tool | Version | Check |
|---|---|---|
| Git | 2.40 or newer | `git --version` |
| Python | 3.12 | `python --version` |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | 0.7 or newer | `uv --version` |
| Docker Desktop | current | `docker compose version` |
| Node.js | 20 or newer (only for the Bruno HTTP suite) | `node --version` |
| make | optional | `make --version` |

**Windows:** run every command below in **Git Bash**. `make` is not installed by default on
Windows. It is optional: each `make` target below also shows the plain command it runs.

## 2. First-time setup

### Clone and install

```bash
git clone https://github.com/pup-cs-projects/cbpupsis-api.git
cd cbpupsis-api
git checkout dev

uv sync                        # creates .venv with the app and dev tools
uv run pre-commit install      # installs the pre-commit and commit-msg hooks
```

`pre-commit install` is not optional. Without it your commits skip formatting and the commit
message check, and CI rejects the PR later.

### Configure your local environment

```bash
mkdir -p env/student env/faculty env/admin
for app in student faculty admin; do cp env.example env/$app/env.dev; done
uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Each app reads its own file: `env/student/env.dev`, `env/faculty/env.dev`, and
`env/admin/env.dev`. Open all three and set the same values in each:

| Key | Local value |
|---|---|
| `JWT_SECRET` | the value printed above |
| `DATABASE_URL` | `postgresql://postgres:postgres@db:5432/app` |
| `DIRECT_DATABASE_URL` | `postgresql://postgres:postgres@db:5432/app` |

The three must match: the apps share one database, and a token issued by one
app is checked by the others. `env/` is gitignored. Never commit it, and never paste its contents into an issue or chat.

### Prove it works

```bash
uv run pytest -q                    # the whole suite; needs no database
uv run pre-commit run --all-files   # every hook over every file
```

Both must pass before you change anything. If they do not, open an issue with the output.

### Run the API

```bash
make setup
```

Without make:

```bash
docker compose -f docker/docker-compose.yml up -d --build
docker compose -f docker/docker-compose.yml run --rm api-admin python -m scripts.seed_iam
```

Compose runs the three apps as separate services. A one-shot `migrate` service applies
migrations once before any app starts. Each app has its own API reference:

| App | API reference | Serves |
|---|---|---|
| Student | <http://localhost:8001/scalar> | auth, users, notifications, items |
| Faculty | <http://localhost:8002/scalar> | auth, users, notifications |
| Admin | <http://localhost:8003/scalar> | auth, users, notifications, iam, audit, and the `/admin` panel |

Stop with `make down` (or `docker compose -f docker/docker-compose.yml down`).

To become an admin locally, register through `POST /api/v1/auth/register` on any app, then
run `make create-admin EMAIL=you@example.com` (or
`docker compose -f docker/docker-compose.yml run --rm api-admin python -m scripts.bootstrap_admin you@example.com`).

## 3. Daily workflow

1. **Pick an issue** from the **Ready** column of the board. Assign yourself and move it to **In Progress**.
2. **Branch from an up-to-date `dev`:**

   ```bash
   git checkout dev
   git pull origin dev
   git checkout -b feature/account-lockout
   ```

3. **Build it in small commits.** Run the tests covering your change as you go:
   `uv run pytest tests/domains/auth -q`.
4. **Before pushing,** pull `dev` again and fix any conflicts on your branch:

   ```bash
   git pull origin dev
   uv run pytest -q
   git push -u origin feature/account-lockout
   ```

5. **Open a pull request against `dev`.** Fill in every section of the template. Link the issue
   with `Closes #N`, and move the issue to **In Review**.
6. **Review.** Request review from at least one other backend developer. Reply to every comment,
   push fixes as new commits, and re-request review.
7. **Merge.** When CI is green and one reviewer has approved, a maintainer **squashes and
   merges**. The PR title becomes the commit on `dev`. Delete the branch afterwards.

`dev` is released to `main` by the product owner through a PR from `dev` to `main`.

## 4. Branches and commits

| Branch | Purpose | Example |
|---|---|---|
| `main` | Released code. Never commit to it directly. | |
| `dev` | Integration branch. Every PR targets it. Never commit to it directly. | |
| `feature/<name>` | New work | `feature/account-lockout` |
| `bugfix/<name>` | A fix for something on `dev` | `bugfix/gpa-excludes-nstp` |
| `hotfix/<name>` | An urgent fix for `main` | `hotfix/login-crash` |

Names are lowercase and hyphen-separated. One issue per branch.

Commit messages and PR titles use **Conventional Commits**:

```
<type>(<scope>): <description>
```

| Type | Use for |
|---|---|
| `feat` | a new feature |
| `fix` | a bug fix |
| `docs` | documentation only |
| `style` | formatting only, no behavior change |
| `refactor` | restructuring that neither fixes a bug nor adds a feature |
| `perf` | a performance improvement |
| `test` | adding or changing tests |
| `chore` | tooling, dependencies, maintenance |

The description is imperative, lowercase, with no trailing period, and under about 72
characters: `feat(auth): add account lockout after five failed logins`. The scope is the domain
or area, one lowercase word.

Do not add `Co-Authored-By` or other attribution trailers.

## 5. What the hooks check

Installed by `uv run pre-commit install`, configured in `.pre-commit-config.yaml`.

| Hook | Stage | Checks |
|---|---|---|
| File hygiene | commit | valid YAML, JSON, TOML; trailing whitespace; final newline; merge markers; large files; mixed line endings; stray debugger calls |
| `detect-private-key` | commit | no private keys in the diff |
| `no-commit-to-branch` | commit | refuses commits on `main` and `dev` |
| `ruff-check --fix` | commit | lint (PEP 8, imports, bugbear, pyupgrade) and fixes what it can |
| `ruff-format` | commit | formatting |
| `bandit` | commit | security lint over `packages/`, `apps/`, `scripts/`, and `main.py` |
| `conventional-pre-commit` | commit-msg | the message follows Conventional Commits |

If a hook changes a file, the commit stops. Run `git add` on the changed files and commit again.

**Never use `git commit --no-verify`.** CI runs the same hooks, so skipping them locally only
moves the failure to your PR.

## 6. What CI checks

| Workflow | When | What |
|---|---|---|
| `ci.yml` | every push to `main` or `dev`, and every PR | pre-commit hooks; `pytest` with coverage; `pip-audit` for vulnerable dependencies |
| `e2e.yml` | every push to `main` or `dev`, and every PR | starts Docker, Postgres, and the API, seeds accounts, runs the Bruno suite |
| `pr-conventions.yml` | every PR | Conventional Commits title; branch name; PR targets `dev` |

The org is on the GitHub Free plan, so these checks cannot be made required. **The team rule is
that nobody merges a PR with a red check.** To read a failure: `gh run view <id> --log-failed`.

## 7. Coding standards

[docs/tech-book/CONVENTIONS.md](docs/tech-book/CONVENTIONS.md) holds the rules and
[docs/tech-book/API-GUIDE.md](docs/tech-book/API-GUIDE.md) explains the reasoning. The
non-negotiable ones:

- Tables live only in `packages/database/src/cbpupsis_database/models/<name>.py`. A feature's
  other layers live in one domain folder: `schemas`, `repository`, `service`, `router`, in
  `apps/api-<role>/src/cbpupsis_api_<role>/domains/<name>/` when one app owns it, or in
  `packages/shared/src/cbpupsis_shared/domains/<name>/` when every app needs it. Its tests
  live in `tests/domains/<name>/`.
- An app never imports another app.
- SQL only in `repository.py`. Rules only in `service.py`. Routers stay thin.
- Every endpoint that takes an id checks both the permission and ownership.
- Someone else's record answers 404, not 403.
- Every model change gets a migration. Read the autogenerated file before committing it:
  autogenerate turns a column rename into drop and add, which loses data.
- Every new error has a row in `docs/tech-book/ERROR-CODES.md`.
- Money is `Decimal`, time is timezone-aware UTC.
- No real student data anywhere: not in fixtures, seeds, screenshots, logs, or issues.

**API contract changes.** If your PR changes a status code, a response field, or an error
`code`, tick that box in the PR template and file a linked task in `cbpupsis-client` so the
frontend team hears about it from you, not from a broken screen.

## 8. Writing issues

Use the issue forms: **User story**, **Task**, **Bug report**, **Decision**. The full guide is
[docs/tech-book/WRITING-ISSUES.md](docs/tech-book/WRITING-ISSUES.md). In short:

- Titles: `EP-NN: <module>` for epics, `US-N: <capability>` for stories, `Decide: <question>` for
  decisions, and a plain imperative sentence for tasks.
- Labels: one `type:`, one `area:`, one `priority:`.
- Stories describe what a user can observe and carry Given, When, Then acceptance criteria with
  ids (`AC-4.1`) that tests reference.
- Attach every story and task to its epic as a **sub-issue**.
- Write plainly. Do not use em dashes.

## 9. Definition of ready and done

**Ready to start** when the issue has:

- [ ] A parent epic, and `type:`, `area:`, and `priority:` labels
- [ ] Acceptance criteria that can fail, with ids for stories
- [ ] An SRS reference
- [ ] No open blocker (no `status: blocked`)

**Done** when:

- [ ] Each acceptance criterion is covered by a test that names its AC id
- [ ] CI is green on the PR
- [ ] Error codes, migrations, and docs are updated where the change needs them
- [ ] One reviewer approved and the PR is squash-merged into `dev`
- [ ] The issue is closed by the merge

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| `Permission denied` on push | Accept the org invitation and confirm you have write access to the repo |
| Commit refused: "Don't commit to branch" | You are on `dev` or `main`. `git checkout -b feature/<name>`, then commit |
| Commit refused by `conventional-pre-commit` | Rewrite the message as `type(scope): description` |
| Hook changed files and the commit stopped | `git add` those files and commit again |
| `make: command not found` on Windows | Use the plain commands shown next to each make target |
| An app container restarts in a loop | `docker compose -f docker/docker-compose.yml logs api-student` (or `api-faculty`, `api-admin`, `migrate`); usually a missing value in that app's `env/<app>/env.dev` |
| Tests pass locally but fail in CI | `gh run view <id> --log-failed`, then run the one failing test by node id |

Still stuck? Comment on your issue with the exact command and output, and mention the product
owner.
