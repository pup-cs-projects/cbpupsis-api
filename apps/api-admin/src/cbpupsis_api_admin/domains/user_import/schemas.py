"""Pydantic schemas (DTOs) for the CSV account import."""

from __future__ import annotations

import uuid

from pydantic import BaseModel


class UserImportResult(BaseModel):
    """What a successful import created."""

    created: int
    user_ids: list[uuid.UUID]


class ImportProblem(BaseModel):
    """One reason the file was refused: where, and which rule.

    Never the cell's value: the file carries personal data, and the row and
    column are enough to find it.
    """

    #: ``None`` for a problem with the file as a whole.
    row: int | None
    column: str | None
    rule: str
