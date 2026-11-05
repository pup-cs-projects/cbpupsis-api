from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cbpupsis_shared.domains.enrollment.schemas import (
    AvailableCourseRead,
    EnrollmentEligibilityRead,
    EnrollmentSubmitRead,
    OverloadRequestFiledRead,
)


class EnrollmentSubmitRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    term: str = Field(min_length=3)
    section_ids: list[uuid.UUID] = Field(
        min_length=1, serialization_alias="sectionIds", validation_alias="sectionIds"
    )
    confirmation: Literal[True]

    @model_validator(mode="after")
    def section_ids_are_unique(self) -> EnrollmentSubmitRequest:
        if len(set(self.section_ids)) != len(self.section_ids):
            raise ValueError("sectionIds must not contain duplicates")
        return self


class OverloadRequestCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    term: str = Field(min_length=3)
    requested_units: int = Field(
        gt=0, serialization_alias="requestedUnits", validation_alias="requestedUnits"
    )
    justification: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def justification_is_not_blank(self) -> OverloadRequestCreate:
        self.justification = self.justification.strip()
        if not self.justification:
            raise ValueError("justification must not be blank")
        return self


__all__ = [
    "AvailableCourseRead",
    "EnrollmentEligibilityRead",
    "EnrollmentSubmitRead",
    "EnrollmentSubmitRequest",
    "OverloadRequestCreate",
    "OverloadRequestFiledRead",
]
