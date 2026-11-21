"""SQLAlchemy models for the items domain.

``items`` is the reference domain: a deliberately generic owned resource that
demonstrates the full shape without carrying business meaning. Rename it (or
copy it) for the first real feature.

``owner_id`` references a user but carries no hard foreign key: a database-level
constraint across domains is a coupling a service split cannot sever. Integrity
is enforced in the service layer via ``cbpupsis_shared.domains.users.client``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from cbpupsis_database.base import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin

NAME_MAX_LENGTH = 200
DESCRIPTION_MAX_LENGTH = 2000
#: Numeric(12, 2): money is exact, never float.
PRICE_PRECISION = 12
PRICE_SCALE = 2


class Item(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """An item owned by a user."""

    __tablename__ = "items"

    #: Owning user's id. No cross-domain FK by design (see module docstring).
    owner_id: Mapped[uuid.UUID] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), index=True)
    description: Mapped[str | None] = mapped_column(
        String(DESCRIPTION_MAX_LENGTH), default=None
    )
    #: Money as exact NUMERIC mapped to Decimal — never float, whose binary
    #: representation cannot hold values like 0.10 exactly and accumulates
    #: rounding errors across arithmetic.
    price: Mapped[Decimal] = mapped_column(Numeric(PRICE_PRECISION, PRICE_SCALE))
