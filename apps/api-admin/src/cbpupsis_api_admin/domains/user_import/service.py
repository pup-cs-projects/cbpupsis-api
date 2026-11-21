"""Rules for importing accounts from a CSV file (FR19, UC-ADM-002).

The file is checked whole before anything is written: its shape, every row's
fields (through ``AccountCreate``, the schema ``POST /users`` uses), duplicates
within the file, and collisions with existing accounts. One problem refuses the
whole file. A clean file is created in one transaction by the auth client.

Problems name a row, a column, and a rule, never a cell's value.
"""

from __future__ import annotations

import csv
import io
import uuid

from pydantic import ValidationError as SchemaValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_admin.domains.user_import.constants import CSV_COLUMNS, HEADER_ROW
from cbpupsis_api_admin.domains.user_import.exceptions import (
    UserImportInvalidError,
    UserImportTooLargeError,
)
from cbpupsis_api_admin.domains.user_import.schemas import (
    ImportProblem,
    UserImportResult,
)
from cbpupsis_core.config import settings
from cbpupsis_shared.domains.auth import client as auth_client
from cbpupsis_shared.domains.users import client as users_client
from cbpupsis_shared.domains.users.schemas import AccountCreate

#: Wording for the schema's built-in error types. Built-in messages can quote
#: the input, so only these fixed texts (and the schema's own custom messages)
#: ever reach the response.
RULES: dict[str, str] = {
    "missing": "required",
    "string_too_short": "required",
    "string_too_long": "too long (at most {max_length} characters)",
    "enum": "must be one of: {expected}",
    "date_parsing": "must be a date in YYYY-MM-DD form",
    "date_from_datetime_parsing": "must be a date in YYYY-MM-DD form",
    "date_from_datetime_inexact": "must be a date in YYYY-MM-DD form",
    "int_parsing": "must be a whole number",
    "int_from_float": "must be a whole number",
    "greater_than_equal": "must be at least {ge}",
    "less_than_equal": "must be at most {le}",
}
CUSTOM_RULES = {"institutional_id_format", "birthdate_not_past"}


async def import_accounts(
    db: AsyncSession, *, actor_id: uuid.UUID, content: bytes
) -> UserImportResult:
    """Check the whole file, then create every row or none of them."""
    if len(content) > settings.user_import_max_bytes:
        raise UserImportTooLargeError(settings.user_import_max_bytes)
    accounts, rows, problems = _parse(content)
    if accounts:
        problems += [
            ImportProblem(row=rows[p.index], column=p.column, rule=p.rule)
            for p in await users_client.find_account_problems(db, accounts)
        ]
    if problems:
        problems.sort(key=lambda p: (p.row or 0, p.column or ""))
        raise UserImportInvalidError(problems)
    user_ids = await auth_client.create_accounts(
        db, actor_id=actor_id, accounts=accounts
    )
    return UserImportResult(created=len(user_ids), user_ids=user_ids)


def _file_problem(rule: str) -> UserImportInvalidError:
    return UserImportInvalidError([ImportProblem(row=None, column=None, rule=rule)])


def _parse(
    content: bytes,
) -> tuple[list[AccountCreate], list[int], list[ImportProblem]]:
    """Return the valid accounts, their row numbers, and every row problem.

    Raises at once for a problem with the file itself: not UTF-8, no header,
    a wrong header, too many rows, or broken CSV quoting.
    """
    try:
        # utf-8-sig drops the byte-order mark spreadsheet exports prepend.
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _file_problem("the file is not UTF-8 text") from None
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = [name.strip().lower() for name in next(reader)]
    except StopIteration:
        raise _file_problem("the file is empty") from None
    except csv.Error:
        raise _file_problem("the header row is not valid CSV") from None
    _check_header(header)

    accounts: list[AccountCreate] = []
    rows: list[int] = []
    problems: list[ImportProblem] = []
    row = HEADER_ROW
    data_rows = 0
    try:
        for cells in reader:
            row += 1
            if not any(cell.strip() for cell in cells):
                continue
            data_rows += 1
            if data_rows > settings.user_import_max_rows:
                raise _file_problem(
                    f"more than {settings.user_import_max_rows} data rows"
                )
            if len(cells) != len(header):
                problems.append(
                    ImportProblem(
                        row=row,
                        column=None,
                        rule=f"has {len(cells)} cells; the header has {len(header)}",
                    )
                )
                continue
            values = {
                name: cell.strip()
                for name, cell in zip(header, cells, strict=True)
                if cell.strip()
            }
            try:
                accounts.append(AccountCreate.model_validate(values))
                rows.append(row)
            except SchemaValidationError as exc:
                problems += _row_problems(row, exc)
    except csv.Error:
        raise _file_problem(f"row {row + 1} is not valid CSV") from None
    if data_rows == 0:
        raise _file_problem("the file has no data rows")
    return accounts, rows, problems


def _check_header(header: list[str]) -> None:
    problems = [
        ImportProblem(row=HEADER_ROW, column=name, rule="missing from the header")
        for name in CSV_COLUMNS
        if name not in header
    ]
    seen: set[str] = set()
    for name in header:
        if name not in CSV_COLUMNS:
            problems.append(
                ImportProblem(row=HEADER_ROW, column=None, rule="unknown column")
            )
        elif name in seen:
            problems.append(
                ImportProblem(row=HEADER_ROW, column=name, rule="appears twice")
            )
        seen.add(name)
    if problems:
        raise UserImportInvalidError(problems)


def _row_problems(row: int, exc: SchemaValidationError) -> list[ImportProblem]:
    problems = []
    for error in exc.errors():
        kind = error["type"]
        if kind == "role_fields_missing":
            problems += [
                ImportProblem(row=row, column=name, rule="required for this role")
                for name in error["ctx"]["fields"].split(", ")
            ]
            continue
        column = str(error["loc"][0]) if error["loc"] else None
        if kind in CUSTOM_RULES:
            rule = error["msg"]
        elif kind == "value_error" and column == "email":
            rule = "not a valid email address"
        else:
            rule = RULES.get(kind, "invalid value").format(**error.get("ctx", {}))
        problems.append(ImportProblem(row=row, column=column, rule=rule))
    return problems
