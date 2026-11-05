"""Domain exceptions for catalog, curriculum versions, and course prerequisites."""

from __future__ import annotations

from collections.abc import Sequence

from cbpupsis_core.exceptions import AppError, ConflictError, ValidationError


class CatalogError(AppError):
    """Base exception for catalog and curriculum management errors."""


class PrerequisiteCycleError(CatalogError, ValidationError):
    """A prerequisite or co-requisite chain forms a cycle (HTTP 422)."""

    code = "CATALOG_PREREQUISITE_CYCLE"

    def __init__(self, cycle: Sequence[str]) -> None:
        cycle_list = list(cycle)
        super().__init__(
            "Prerequisite or co-requisite configuration forms a cycle.",
            response_fields={"details": {"cycle": cycle_list}},
        )
        self.cycle = cycle_list


class CurriculumVersionInUseError(CatalogError, ConflictError):
    """Cannot delete a curriculum version while students are bound to it (HTTP 409)."""

    code = "CATALOG_VERSION_IN_USE"

    def __init__(self, bound_students: int = 0) -> None:
        super().__init__(
            "Cannot delete a curriculum version while students are bound to it.",
            response_fields=(
                {"details": {"boundStudents": bound_students}}
                if bound_students
                else None
            ),
        )
        self.bound_students = bound_students
