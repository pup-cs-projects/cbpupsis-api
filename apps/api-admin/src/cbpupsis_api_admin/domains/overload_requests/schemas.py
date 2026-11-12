from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cbpupsis_shared.domains.enrollment.schemas import OverloadRequestRead


class OverloadDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["APPROVE", "REJECT"]
    remarks: str | None = Field(default=None, max_length=2000)

    @field_validator("remarks")
    @classmethod
    def trim_remarks(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


__all__ = ["OverloadDecisionRequest", "OverloadRequestRead"]
