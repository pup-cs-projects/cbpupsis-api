"""Tests that the architectural rules hold in the code, not just in the docs.

docs/tech-book/CONVENTIONS.md states the workspace and layering rules as
non-negotiable. Prose does not hold that line: a
``from cbpupsis_database.models.users import User`` inside the IAM domain looks
perfectly reasonable on the line where it sits, and only the aggregate view shows
a boundary dissolving. These tests fail at the moment a rule is broken.

Two kinds of boundary are checked:

- **Workspace:** the kernel packages layer one way (core, then database, then
  shared), no app imports another app, the kernel never imports an app, and only
  the database package defines tables.
- **Domain:** each domain reaches another only through its public modules,
  imports only its own models module, and keeps SQL in ``repository.py``.

Parsed with ``ast`` rather than matched with regex, so a name inside a string or
a comment cannot trigger a false failure, and an import split across lines
cannot hide from one. Each detector is a function the self-tests at the bottom
feed a known-bad snippet, so a detector that silently stopped working fails too.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

from cbpupsis_database import models as _models  # noqa: F401
from cbpupsis_database.base import Base


def _repo_root() -> Path:
    """Locate the workspace root by searching upward for ``packages/`` and ``apps/``.

    Resolved by search rather than by counting parents, so moving this file
    cannot point every scan at a missing directory and let them pass vacuously.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "packages").is_dir() and (parent / "apps").is_dir():
            return parent
    raise RuntimeError("could not locate the workspace root from this test file")


ROOT = _repo_root()


def _source_packages(base: Path) -> dict[str, Path]:
    """Map each importable package under ``base/*/src/`` to its directory."""
    return {
        pkg.name: pkg
        for pkg in sorted(base.glob("*/src/*"))
        if (pkg / "__init__.py").is_file()
    }


KERNEL = _source_packages(ROOT / "packages")
APPS = _source_packages(ROOT / "apps")
MODELS_DIR = KERNEL["cbpupsis_database"] / "models"

#: Which kernel packages each kernel package may import. One direction only:
#: core is the leaf, so the database can read settings from it, and shared sits
#: on both. A cycle here would make the packages impossible to build apart.
KERNEL_DEPENDENCIES: dict[str, set[str]] = {
    "cbpupsis_core": set(),
    "cbpupsis_database": {"cbpupsis_core"},
    "cbpupsis_shared": {"cbpupsis_core", "cbpupsis_database"},
}

#: A domain's public interface: what another domain may import from it.
#: ``models`` and ``repository`` are private. ``exceptions`` is public because a
#: caller has to be able to catch what a domain raises.
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

#: Names that declare a table. An app or a non-database package importing one
#: is about to define a model outside the kernel.
TABLE_DECLARING_NAMES = {
    "DeclarativeBase",
    "declarative_base",
    "mapped_column",
    "registry",
    "relationship",
    "Table",
}


def python_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*.py") if "__pycache__" not in p.parts)


def domain_dirs() -> list[Path]:
    """Every domain folder, in the shared package and in every app."""
    return sorted(
        d
        for pkg in (*KERNEL.values(), *APPS.values())
        if (pkg / "domains").is_dir()
        for d in (pkg / "domains").iterdir()
        if d.is_dir() and d.name != "__pycache__"
    )


def domain_files() -> list[Path]:
    return [f for d in domain_dirs() for f in python_files(d)]


def domain_of(path: Path) -> str:
    parts = path.parts
    return parts[parts.index("domains") + 1]


def package_of(path: Path) -> str:
    parts = path.parts
    return parts[parts.index("src") + 1]


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def imported_modules(tree: ast.Module) -> list[tuple[str, int]]:
    """Return ``(dotted_module, lineno)`` for every absolute import in the file.

    ``from a.b import c`` yields both ``a.b`` and ``a.b.c``, since the imported
    name may itself be the private module (``from x.domains.users import
    repository``). Relative imports cannot leave their own package and are
    skipped.
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.extend(
                (f"{node.module}.{alias.name}", node.lineno) for alias in node.names
            )
            found.append((node.module, node.lineno))
    return found


def cross_domain_private_imports(tree: ast.Module, own: str) -> list[str]:
    """Imports of another domain's private module (``repository`` and the like)."""
    violations = []
    for module, lineno in imported_modules(tree):
        parts = module.split(".")
        if len(parts) < 4 or parts[1] != "domains":
            continue
        other, member = parts[2], parts[3]
        if other != own and member not in PUBLIC_MODULES:
            violations.append(f"{lineno}: imports {other}.{member}")
    return violations


def foreign_model_imports(tree: ast.Module, own: str) -> list[str]:
    """Imports of any models module but the domain's own, or of all of them."""
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module in {"cbpupsis_database", "cbpupsis_database.models"}:
                targets = [f"{node.module}.{alias.name}" for alias in node.names]
            else:
                targets = [node.module]
        else:
            continue
        for target in targets:
            parts = target.split(".")
            if parts[:2] != ["cbpupsis_database", "models"]:
                continue
            owner = parts[2] if len(parts) > 2 else "every domain's"
            if owner != own:
                violations.append(f"{node.lineno}: imports the {owner} models")
    return violations


def entry_point_imports(tree: ast.Module) -> list[str]:
    """Imports of anything that assembles an application."""
    violations = []
    for module, lineno in imported_modules(tree):
        parts = module.split(".")
        if parts[0] == "main" or (
            len(parts) >= 2
            and parts[0].startswith("cbpupsis_")
            and parts[1] in {"main", "routes", "application"}
        ):
            violations.append(f"{lineno}: imports {module}")
    return violations


def workspace_imports(tree: ast.Module) -> list[tuple[str, int]]:
    """Return ``(top_level_package, lineno)`` for each import of workspace code."""
    found = []
    for module, lineno in imported_modules(tree):
        top = module.split(".")[0]
        if top in KERNEL or top in APPS or top == "main":
            found.append((top, lineno))
    return found


def table_definitions(tree: ast.Module) -> list[str]:
    """Places a file declares a table: a mapped class, or the tools to make one."""
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    targets = stmt.targets
                elif isinstance(stmt, ast.AnnAssign):
                    targets = [stmt.target]
                else:
                    continue
                names = {t.id for t in targets if isinstance(t, ast.Name)}
                if names & {"__tablename__", "__table__"}:
                    violations.append(f"{node.lineno}: class {node.name} maps a table")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "cbpupsis_database.base":
                violations.append(f"{node.lineno}: imports the declarative base")
            elif node.module.startswith("sqlalchemy"):
                declared = {a.name for a in node.names} & TABLE_DECLARING_NAMES
                if declared:
                    violations.append(f"{node.lineno}: imports {sorted(declared)}")
    return violations


def sql_builder_calls(tree: ast.Module) -> list[str]:
    return [
        f"{node.lineno}: builds SQL with {node.func.id}()"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"select", "insert", "update", "delete"}
    ]


def session_calls(tree: ast.Module) -> list[str]:
    return [
        f"{node.lineno}: calls {node.func.value.id}.{node.func.attr}()"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"execute", "scalar", "scalars", "get"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"db", "session"}
    ]


def _report(violations: list[str], message: str) -> None:
    assert not violations, message + ":\n  " + "\n  ".join(sorted(set(violations)))


class TestWorkspaceBoundaries:
    """Each app stays buildable on its own, over a kernel it does not reach into."""

    def test_an_app_never_imports_another_app(self) -> None:
        """An app that imports another cannot be built or deployed without it."""
        violations = [
            f"{rel(path)}:{lineno} imports {top}"
            for name, pkg in APPS.items()
            for path in python_files(pkg)
            for top, lineno in workspace_imports(parse(path))
            if (top in APPS and top != name) or top == "main"
        ]
        _report(violations, "An app must not import another app or the composer")

    def test_the_kernel_never_imports_an_app(self) -> None:
        """The kernel is what apps share; importing one app would ship it in all."""
        violations = [
            f"{rel(path)}:{lineno} imports {top}"
            for path in python_files(ROOT / "packages")
            for top, lineno in workspace_imports(parse(path))
            if top in APPS or top == "main"
        ]
        _report(violations, "packages/ must not import from apps/ or main.py")

    def test_the_kernel_packages_depend_in_one_direction(self) -> None:
        """core, then database, then shared: never back down the chain."""
        violations = [
            f"{rel(path)}:{lineno} imports {top}"
            for name, allowed in KERNEL_DEPENDENCIES.items()
            for path in python_files(KERNEL[name])
            for top, lineno in workspace_imports(parse(path))
            if top != name and top not in allowed
        ]
        _report(violations, "A kernel package imported one it must not depend on")

    def test_every_kernel_package_has_a_declared_direction(self) -> None:
        """A new kernel package must be placed in the dependency order above."""
        assert set(KERNEL) == set(KERNEL_DEPENDENCIES)

    def test_only_the_database_package_defines_tables(self) -> None:
        """Apps and the other kernel packages never declare a table."""
        database = KERNEL["cbpupsis_database"]
        violations = [
            f"{rel(path)}:{v}"
            for pkg in (*KERNEL.values(), *APPS.values())
            if pkg != database
            for path in python_files(pkg)
            for v in table_definitions(parse(path))
        ]
        _report(violations, "Only packages/database may define tables")

    def test_every_mapped_class_is_in_the_models_package(self) -> None:
        """The runtime view of the rule above: no mapper registered from elsewhere."""
        stray = [
            f"{m.class_.__module__}.{m.class_.__name__}"
            for m in Base.registry.mappers
            if not m.class_.__module__.startswith("cbpupsis_database.models.")
        ]
        assert not stray, f"mapped outside cbpupsis_database.models: {stray}"

    def test_no_table_is_mapped_twice(self) -> None:
        counts = Counter(m.local_table.name for m in Base.registry.mappers)
        twice = sorted(name for name, n in counts.items() if n > 1)
        assert not twice, f"tables mapped by more than one class: {twice}"

    def test_the_models_package_imports_every_model_module(self) -> None:
        """A module missing here is a table autogenerate silently skips."""
        tree = parse(MODELS_DIR / "__init__.py")
        registered = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "cbpupsis_database.models"
            for alias in node.names
        }
        on_disk = {p.stem for p in MODELS_DIR.glob("*.py") if p.stem != "__init__"}
        assert registered == on_disk


class TestDomainBoundaries:
    """A domain is extractable only if nothing reaches past its public interface."""

    def test_domain_names_are_unique_across_the_workspace(self) -> None:
        """A domain's models module is named for it, so two cannot share a name."""
        counts = Counter(d.name for d in domain_dirs())
        assert not [n for n, c in counts.items() if c > 1], counts

    def test_no_domain_imports_another_domains_private_modules(self) -> None:
        """Cross-domain imports go through service/client/schemas, never repository."""
        violations = [
            f"{rel(path)}:{v}"
            for path in domain_files()
            for v in cross_domain_private_imports(parse(path), domain_of(path))
        ]
        _report(
            violations,
            "Domains must talk through each other's service.py (or client.py)",
        )

    def test_a_domain_imports_only_its_own_models(self) -> None:
        """Centralising the tables does not make another domain's tables public."""
        violations = [
            f"{rel(path)}:{v}"
            for path in domain_files()
            for v in foreign_model_imports(parse(path), domain_of(path))
        ]
        _report(violations, "A domain may import only cbpupsis_database.models.<own>")

    def test_a_domain_never_imports_an_entry_point(self) -> None:
        """A domain that reaches for an application object cannot be lifted out."""
        violations = [
            f"{rel(path)}:{v}"
            for path in domain_files()
            for v in entry_point_imports(parse(path))
        ]
        _report(violations, "A domain must not import main, routes, or the factory")

    def test_every_domain_has_the_expected_layers(self) -> None:
        """Four layers in the domain folder, plus its models module in the kernel.

        ``client.py`` is not required: only a domain another domain calls needs one.
        """
        required = {"schemas.py", "repository.py", "service.py", "router.py"}
        missing: dict[str, set[str]] = {}
        for domain in domain_dirs():
            absent = required - {f.name for f in domain.iterdir() if f.is_file()}
            if not (MODELS_DIR / f"{domain.name}.py").is_file():
                absent.add(f"cbpupsis_database/models/{domain.name}.py")
            if absent:
                missing[domain.name] = absent
        assert not missing, f"domains missing expected layers: {missing}"


class TestLayerSeparation:
    """All SQL in repository.py; services hold rules; routers stay thin."""

    def test_only_repositories_build_sql(self) -> None:
        violations = [
            f"{rel(path)}:{v}"
            for path in domain_files()
            if path.name in NON_REPOSITORY_LAYERS
            for v in sql_builder_calls(parse(path))
        ]
        _report(violations, "All SQL belongs in repository.py")

    def test_services_never_touch_the_session_directly(self) -> None:
        violations = [
            f"{rel(path)}:{v}"
            for path in domain_files()
            if path.name in NON_REPOSITORY_LAYERS
            for v in session_calls(parse(path))
        ]
        _report(violations, "Services must go through repository.py, not the session")

    def test_routers_do_not_import_models(self) -> None:
        """Routers speak DTOs. Returning a model serialises whatever columns exist."""
        violations = [
            f"{rel(path)}:{lineno}"
            for path in domain_files()
            if path.name == "router.py"
            for module, lineno in imported_modules(parse(path))
            if module.split(".")[-1] == "models" or ".models." in module
        ]
        _report(violations, "Routers must not import models; use schemas")


class TestDataConventions:
    """Conventions whose violation is silent until it costs money or precision."""

    def test_money_columns_are_never_float(self) -> None:
        """Binary floating point cannot represent 0.10 exactly; use Numeric."""
        violations = []
        for pkg in (*KERNEL.values(), *APPS.values()):
            for path in python_files(pkg):
                for node in ast.walk(parse(path)):
                    if not isinstance(node, ast.Call):
                        continue
                    func = node.func
                    name = getattr(func, "id", None) or getattr(func, "attr", None)
                    if name == "Float":
                        violations.append(f"{rel(path)}:{node.lineno} uses Float(")
        _report(violations, "Use Numeric/Decimal for money, never Float")


def _sql_block(doc: str) -> str | None:
    """Return the text after the docstring's ``::`` literal-block marker, or None.

    Matched on ``::`` rather than a fixed phrase: a query says ``SQL::`` while a
    staged write says "On the service's commit::", and that sentence may wrap.
    """
    if "::" not in doc:
        return None
    return doc.split("::", 1)[1]


def _public_repository_functions() -> list[tuple[Path, ast.AST]]:
    return [
        (path, node)
        for d in domain_dirs()
        if (path := d / "repository.py").is_file()
        for node in ast.walk(parse(path))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and not node.name.startswith("_")
    ]


class TestRepositoryDocstrings:
    """The repository layer is where all SQL lives, so the SQL is its contract."""

    def test_every_repository_function_documents_its_sql(self) -> None:
        """Each public repository function shows the SQL it runs.

        Only public functions: a private helper composing a partial query has no
        complete statement to show, and demanding one invites a fabricated one.
        """
        missing = [
            f"{rel(path)}:{node.lineno} {node.name}()"
            for path, node in _public_repository_functions()
            if _sql_block(ast.get_docstring(node) or "") is None
        ]
        _report(
            missing,
            "Every public repository function needs a `SQL::` block showing the "
            "statement it runs (or, for a staged write, the one the commit emits)",
        )

    def test_the_sql_blocks_are_not_empty(self) -> None:
        """A ``SQL::`` heading with nothing under it reads as documented and is not."""
        keywords = ("SELECT", "INSERT", "UPDATE", "DELETE")
        empty = [
            f"{rel(path)}:{node.lineno} {node.name}()"
            for path, node in _public_repository_functions()
            if (after := _sql_block(ast.get_docstring(node) or "")) is not None
            and not any(k in after.upper() for k in keywords)
        ]
        _report(empty, "A `SQL::` block must contain an actual statement")


class TestTheGuardItself:
    """A test that cannot fail is worse than no test. Prove these can."""

    @pytest.mark.parametrize(
        ("source", "detector"),
        [
            (
                "from cbpupsis_shared.domains.users.repository import get_user",
                lambda t: cross_domain_private_imports(t, own="iam"),
            ),
            (
                "from cbpupsis_database.models.users import User",
                lambda t: foreign_model_imports(t, own="iam"),
            ),
            (
                "from cbpupsis_database.models import users",
                lambda t: foreign_model_imports(t, own="iam"),
            ),
            (
                "import cbpupsis_database.models",
                lambda t: foreign_model_imports(t, own="iam"),
            ),
            ("from main import app", entry_point_imports),
            ("from cbpupsis_api_admin.routes import MOUNTS", entry_point_imports),
            (
                "class Course(Base):\n    __tablename__ = 'courses'",
                table_definitions,
            ),
            ("from sqlalchemy.orm import mapped_column", table_definitions),
            ("from cbpupsis_database.base import Base", table_definitions),
            ("rows = db.execute(query)", session_calls),
            ("stmt = select(Item)", sql_builder_calls),
        ],
    )
    def test_each_detector_fires_on_a_known_violation(
        self, source: str, detector
    ) -> None:
        """The scans walk real files; these snippets prove the detection works."""
        assert detector(ast.parse(source))

    def test_own_models_and_public_modules_are_allowed(self) -> None:
        """The detectors must not fire on the imports the rules permit."""
        tree = ast.parse(
            "from cbpupsis_database.models.iam import Group\n"
            "from cbpupsis_shared.domains.users import service\n"
            "from cbpupsis_shared.domains.users.constants import MANAGE_USER\n"
        )
        assert not foreign_model_imports(tree, own="iam")
        assert not cross_domain_private_imports(tree, own="iam")

    def test_an_app_importing_another_app_is_seen(self) -> None:
        tree = ast.parse("from cbpupsis_api_student.domains.items import service")
        assert ("cbpupsis_api_student", 1) in workspace_imports(tree)

    def test_the_scans_actually_find_files(self) -> None:
        """A path resolved one level too high would scan nothing and pass."""
        assert set(KERNEL) >= {"cbpupsis_core", "cbpupsis_database", "cbpupsis_shared"}
        assert set(APPS) == {
            "cbpupsis_api_student",
            "cbpupsis_api_faculty",
            "cbpupsis_api_admin",
        }
        files = domain_files()
        assert len(files) > 10, f"expected to scan the domains, found {len(files)}"
        assert {package_of(f) for f in files} >= {
            "cbpupsis_shared",
            "cbpupsis_api_student",
        }
        assert any(f.name == "service.py" for f in files)
        assert len(list(MODELS_DIR.glob("*.py"))) > 5
        assert len(Base.registry.mappers) > 5
