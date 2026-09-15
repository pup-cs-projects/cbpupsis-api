"""Pydantic schemas (DTOs) for the IAM domain.

These define the IAM domain's public data contract for both its own management
endpoints and cross-domain/auth consumers. ORM models never cross a boundary;
these do.

Schema shapes follow the project convention of splitting by direction and role:
a ``Base`` holds shared fields, ``Create`` carries input, and ``Read`` carries
output (adding server-owned fields such as ids and timestamps).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domains.iam.constants import (
    ACTION_MAX_LENGTH,
    DESCRIPTION_MAX_LENGTH,
    NAME_MAX_LENGTH,
)


# --------------------------------------------------------------------------- #
# Permission
# --------------------------------------------------------------------------- #
class PermissionBase(BaseModel):
    """Fields shared by permission input and output."""

    action: str = Field(
        ...,
        max_length=ACTION_MAX_LENGTH,
        examples=["ReadItem", "ModerateItem"],
        description="Machine-readable action name; unique and stable.",
    )
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)


class PermissionCreate(PermissionBase):
    """Payload for creating a permission."""


class PermissionRead(PermissionBase):
    """Permission as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
class PolicyBase(BaseModel):
    """Fields shared by policy input and output."""

    name: str = Field(..., max_length=NAME_MAX_LENGTH, examples=["ObjectFullAccess"])
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)


class PolicyCreate(PolicyBase):
    """Payload for creating a policy.

    ``permission_actions`` lets the caller declare the policy's contents in one
    request by naming existing permissions; the service resolves them to ids.
    """

    permission_actions: list[str] = Field(
        default_factory=list,
        description="Actions to include in this policy (must already exist).",
    )


class PolicyRead(PolicyBase):
    """Policy as returned by the API, including its resolved permissions."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    permissions: list[PermissionRead] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Group
# --------------------------------------------------------------------------- #
class GroupBase(BaseModel):
    """Fields shared by group input and output."""

    name: str = Field(..., max_length=NAME_MAX_LENGTH, examples=["Teachers"])
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)


class GroupCreate(GroupBase):
    """Payload for creating a group."""


class GroupRead(GroupBase):
    """Group as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int


# --------------------------------------------------------------------------- #
# Attachment request bodies
# --------------------------------------------------------------------------- #
class AttachPolicyRequest(BaseModel):
    """Body for attaching a policy to a group."""

    policy_id: int


class AddUserToGroupRequest(BaseModel):
    """Body for adding a user to a group."""

    user_id: str = Field(..., description="Target user's UUID.")


class EffectivePermissionsRead(BaseModel):
    """Every action a user is effectively granted, and how they got it.

    The two sources are reported separately rather than pre-merged because the
    question an admin screen actually asks is not "what can they do?" but "why
    can they do it, and what do I change to stop it?" — and those have different
    answers: a group grant is revoked by changing membership, a direct grant by
    detaching the policy. ``effective`` is the union the guards actually check.
    """

    user_id: str
    #: The union of both paths — what ``require_permission`` resolves against.
    effective: list[str]
    #: Granted via group membership (user -> group -> policy -> permission).
    from_groups: list[str]
    #: Granted by a policy attached straight to the user, bypassing groups.
    from_direct: list[str]
