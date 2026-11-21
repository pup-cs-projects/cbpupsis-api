"""Shared pagination envelope.

Lives in the core package because every domain that lists anything returns one.
Anything used by exactly one domain belongs in that domain, not here.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Page[T](BaseModel):
    """One page of results plus the totals a client needs to render paging.

    Generic over the item type so ``Page[ItemRead]`` documents its element shape
    in OpenAPI, rather than degrading to an untyped list.
    """

    items: list[T]
    #: Total rows matching the query, ignoring limit/offset.
    total: int
    limit: int
    offset: int


class PaginationParams(BaseModel):
    """Query parameters for paginated endpoints.

    ``limit`` is capped so a client cannot request an unbounded page and turn a
    list endpoint into a denial-of-service vector.
    """

    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)
