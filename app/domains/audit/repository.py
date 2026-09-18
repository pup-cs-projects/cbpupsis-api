"""Data access for the audit domain. Append and read only, by design.

There is no ``update_*`` or ``delete_*`` here and there should never be one: an
audit trail the application can rewrite is not evidence. Retention is a
retention job's business (a scheduled bulk delete by age), not an operation the
API exposes.

Transactions belong to the service, as everywhere else in this codebase.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.audit.models import AuditEntry


def add_entry(
    db: AsyncSession,
    action: str,
    occurred_at: datetime,
    actor_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> AuditEntry:
    """Stage one audit entry on the session and return it.

    Synchronous because ``Session.add`` does not touch the database — the INSERT
    happens when the service commits.

    Emits no SQL here. On the service's commit::

        INSERT INTO audit_entries (action, actor_id, target_type, target_id,
                                   payload, occurred_at, id)
        VALUES (:action, :actor_id::UUID, :target_type, :target_id,
                :payload, :occurred_at, :id::UUID)
    """
    entry = AuditEntry(
        action=action,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        payload=payload or {},
        occurred_at=occurred_at,
    )
    db.add(entry)
    return entry


async def list_entries(
    db: AsyncSession,
    limit: int,
    offset: int,
    action: str | None = None,
    actor_id: uuid.UUID | None = None,
    target_id: str | None = None,
) -> tuple[list[AuditEntry], int]:
    """Return one page of audit entries, newest first, and the total count.

    Ordered by ``occurred_at`` with ``id`` as a unique tiebreaker: entries
    written in the same transaction can share a timestamp, and without the
    second key adjacent pages could repeat or skip one.

    Two statements. Each predicate below is present only when the corresponding
    argument is not ``None``; with no filters the query has no WHERE clause.

    SQL::

        -- 1. total matching rows, before limit/offset
        SELECT count(*) AS count_1
        FROM (SELECT audit_entries.action AS action,
                     audit_entries.actor_id AS actor_id,
                     audit_entries.target_type AS target_type,
                     audit_entries.target_id AS target_id,
                     audit_entries.payload AS payload,
                     audit_entries.occurred_at AS occurred_at,
                     audit_entries.id AS id
              FROM audit_entries
              WHERE audit_entries.action = :action_1
                AND audit_entries.actor_id = :actor_id_1::UUID
                AND audit_entries.target_id = :target_id_1) AS anon_1

        -- 2. the page itself
        SELECT audit_entries.action, audit_entries.actor_id,
               audit_entries.target_type, audit_entries.target_id,
               audit_entries.payload, audit_entries.occurred_at,
               audit_entries.id
        FROM audit_entries
        WHERE audit_entries.action = :action_1
          AND audit_entries.actor_id = :actor_id_1::UUID
          AND audit_entries.target_id = :target_id_1
        ORDER BY audit_entries.occurred_at DESC, audit_entries.id DESC
        LIMIT :param_1 OFFSET :param_2
    """
    query = select(AuditEntry)
    if action is not None:
        query = query.where(AuditEntry.action == action)
    if actor_id is not None:
        query = query.where(AuditEntry.actor_id == actor_id)
    if target_id is not None:
        query = query.where(AuditEntry.target_id == target_id)

    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    result = await db.execute(
        query.order_by(AuditEntry.occurred_at.desc(), AuditEntry.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all()), total or 0
