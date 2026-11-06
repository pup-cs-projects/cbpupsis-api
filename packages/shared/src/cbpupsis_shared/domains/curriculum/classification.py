"""Default PE and NSTP classification of a course from its code.

The academic checks refuse a student when a course has no classification, so
every write path must store one. ``scripts/seed_course_classifications.py``
overrides these defaults from a reviewed manifest; migration
``e099_academic_waivers`` applies the same patterns to existing rows.
"""

from __future__ import annotations

import re

_NOT_ALPHANUMERIC = re.compile(r"[^A-Z0-9]")
_NSTP = re.compile(r"^(?:NSTP|CWTS|LTS|ROTC)\d*$")
_PE = re.compile(r"^(?:PE|PED|PHED|PATHFIT)(?:10|0*)([1-4])$")


def classify_course_code(course_code: str) -> tuple[bool, int | None]:
    """Return ``(is_nstp, pe_sequence)`` for a course code."""
    normalized = _NOT_ALPHANUMERIC.sub("", course_code.upper())
    if _NSTP.match(normalized):
        return True, None
    pe = _PE.match(normalized)
    return False, int(pe.group(1)) if pe else None
