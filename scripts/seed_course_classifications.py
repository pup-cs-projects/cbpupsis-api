"""Load a catalogue-owner-reviewed JSON course classification manifest.

Dry run by default. Use --apply only after owner review and migration to e099.
No prefixes, title heuristics, or implicit defaults classify a course.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.curriculum import Course
from cbpupsis_database.session import AsyncSessionLocal


class CourseClassification(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    course_code: Annotated[str, StringConstraints(min_length=1, max_length=20)]
    is_nstp: bool
    pe_sequence: Annotated[int, Field(ge=1, le=4)] | None


MANIFEST = TypeAdapter(Annotated[list[CourseClassification], Field(min_length=1)])


async def stage_classifications(
    db: AsyncSession, entries: list[CourseClassification], *, apply: bool
) -> int:
    """Validate the entire explicit batch before staging any changes."""
    by_code = {row.course_code: row for row in entries}
    if len(by_code) != len(entries):
        raise ValueError("The manifest contains duplicate course codes.")
    if any(row.is_nstp and row.pe_sequence is not None for row in entries):
        raise ValueError("A course cannot be both NSTP and PE.")
    query = select(Course).where(Course.course_code.in_(by_code))
    if apply:
        query = query.with_for_update()
    rows = list(await db.scalars(query))
    unknown = set(by_code).difference(row.course_code for row in rows)
    if unknown:
        raise ValueError(f"Unknown course codes: {', '.join(sorted(unknown))}")
    changed = 0
    for course in rows:
        classification = by_code[course.course_code]
        if (course.is_nstp, course.pe_sequence) != (
            classification.is_nstp,
            classification.pe_sequence,
        ):
            changed += 1
            if apply:
                course.is_nstp = classification.is_nstp
                course.pe_sequence = classification.pe_sequence
    return changed


async def seed(entries: list[CourseClassification], *, apply: bool) -> None:
    async with AsyncSessionLocal() as db:
        changed = await stage_classifications(db, entries, apply=apply)
        if apply:
            await db.commit()
    action = "updated" if apply else "would update"
    print(f"Validated {len(entries)} course codes; {action} {changed}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    entries = MANIFEST.validate_json(args.manifest.read_bytes())
    asyncio.run(seed(entries, apply=args.apply))


if __name__ == "__main__":
    main()
