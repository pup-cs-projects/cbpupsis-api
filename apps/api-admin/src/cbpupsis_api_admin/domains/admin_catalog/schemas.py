"""Admin catalog HTTP schemas re-exported from curriculum and sections."""

from cbpupsis_shared.domains.curriculum.schemas import (
    CatalogCourseCreate,
    CatalogCourseRead,
    CatalogCourseUpdate,
    CurriculumVersionCreate,
    CurriculumVersionRead,
    StudentDegreeProgressRead,
    SubstitutionApprovalRead,
    SubstitutionApproveRequest,
)
from cbpupsis_shared.domains.sections.schemas import (
    AdminSectionCreate,
    AdminSectionRead,
    AdminSectionUpdate,
)

__all__ = [
    "AdminSectionCreate",
    "AdminSectionRead",
    "AdminSectionUpdate",
    "CatalogCourseCreate",
    "CatalogCourseRead",
    "CatalogCourseUpdate",
    "CurriculumVersionCreate",
    "CurriculumVersionRead",
    "StudentDegreeProgressRead",
    "SubstitutionApprovalRead",
    "SubstitutionApproveRequest",
]
