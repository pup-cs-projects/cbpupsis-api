# Contributing to cbpupsis-api

This guide is for the backend team of CBPUPSIS. Read it once, end to end, before your first
pull request. The frontend team has its own guide in
[cbpupsis-client](https://github.com/pup-cs-projects/cbpupsis-client/blob/dev/CONTRIBUTING.md).

| Who | Owns |
|---|---|
| Backend developers (3) | API code, database, migrations, jobs, API tests |
| Frontend developers (2) | The web client, in `cbpupsis-client` |
| Product owner (@JpCurada) | Backlog, priorities, final review, merges to `main` |

Work in both repositories is tracked on one board, [CBPUPSIS](https://github.com/orgs/pup-cs-projects/projects/4).

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
docker compose -f docker/docker-compose.yml run --rm api-admin python -m scripts.seed_programs
```

`make seed` seeds permissions, policies, and groups. `make seed-programs` seeds the BSCS and
BSIT degree programs a student account must name; `make setup` runs both. `make seed-e2e`
seeds the accounts the HTTP suite logs in as.

Compose runs the three apps as separate services. A one-shot `migrate` service applies
migrations once before any app starts. Each app has its own API reference:

| App | API reference | Serves |
|---|---|---|
| Student | <http://localhost:8001/scalar> | sign-in, your own account, notifications, items |
| Faculty | <http://localhost:8002/scalar> | sign-in, your own account, notifications, sections |
| Admin | <http://localhost:8003/scalar> | Admin and Superadmin sign-in, your own account, notifications, user management and CSV import, iam, audit, Superadmin operations, and the `/admin` panel |

Every email the stack sends (verification and reset links, temporary passwords) lands in
**Mailpit** at <http://localhost:8025>, not in a real inbox, as long as your env files keep
`DEV_SMTP_HOST=mailpit` from `env.example`. Without it, emails are only logged by recipient
and subject.

Stop with `make down` (or `docker compose -f docker/docker-compose.yml down`).

There is no public signup, so the first account comes from the console. To become an
admin locally, create a verified account with
`make create-user EMAIL=you@example.com VERIFIED=1` (it prints a generated password once;
without make: `docker compose -f docker/docker-compose.yml run --rm api-admin python -m scripts.create_user you@example.com --verified`), then
run `make create-admin EMAIL=you@example.com` (or
`docker compose -f docker/docker-compose.yml run --rm api-admin python -m scripts.bootstrap_admin you@example.com`).

## 3. Daily workflow

Work flows through one branch per functional requirement (FR) and one branch per user story:

```
dev
 └── fr003                      (FR-003 Enrollment)
      ├── api-us-enrl-001       (US-ENRL-001, backend)
      └── api-us-enrl-002       (US-ENRL-002, backend)
```

Every issue says its branch and where to merge it, in the **In short** box at the top.

1. **Pick an issue** from the **Ready** column of the board. Assign yourself and move it to **In Progress**.
2. **Branch from an up-to-date FR branch.** The issue tells you which one. For US-ENRL-001 under FR-003:

   ```bash
   git fetch origin
   git checkout fr003
   git pull origin fr003
   git checkout -b api-us-enrl-001
   ```

   If the FR branch does not exist yet, the product owner creates it from `dev` first.
   All `[API]` tasks of one story are built on the same story branch.
3. **Build it in small commits.** Run the tests covering your change as you go:
   `uv run pytest tests/domains/enrollment -q`.
4. **Before pushing,** pull the FR branch again and fix any conflicts on your branch:

   ```bash
   git pull origin fr003
   uv run pytest -q
   git push -u origin api-us-enrl-001
   ```

5. **Open a pull request against the FR branch** (`fr003`), not `dev`. Fill in every section of
   the template. List every task it finishes with `Closes #N`, and move the issues to **In Review**.
6. **Review.** Request review from at least one other backend developer. Reply to every comment,
   push fixes as new commits, and re-request review.
7. **Merge.** When CI is green and one reviewer has approved, a maintainer **squashes and
   merges** into the FR branch. Close the issues by hand (GitHub only closes them automatically on
   the default branch). Delete the story branch afterwards.
8. **Finish the FR.** When every story of the FR is merged and tested on its FR branch, the product
   owner opens a PR from `fr003` to `dev` and merges it with a **merge commit**.

**Fixes after testing.** A problem found while testing an FR branch or `dev` gets a short-lived
branch named by what it changes: `bugfix/<name>`, `style/<name>`, or `docs/<name>`. Branch from the
branch you tested (the FR branch or `dev`) and open the PR back into it.

### Releasing

Releases move `dev` -> `staging` -> `main`, each step a pull request merged by the product owner.
A merge into `staging` deploys the staging environment, and a merge into `main` deploys
production (see `deploy.yml` below).

1. Open a PR from `dev` to `staging`. Merging it deploys staging.
2. Check the release on the staging portals.
3. Open a PR from `staging` to `main`. Merging it deploys production.

Use a **merge commit**, not a squash, for `dev` -> `staging` and `staging` -> `main`, so the
three branches keep the same history and the next release PR shows only what is new.

An urgent fix may go `hotfix/<name>` -> `main` directly. Afterwards open a PR from `main` back
into `staging` and from `staging` into `dev`, so the fix is not lost by the next release.

Every migration must be **expand then contract**: a deploy that fails its smoke test rolls the
code back, but not the database, so the previous code must still work against the new schema.
Add a column in one release and drop the old one in a later release, never in the same one.

## 4. Branches and commits

| Branch | Purpose | Example |
|---|---|---|
| `main` | Production. A merge deploys it. Never commit to it directly. | |
| `staging` | The release candidate. A merge deploys staging. Never commit to it directly. | |
| `dev` | Integration branch. Receives finished FR branches and fixes. Never commit to it directly. | |
| `fr00N` | One per functional requirement, from `dev`. Story branches merge into it. Never commit to it directly. | `fr003` |
| `api-us-<module>-<nnn>` | One per user story (all its `[API]` tasks), from its FR branch, merged back into it | `api-us-enrl-001` |
| `api-fr00N-<name>` | An `[API]` task that belongs to an FR but to no story, from that FR branch | `api-fr001-password-policy` |
| `bugfix/<name>` | A fix found while testing, into the branch it was found on | `bugfix/gpa-excludes-nstp` |
| `style/<name>` | A formatting or lint fix found while testing | `style/enrollment-imports` |
| `docs/<name>` | A documentation change | `docs/branch-workflow` |
| `hotfix/<name>` | An urgent fix for `main` | `hotfix/login-crash` |

The FR number is the story's parent FR issue: FR-001 is `fr001`, FR-003 is `fr003`. The story
part is the story id in lowercase: US-ENRL-001 is `us-enrl-001`. The frontend repository uses the
same names with `web-` instead of `api-`, so both halves of a story are easy to match.

| Branch | Branch from | PR into | Merge |
|---|---|---|---|
| `fr00N` | `dev` | `dev` | merge commit, by the product owner |
| `api-us-...`, `api-fr00N-...` | its FR branch | the same FR branch | squash |
| `bugfix/`, `style/`, `docs/` | the FR branch or `dev` | the branch it came from | squash |
| `hotfix/` | `main` | `main` | see Releasing |

Names are lowercase and hyphen-separated. Do not use `feature/` or `refactor/` branches.

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
| `no-commit-to-branch` | commit | refuses commits on `main`, `staging`, and `dev` |
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
| `ci.yml` | every push to `dev`, every PR, and as the first stage of every deploy | pre-commit hooks; `pytest` with coverage; `pip-audit` for vulnerable dependencies |
| `deploy.yml` | every push to `staging` (deploys staging) or `main` (deploys production) | CI; database migrations; builds the student, faculty, admin, and worker images; updates each Lambda function; smoke-tests each one and rolls it back to its previous image if the test fails |
| `e2e.yml` | manual only (Actions tab), paused until the Bruno collection stops using the removed `/auth/register` | starts Docker, Postgres, and the API, seeds accounts, runs the Bruno suite |
| `pr-conventions.yml` | every PR | Conventional Commits title; branch name; base branch (story -> FR branch -> `dev`, fixes back into the branch they came from, `dev` -> `staging` -> `main` for releases) |

A deploy that fails shows as a red run on the `staging` or `main` branch. The infrastructure it
deploys to is defined in `cbpupsis-infra`.

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

- Titles: `FR-00N: <module>` for functional requirements, `US-<MODULE>-<NNN>: <capability>` for
  stories, `[API] US-...` and `[WEB] US-...` for the backend and frontend halves of a story,
  `Chore: <verb> <thing>` for setup and cleanup work. There are no decision issues: pick the
  answer and write it into the task.
- Every issue starts with an **In short** box: one sentence, who builds it, the sprint, what to
  finish first, and the branch with where it merges, for example
  `` **Branch:** `api-us-enrl-001` from `fr003` · **Merge into:** `fr003` ``.
- Labels: one `type:`, one `module:`, and one MoSCoW label (`must-have`, `should-have`,
  `could-have`, `wont-have`).
- Stories describe what a user can observe and carry Given, When, Then acceptance criteria with
  ids (`AC-STU-001.1`) that tests reference.
- Every `[API]` task names the permission each endpoint requires. Sample JSON in an issue is an
  illustration, never a default.
- Attach every story and task to its parent as a **sub-issue**, and add it to the
  [CBPUPSIS board](https://github.com/orgs/pup-cs-projects/projects/4).
- Write plainly. Do not use em dashes.

## 9. Definition of ready and done

**Ready to start** when the issue has:

- [ ] A parent issue, and `type:`, `module:`, and MoSCoW labels
- [ ] Acceptance criteria that can fail, with ids for stories
- [ ] An SRS reference
- [ ] No open blocker (no `status: blocked`)

**Done** when:

- [ ] Each acceptance criterion is covered by a test that names its AC id
- [ ] CI is green on the PR
- [ ] Error codes, migrations, and docs are updated where the change needs them
- [ ] One reviewer approved and the PR is squash-merged into its FR branch
- [ ] The issue is closed after the merge

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| `Permission denied` on push | Accept the org invitation and confirm you have write access to the repo |
| Commit refused: "Don't commit to branch" | You are on `dev`, `staging`, `main`, or an FR branch. `git checkout -b api-us-<module>-<nnn>`, then commit |
| PR check "Branch name and base" is red | The branch name or the PR's base is wrong. A story branch targets its FR branch, an FR branch targets `dev`. Rename the branch or change the base in the PR |
| Commit refused by `conventional-pre-commit` | Rewrite the message as `type(scope): description` |
| Hook changed files and the commit stopped | `git add` those files and commit again |
| `make: command not found` on Windows | Use the plain commands shown next to each make target |
| An app container restarts in a loop | `docker compose -f docker/docker-compose.yml logs api-student` (or `api-faculty`, `api-admin`, `migrate`); usually a missing value in that app's `env/<app>/env.dev` |
| Tests pass locally but fail in CI | `gh run view <id> --log-failed`, then run the one failing test by node id |

Still stuck? Comment on your issue with the exact command and output, and mention the product
owner.
