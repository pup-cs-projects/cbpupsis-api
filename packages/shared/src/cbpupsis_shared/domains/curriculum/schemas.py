"""Pydantic schemas (DTOs) for the curriculum and catalog domain."""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class ProgramRead(BaseModel):
    """A degree program, as other domains see it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    department: str
    college: str | None = None
    total_units_required: Decimal = Decimal("0")
    is_active: bool


class CurriculumRead(BaseModel):
    """A versioned curriculum specification, as other domains see it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    program_id: uuid.UUID
    curriculum_year: str
    effective_admission_year: int | None = None
    is_active: bool


class RecordCourse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    course_code: str
    title: str
    total_units: Decimal


class ScheduleCourse(BaseModel):
    """Course catalog fields used by student schedules."""

    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    course_code: str
    title: str
    lecture_units: Decimal
    lab_units: Decimal


class CatalogCourseCreate(BaseModel):
    """Payload to create a course in the caller's department."""

    model_config = ConfigDict(populate_by_name=True)

    course_code: str = Field(
        min_length=1,
        max_length=20,
        validation_alias=AliasChoices("courseCode", "course_code", "code"),
    )
    title: str = Field(
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices("title", "name"),
    )
    description: str | None = None
    lecture_units: Decimal = Field(
        default=Decimal("3.0"),
        ge=Decimal("0"),
        validation_alias=AliasChoices(
            "lectureUnits", "lecture_units", "units", "credits"
        ),
    )
    lab_units: Decimal = Field(
        default=Decimal("0.0"),
        ge=Decimal("0"),
        validation_alias=AliasChoices("labUnits", "lab_units"),
    )
    lecture_hours: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("lectureHours", "lecture_hours"),
    )
    lab_hours: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("labHours", "lab_hours"),
    )
    grading_mode: str = Field(
        default="NUMERIC",
        max_length=30,
        validation_alias=AliasChoices("gradingMode", "grading_mode"),
    )
    is_active: bool = Field(
        default=True,
        validation_alias=AliasChoices("isActive", "is_active"),
    )
    college: str | None = None
    prerequisite_ids: list[uuid.UUID | str] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "prerequisiteIds",
            "prerequisite_ids",
            "prerequisites",
            "prerequisiteCourseCodes",
            "prerequisite_course_codes",
        ),
    )
    corequisite_ids: list[uuid.UUID | str] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "corequisiteIds",
            "corequisite_ids",
            "corequisites",
            "corequisiteCourseCodes",
            "corequisite_course_codes",
        ),
    )


class CatalogCourseUpdate(BaseModel):
    """Payload to edit an existing course in the caller's department."""

    model_config = ConfigDict(populate_by_name=True)

    course_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        validation_alias=AliasChoices("courseCode", "course_code", "code"),
    )
    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices("title", "name"),
    )
    description: str | None = None
    lecture_units: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        validation_alias=AliasChoices(
            "lectureUnits", "lecture_units", "units", "credits"
        ),
    )
    lab_units: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        validation_alias=AliasChoices("labUnits", "lab_units"),
    )
    lecture_hours: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("lectureHours", "lecture_hours"),
    )
    lab_hours: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("labHours", "lab_hours"),
    )
    grading_mode: str | None = Field(
        default=None,
        max_length=30,
        validation_alias=AliasChoices("gradingMode", "grading_mode"),
    )
    is_active: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("isActive", "is_active"),
    )
    prerequisite_ids: list[uuid.UUID | str] | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "prerequisiteIds",
            "prerequisite_ids",
            "prerequisites",
            "prerequisiteCourseCodes",
            "prerequisite_course_codes",
        ),
    )
    corequisite_ids: list[uuid.UUID | str] | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "corequisiteIds",
            "corequisite_ids",
            "corequisites",
            "corequisiteCourseCodes",
            "corequisite_course_codes",
        ),
    )


class CatalogCourseRead(BaseModel):
    """Catalog course representation returned by admin catalog endpoints."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    code: str = ""
    course_code: str = Field(serialization_alias="courseCode")
    title: str
    description: str | None = None
    department: str | None = None
    college: str | None = None
    units: Decimal = Decimal("0")
    lecture_units: Decimal = Field(serialization_alias="lectureUnits")
    lab_units: Decimal = Field(serialization_alias="labUnits")
    lecture_hours: int = Field(default=0, serialization_alias="lectureHours")
    lab_hours: int = Field(default=0, serialization_alias="labHours")
    total_units: Decimal = Field(serialization_alias="totalUnits")
    grading_mode: str = Field(default="NUMERIC", serialization_alias="gradingMode")
    is_active: bool = Field(default=True, serialization_alias="isActive")
    prerequisite_ids: list[uuid.UUID] = Field(
        default_factory=list, serialization_alias="prerequisiteIds"
    )
    corequisite_ids: list[uuid.UUID] = Field(
        default_factory=list, serialization_alias="corequisiteIds"
    )
    prerequisites: list[str] = Field(default_factory=list)
    corequisites: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def populate_convenience_fields(self) -> CatalogCourseRead:
        if not self.code:
            self.code = self.course_code
        if self.units == Decimal("0") and self.total_units:
            self.units = self.total_units
        return self


class CurriculumCourseInput(BaseModel):
    """One course mapping when publishing a curriculum version."""

    model_config = ConfigDict(populate_by_name=True)

    course_id: uuid.UUID | None = Field(
        default=None,
        validation_alias=AliasChoices("courseId", "course_id", "id"),
    )
    course_code: str | None = Field(
        default=None,
        validation_alias=AliasChoices("courseCode", "course_code", "code"),
    )
    year_level: int = Field(
        default=1,
        ge=1,
        le=6,
        validation_alias=AliasChoices("yearLevel", "year_level"),
    )
    semester: str = Field(default="First Semester")
    course_type: str = Field(
        default="core",
        validation_alias=AliasChoices("courseType", "course_type", "type"),
    )
    prerequisite_ids: list[uuid.UUID | str] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "prerequisiteIds",
            "prerequisite_ids",
            "prerequisites",
            "prerequisiteCourseCodes",
            "prerequisite_course_codes",
        ),
    )
    corequisite_ids: list[uuid.UUID | str] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "corequisiteIds",
            "corequisite_ids",
            "corequisites",
            "corequisiteCourseCodes",
            "corequisite_course_codes",
        ),
    )

    @model_validator(mode="after")
    def require_course_reference(self) -> CurriculumCourseInput:
        if self.course_id is None and not self.course_code:
            raise ValueError("Each curriculum course requires courseId or courseCode.")
        return self


class CurriculumVersionCreate(BaseModel):
    """Payload to publish a curriculum version for a program."""

    model_config = ConfigDict(populate_by_name=True)

    effective_admission_year: int = Field(
        ge=1900,
        le=2200,
        validation_alias=AliasChoices(
            "effectiveAdmissionYear", "effective_admission_year"
        ),
    )
    curriculum_year: str | None = Field(
        default=None,
        max_length=20,
        validation_alias=AliasChoices(
            "curriculumYear", "curriculum_year", "version", "name"
        ),
    )
    courses: list[CurriculumCourseInput] = Field(
        default_factory=list,
        validation_alias=AliasChoices("courses", "items"),
    )


class CurriculumCourseItemRead(BaseModel):
    """One mapped course inside a curriculum version."""

    model_config = ConfigDict(populate_by_name=True)

    course_id: uuid.UUID = Field(serialization_alias="courseId")
    course_code: str = Field(serialization_alias="courseCode")
    title: str
    units: Decimal
    year_level: int = Field(serialization_alias="yearLevel")
    semester: str
    course_type: str = Field(serialization_alias="courseType")


class CurriculumVersionRead(BaseModel):
    """Published curriculum version returned by the admin catalog API."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    program_id: uuid.UUID = Field(serialization_alias="programId")
    program_code: str = Field(serialization_alias="programCode")
    version: str = ""
    curriculum_year: str = Field(serialization_alias="curriculumYear")
    effective_admission_year: int | None = Field(
        default=None, serialization_alias="effectiveAdmissionYear"
    )
    is_active: bool = Field(default=True, serialization_alias="isActive")
    courses: list[CurriculumCourseItemRead] = Field(default_factory=list)

    @model_validator(mode="after")
    def populate_version(self) -> CurriculumVersionRead:
        if not self.version:
            self.version = self.curriculum_year
        return self


class SubstitutionApproveRequest(BaseModel):
    """Payload to approve a course substitution for one or more students."""

    model_config = ConfigDict(populate_by_name=True)

    original_course_id: uuid.UUID = Field(
        validation_alias=AliasChoices("originalCourseId", "original_course_id")
    )
    substitute_course_id: uuid.UUID = Field(
        validation_alias=AliasChoices("substituteCourseId", "substitute_course_id")
    )
    student_ids: list[uuid.UUID] = Field(
        min_length=1,
        validation_alias=AliasChoices("studentIds", "student_ids"),
    )


class StudentDegreeCourseProgressRead(BaseModel):
    """Per-course degree progress status for a student."""

    model_config = ConfigDict(populate_by_name=True)

    course_id: uuid.UUID = Field(serialization_alias="courseId")
    course_code: str = Field(serialization_alias="courseCode")
    title: str
    units: Decimal
    completed: bool
    substituted_by_course_id: uuid.UUID | None = Field(
        default=None, serialization_alias="substitutedByCourseId"
    )
    substituted_by_course_code: str | None = Field(
        default=None, serialization_alias="substitutedByCourseCode"
    )


class StudentDegreeProgressRead(BaseModel):
    """A student's curriculum degree progress, accounting for approved substitutions."""

    model_config = ConfigDict(populate_by_name=True)

    student_id: uuid.UUID = Field(serialization_alias="studentId")
    curriculum_id: uuid.UUID | None = Field(
        default=None, serialization_alias="curriculumId"
    )
    completed_units: Decimal = Field(serialization_alias="completedUnits")
    total_units_required: Decimal = Field(serialization_alias="totalUnitsRequired")
    completed_course_ids: list[uuid.UUID] = Field(
        default_factory=list, serialization_alias="completedCourseIds"
    )
    completed_course_codes: list[str] = Field(
        default_factory=list, serialization_alias="completedCourseCodes"
    )
    substituted_course_ids: dict[str, uuid.UUID] = Field(
        default_factory=dict, serialization_alias="substitutedCourseIds"
    )
    courses: list[StudentDegreeCourseProgressRead] = Field(default_factory=list)


class SubstitutionApprovalRead(BaseModel):
    """Response returned when a course substitution is approved."""

    model_config = ConfigDict(populate_by_name=True)

    original_course_id: uuid.UUID = Field(serialization_alias="originalCourseId")
    substitute_course_id: uuid.UUID = Field(serialization_alias="substituteCourseId")
    student_ids: list[uuid.UUID] = Field(serialization_alias="studentIds")
    degree_progress: list[StudentDegreeProgressRead] = Field(
        default_factory=list, serialization_alias="degreeProgress"
    )
