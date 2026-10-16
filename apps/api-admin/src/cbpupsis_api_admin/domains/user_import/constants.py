"""Fixed values for the CSV account import."""

from __future__ import annotations

#: The header row every import file carries, in any order. Each name is a field
#: of ``AccountCreate``; a column a row's role does not use is left empty.
CSV_COLUMNS: tuple[str, ...] = (
    "first_name",
    "surname",
    "email",
    "role",
    "institutional_id",
    "birthdate",
    "department",
    "college",
    "program",
    "year_level",
)

#: Row numbers count the header as row 1, as a spreadsheet does.
HEADER_ROW = 1
