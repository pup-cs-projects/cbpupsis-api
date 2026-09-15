"""Tests that the architectural rules hold in the code, not just in the docs.

docs/tech-book/CONVENTIONS.md states the layering rules as non-negotiable:
domains talk only through each other's ``service.py``, all SQL lives in
``repository.py``, ``service.py`` never touches the session directly. Those are
the rules that make a domain extractable into its own service later.

Prose does not hold that line. A ``from app.domains.users.models import User``
looks perfectly reasonable on the line where it sits; only the aggregate view
shows a boundary dissolving. The template carried exactly that import for a
while with a comment calling it "a boundary worth closing separately" — a
comment cannot fail CI, so it stayed.

These tests are the guard, in the same spirit as ``test_permission_naming.py``:
they fail at the moment a rule is broken rather than years later during an
extraction that turns out to be impossible.

Parsed with ``ast`` rather than matched with regex, so a name inside a string or
a comment cannot trigger a false failure, and an import split across lines
cannot hide from one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


def _app_dir() -> Path:
    """Locate ``app/`` by walking up until it is found.

    Resolved by search rather than by counting parents so that moving this file
    between test folders cannot silently point it at a directory that does not
    exist — every scan below would then walk zero files and pass vacuously.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "app"
        if candidate.is_dir():
            return candidate
    raise RuntimeError("could not locate the app/ package from this test file")


APP_DIR = _app_dir()
DOMAINS_DIR = APP_DIR / "domains"

#: A domain's public interface — what another domain may import from it.
#:
#: ``service`` is the contract; ``client`` is the thin seam that becomes an HTTP
#: call on extraction; ``schemas`` are the DTOs that cross the boundary;
#: ``constants``, ``dependencies``, and ``exceptions`` are shared declarations
#: with no state. Everything else — ``models``, ``repository`` — is private.
#:
#: ``exceptions`` is public because a caller has to be able to catch what a
#: domain raises; that is the whole point of each domain owning its own errors.
PUBLIC_MODULES = {
    "service",
    "client",
    "schemas",
    "constants",
    "dependencies",
    "exceptions",
}

#: Layer files that must never contain SQL construction.
NON_REPOSITORY_LAYERS = {"service.py", "router.py", "dependencies.py", "client.py"}


def domain_files() -> list[Path]:
    """Every Python file belonging to a domain."""
    return sorted(p for p in DOMAINS_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imported_modules(tree: ast.Module) -> list[tuple[str, int]]:
    """Return ``(dotted_module, lineno)`` for every import in the file.

    Covers both ``import a.b.c`` and ``from a.b import c``. Relative imports are
    skipped: the rules here are about crossing *domain* boundaries, and a
    relative import cannot leave its own package.
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            # `from app.domains.users import repository` — the imported name
            # matters as much as the module, since that is the private layer.
            found.extend(
                (f"{node.module}.{alias.name}", node.lineno) for alias in node.names
            )
            found.append((node.module, node.lineno))
    return found


def rel(path: Path) -> str:
    """A short, readable path for assertion messages."""
    return path.relative_to(APP_DIR.parent).as_posix()


class TestDomainBoundaries:
    """A domain is extractable only if nothing reaches past its public interface."""

    def test_no_domain_imports_another_domains_private_modules(self) -> None:
        """Cross-domain imports go through service/client/schemas, never models.

        This is the rule that makes a service split possible at all. Importing
        another domain's ``models`` couples you to its table definitions;
        importing its ``repository`` couples you to its SQL. Neither survives
        that domain moving behind an HTTP call, and both are invisible until
        the day someone tries.
        """
        violations: list[str] = []

        for path in domain_files():
            own_domain = path.relative_to(DOMAINS_DIR).parts[0]
            for module, lineno in imported_modules(parse(path)):
                parts = module.split(".")
                if parts[:2] != ["app", "domains"] or len(parts) < 4:
                    continue
                other_domain, member = parts[2], parts[3]
                if other_domain == own_domain or member in PUBLIC_MODULES:
                    continue
                violations.append(
                    f"{rel(path)}:{lineno} imports {other_domain}.{member} "
                    f"(private to the {other_domain} domain)"
                )

        assert not violations, (
            "Domains must talk through each other's service.py (or client.py), "
            "never by importing another domain's models or repository:\n  "
            + "\n  ".join(sorted(set(violations)))
        )

    def test_a_domain_never_imports_the_app_entrypoint(self) -> None:
        """No domain imports ``app.main``.

        A domain that reaches for the application object cannot be lifted out of
        it, and it makes the import graph circular the moment main.py mounts
        that domain's router.
        """
        violations = [
            f"{rel(path)}:{lineno}"
            for path in domain_files()
            for module, lineno in imported_modules(parse(path))
            if module == "app.main" or module.startswith("app.main.")
        ]
        assert not violations, "A domain must not import app.main:\n  " + "\n  ".join(
            violations
        )


class TestLayerSeparation:
    """All SQL in repository.py; services hold rules; routers stay thin."""

    def test_only_repositories_build_sql(self) -> None:
        """``select()``/``insert()``/``update()``/``delete()`` live in repository.py.

        A service that builds its own query has quietly taken on data-access
        responsibility, which is what makes the repository swappable and the
        service unit-testable without a database.
        """
        sql_builders = {"select", "insert", "update", "delete"}
        violations: list[str] = []

        for path in domain_files():
            if path.name not in NON_REPOSITORY_LAYERS:
                continue
            for node in ast.walk(parse(path)):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in sql_builders
                ):
                    violations.append(
                        f"{rel(path)}:{node.lineno} builds SQL with {node.func.id}()"
                    )

        assert not violations, "All SQL belongs in repository.py:\n  " + "\n  ".join(
            sorted(violations)
        )

    def test_services_never_touch_the_session_directly(self) -> None:
        """No ``db.execute`` / ``session.scalars`` outside the repository.

        The service layer coordinates; it does not run queries. Reaching for the
        session here is how data access leaks upward one call at a time.
        """
        session_methods = {"execute", "scalar", "scalars", "get"}
        violations: list[str] = []

        for path in domain_files():
            if path.name not in NON_REPOSITORY_LAYERS:
                continue
            for node in ast.walk(parse(path)):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in session_methods
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in {"db", "session"}
                ):
                    violations.append(
                        f"{rel(path)}:{node.lineno} calls "
                        f"{node.func.value.id}.{node.func.attr}()"
                    )

        assert not violations, (
            "Services must go through repository.py, not the session:\n  "
            + "\n  ".join(sorted(violations))
        )

    def test_routers_do_not_import_models(self) -> None:
        """Routers speak DTOs. An ORM object in a router is a leaked boundary.

        Returning a model from a route serialises whatever columns happen to
        exist — including ones added later that nobody meant to expose.
        """
        violations = [
            f"{rel(path)}:{lineno}"
            for path in domain_files()
            if path.name == "router.py"
            for module, lineno in imported_modules(parse(path))
            if module.split(".")[-1] == "models" or ".models." in module
        ]
        assert not violations, (
            "Routers must not import models; use schemas:\n  " + "\n  ".join(violations)
        )


class TestDataConventions:
    """Conventions whose violation is silent until it costs money or precision."""

    def test_money_columns_are_never_float(self) -> None:
        """Money is ``Numeric``/``Decimal``, never ``Float``.

        Binary floating point cannot represent 0.10 exactly. A ``Float`` price
        column passes every test with round numbers and loses fractions of a
        cent per row in production, which is the kind of bug found by an
        accountant rather than a test suite.
        """
        violations: list[str] = []

        for path in APP_DIR.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            for node in ast.walk(parse(path)):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr
                    if isinstance(func, ast.Attribute)
                    else None
                )
                if name == "Float":
                    violations.append(f"{rel(path)}:{node.lineno} uses Float(")

        assert not violations, (
            "Use Numeric/Decimal for money, never Float:\n  "
            + "\n  ".join(sorted(violations))
        )

    def test_every_domain_has_the_expected_layers(self) -> None:
        """Each domain carries the five layers, so its shape is predictable.

        Not every domain needs a ``client.py`` — only one another domain calls —
        so that file is not required here. The rest are: a domain missing
        ``repository.py`` usually means its SQL went into the service.
        """
        required = {
            "models.py",
            "schemas.py",
            "repository.py",
            "service.py",
            "router.py",
        }
        missing: dict[str, set[str]] = {}

        for domain in sorted(p for p in DOMAINS_DIR.iterdir() if p.is_dir()):
            if domain.name == "__pycache__":
                continue
            present = {f.name for f in domain.iterdir() if f.is_file()}
            absent = required - present
            if absent:
                missing[domain.name] = absent

        assert not missing, f"domains missing expected layers: {missing}"


def _sql_block(doc: str) -> str | None:
    """Return the text of the docstring's SQL literal block, or ``None``.

    Matched on the ``::`` that opens a reStructuredText literal block rather than
    on a fixed phrase. The lead-in varies for good reason: a query that runs here
    says ``SQL::``, while a staged write says "On the service's commit::" — and
    that sentence may wrap, so the marker is not always the word before it.
    Pinning an exact phrase makes the check fail on the functions whose SQL is
    least obvious, which are the ones it exists for.
    """
    if "::" not in doc:
        return None
    return doc.split("::", 1)[1]


class TestRepositoryDocstrings:
    """The repository layer is where all SQL lives, so the SQL is its contract."""

    def test_every_repository_function_documents_its_sql(self) -> None:
        """Each public repository function shows the SQL it runs.

        A reader should not have to mentally execute the query builder to learn
        what hits the database — especially where it is not obvious: a paginated
        ``list_*`` runs two statements, and a synchronous ``add_*``/``update_*``
        runs none at all until the service commits.

        Only public functions are required. A private helper composing a partial
        query has no complete statement to show, and demanding one would invite
        a fabricated snippet, which is worse than none.
        """
        missing: list[str] = []

        for path in sorted(DOMAINS_DIR.glob("*/repository.py")):
            for node in ast.walk(parse(path)):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name.startswith("_"):
                    continue
                doc = ast.get_docstring(node) or ""
                if _sql_block(doc) is None:
                    missing.append(f"{rel(path)}:{node.lineno} {node.name}()")

        assert not missing, (
            "Every public repository function needs a `SQL::` block showing the "
            "statement it runs (or, for a staged write, the one the commit "
            "emits):\n  " + "\n  ".join(missing)
        )

    def test_the_sql_blocks_are_not_empty(self) -> None:
        """A ``SQL::`` heading with nothing under it is worse than no heading.

        It reads as documented while telling the reader nothing, and it passes
        the check above. Require an actual statement keyword after the marker.
        """
        empty: list[str] = []
        keywords = ("SELECT", "INSERT", "UPDATE", "DELETE")

        for path in sorted(DOMAINS_DIR.glob("*/repository.py")):
            for node in ast.walk(parse(path)):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name.startswith("_"):
                    continue
                doc = ast.get_docstring(node) or ""
                after = _sql_block(doc)
                if after is None:
                    continue  # reported by the test above
                if not any(k in after.upper() for k in keywords):
                    empty.append(f"{rel(path)}:{node.lineno} {node.name}()")

        assert not empty, (
            "A `SQL::` block must contain an actual statement:\n  " + "\n  ".join(empty)
        )


class TestTheGuardItself:
    """A test that cannot fail is worse than no test. Prove these can."""

    @pytest.mark.parametrize(
        ("source", "checker"),
        [
            (
                "from app.domains.users.models import User",
                "cross-domain private import",
            ),
            ("rows = db.execute(query)", "session use in a service"),
            ("stmt = select(Item)", "SQL built outside the repository"),
        ],
    )
    def test_the_detectors_recognise_a_violation(
        self, source: str, checker: str
    ) -> None:
        """Each rule above is backed by a detector that fires on a known-bad line.

        The scans walk real files, so if a path ever resolves wrongly they would
        silently examine nothing and pass. These synthetic snippets prove the
        detection logic itself works, independent of what the codebase contains.
        """
        tree = ast.parse(source)

        if checker == "cross-domain private import":
            modules = [m for m, _ in imported_modules(tree)]
            assert any(
                m.split(".")[3] not in PUBLIC_MODULES
                for m in modules
                if m.startswith("app.domains.") and len(m.split(".")) >= 4
            )
        elif checker == "session use in a service":
            assert any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "execute"
                for n in ast.walk(tree)
            )
        else:
            assert any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "select"
                for n in ast.walk(tree)
            )

    def test_the_scan_actually_finds_files(self) -> None:
        """The domain scan walks a non-empty set of real files.

        Guards the failure mode that made this file necessary: a path resolved
        one directory too high yields no files, every scan above passes, and the
        architecture goes unguarded while the suite reports green.
        """
        files = domain_files()
        assert len(files) > 10, f"expected to scan the domains, found {len(files)}"
        assert any(f.name == "service.py" for f in files)
