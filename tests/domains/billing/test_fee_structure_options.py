"""Fee structure options: terms and programs for the Finance form (AC-FIN-003).

- Finance gets 200 with every term (current first) and active programs by code.
- ``hasFeeStructure`` is true only for terms with a saved fee structure.
- Registrar, Dean, Chair sessions get 403.
- ``/options`` is its own route, never parsed as a ``/{term_id}``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.curriculum import Program
from tests.domains.billing.test_ac_fin_003 import (
    _make_admin,
    _make_program,
    _make_term,
)

URL = "/api/v1/admin/fee-structures/options"


@pytest.mark.asyncio
async def test_finance_gets_terms_and_programs(
    client: AsyncClient, db: AsyncSession
) -> None:
    _, headers = await _make_admin(db, email="opt1@example.com", position="finance")
    current = await _make_term(db)
    older = await _make_term(db)
    older.is_current = False
    older.start_date = date(2025, 8, 1)
    older.end_date = date(2025, 12, 15)
    await db.commit()
    bsit = await _make_program(db, code="OPTBSIT")
    bscs = await _make_program(db, code="OPTBSCS")
    db.add(
        Program(
            code="OPTOLD",
            name="Retired Program",
            department="Department of Computer and Information Sciences",
            total_units_required=Decimal("140.0"),
            is_active=False,
        )
    )
    await db.commit()

    saved = await client.put(
        f"/api/v1/admin/fee-structures/{current.id}",
        json={"tuitionPerUnit": "250.00"},
        headers=headers,
    )
    assert saved.status_code == 200, saved.text

    res = await client.get(URL, headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"terms", "programs"}

    terms = {t["id"]: t for t in body["terms"]}
    assert terms[str(current.id)] == {
        "id": str(current.id),
        "academicYear": current.academic_year,
        "semester": "FIRST_SEMESTER",
        "isCurrent": True,
        "hasFeeStructure": True,
    }
    assert terms[str(older.id)]["isCurrent"] is False
    assert terms[str(older.id)]["hasFeeStructure"] is False
    ids = [t["id"] for t in body["terms"]]
    assert ids.index(str(current.id)) < ids.index(str(older.id))

    codes = [p["code"] for p in body["programs"]]
    assert "OPTOLD" not in codes
    assert codes == sorted(codes)
    programs = {p["code"]: p for p in body["programs"]}
    assert programs["OPTBSCS"] == {
        "id": str(bscs.id),
        "code": "OPTBSCS",
        "name": bscs.name,
    }
    assert programs["OPTBSIT"]["id"] == str(bsit.id)


@pytest.mark.asyncio
async def test_non_finance_admins_get_403(
    client: AsyncClient, db: AsyncSession
) -> None:
    for position in ("registrar", "dean", "chairperson"):
        _, headers = await _make_admin(
            db, email=f"opt-{position}@example.com", position=position
        )
        res = await client.get(URL, headers=headers)
        assert res.status_code == 403, position


@pytest.mark.asyncio
async def test_options_route_not_shadowed_by_term_id(
    client: AsyncClient, db: AsyncSession
) -> None:
    _, headers = await _make_admin(db, email="opt3@example.com", position="finance")
    res = await client.get(URL, headers=headers)
    assert res.status_code == 200, res.text
    assert "terms" in res.json()
    assert "academicTermId" not in res.json()
