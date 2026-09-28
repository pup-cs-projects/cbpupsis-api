"""Pydantic schemas (DTOs) for the items domain.

Split by direction: ``Base`` holds shared fields, ``Create`` carries input,
``Update`` carries partial edits, and the two ``Read`` shapes separate the
lightweight list view from the full detail view.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from cbpupsis_api_student.domains.items.constants import (
    DESCRIPTION_MAX_LENGTH,
    NAME_MAX_LENGTH,
    PRICE_PRECISION,
    PRICE_SCALE,
)


class ItemBase(BaseModel):
    """Fields shared by item input and output."""

    name: str = Field(min_length=1, max_length=NAME_MAX_LENGTH, examples=["Widget"])
    price: Decimal = Field(
        gt=0,
        max_digits=PRICE_PRECISION,
        decimal_places=PRICE_SCALE,
        examples=["19.99"],
    )


class ItemCreate(ItemBase):
    """Payload for creating an item.

    The owner is taken from the authentication context, never the body — a
    client must not be able to create records on someone else's behalf.
    """

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)


class ItemUpdate(BaseModel):
    """Partial update: every field optional so PATCH may send a subset."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX_LENGTH)
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)
    price: Decimal | None = Field(
        default=None,
        gt=0,
        max_digits=PRICE_PRECISION,
        decimal_places=PRICE_SCALE,
    )


class ItemRead(ItemBase):
    """Lightweight shape for list endpoints — only what a row or card shows."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID
    created_at: datetime


class ItemReadDetail(ItemRead):
    """Full shape for a single item; inherits the list fields and adds the rest."""

    description: str | None
    updated_at: datetime
