"""IAM (Identity & Access Management) models — an AWS-IAM-inspired RBAC schema.

The model mirrors AWS IAM's structure, which separates *what an action is* from
*who may perform it* through a reusable policy layer:

    User --< UserGroup >-- Group --< GroupPolicy >-- Policy
                                                       |
                                         PolicyPermission >-- Permission

    User --< UserPolicy >-- Policy      (optional direct attachment)

Concepts
--------
Permission
    A single, atomic action the application understands, e.g. ``"ReadItem"``.
    Analogous to an IAM action such as ``s3:GetObject``.
Policy
    A named, *reusable* bundle of permissions, e.g. ``"ObjectFullAccess"``.
    Attaching one policy to several groups avoids re-listing permissions.
Group
    A container of users, e.g. ``"Teachers"``. Analogous to an IAM group.
    Policies attached to a group are inherited by all its members.
User
    An authenticated principal. May belong to many groups (and optionally have
    policies attached directly), and its *effective permissions* are the union
    of all permissions reachable through those attachments.

Design boundaries (intentionally NOT copied from IAM)
-----------------------------------------------------
This schema stops at *action-level* RBAC. It deliberately omits IAM's more
advanced machinery—policy conditions (ABAC), explicit-deny precedence, and
resource-level policies—because they add substantial evaluation complexity that
most applications never need. Add them only against a concrete requirement.

Cross-domain boundary
---------------------
``user_id`` columns here carry no foreign key to ``users.id``. IAM never reads
the users table — it maps ids to permissions — so the two domains stay
independently extractable. Referential integrity is enforced in the service
layer instead.

Scoping
-------
Roles/permissions are *global* in this version. The association tables that
would carry scope (``user_groups`` especially) are the natural place to add a
nullable ``scope_id`` column later (e.g. an organization or school id) to move
to scoped/multi-tenant RBAC without restructuring the core tables.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.domains.iam.constants import (
    ACTION_MAX_LENGTH,
    DESCRIPTION_MAX_LENGTH,
    NAME_MAX_LENGTH,
)
from app.shared.models import TimestampMixin


class Permission(TimestampMixin, Base):
    """An atomic action the application can authorize (e.g. ``"DeleteItem"``).

    Permissions are the vocabulary of authorization: endpoints check for a
    permission, never for a group or policy name. This indirection is what lets
    you change *who* can do something by editing data (policy/group membership)
    rather than code.
    """

    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Machine-readable action name. Unique and stable; treat as an identifier.
    action: Mapped[str] = mapped_column(
        String(ACTION_MAX_LENGTH), unique=True, index=True
    )
    #: Human-readable explanation for admin UIs and documentation.
    description: Mapped[str | None] = mapped_column(
        String(DESCRIPTION_MAX_LENGTH), default=None
    )


class Policy(TimestampMixin, Base):
    """A named, reusable bundle of permissions (e.g. ``"ObjectReadOnly"``).

    A policy is attached to groups (or, optionally, directly to users). Because
    the same policy can be attached in many places, changing its permission set
    updates every attachment at once.
    """

    __tablename__ = "policies"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Unique policy name, e.g. ``"ObjectFullAccess"``.
    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(
        String(DESCRIPTION_MAX_LENGTH), default=None
    )

    #: The permissions this policy grants, so a policy can be returned with its
    #: contents. ``lazy="selectin"`` loads them in a second query alongside the
    #: parent: eager, but without the row multiplication of a JOIN, and safe
    #: under async (a lazy load outside an await raises MissingGreenlet).
    #: Both relationships are intra-domain, so no boundary is crossed.
    permissions: Mapped[list[Permission]] = relationship(
        secondary="policy_permissions",
        lazy="selectin",
        viewonly=True,
    )


class Group(TimestampMixin, Base):
    """A container of users to which policies are attached (e.g. ``"Admins"``).

    Membership is many-to-many: a user can belong to several groups and inherit
    the union of their policies. This is the primary way permissions are granted
    in this system—prefer group membership over direct user-policy attachment.
    """

    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Unique group name, e.g. ``"Teachers"``.
    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(
        String(DESCRIPTION_MAX_LENGTH), default=None
    )


# ---------------------------------------------------------------------------
# Association (junction) tables.
#
# Each models a many-to-many edge in the graph above. They use composite
# primary keys (the pair of foreign keys) so a given edge cannot be duplicated,
# and index the foreign keys for fast traversal during permission resolution.
# ---------------------------------------------------------------------------


class PolicyPermission(Base):
    """Edge: which permissions a policy contains (policy *N—N* permission)."""

    __tablename__ = "policy_permissions"

    policy_id: Mapped[int] = mapped_column(
        ForeignKey("policies.id", ondelete="CASCADE"),
        primary_key=True,
    )
    permission_id: Mapped[int] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
    )


class GroupPolicy(Base):
    """Edge: which policies are attached to a group (group *N—N* policy)."""

    __tablename__ = "group_policies"

    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"),
        primary_key=True,
    )
    policy_id: Mapped[int] = mapped_column(
        ForeignKey("policies.id", ondelete="CASCADE"),
        primary_key=True,
    )


class UserGroup(Base):
    """Edge: which groups a user belongs to (user *N—N* group).

    This is the recommended future home for a nullable ``scope_id`` column when
    moving to scoped/multi-tenant RBAC (e.g. "admin *of this school*").
    """

    __tablename__ = "user_groups"

    #: Bare id, no FK to users.id: a database-level constraint across domains is
    #: a coupling a service split cannot sever. Integrity is enforced in the
    #: service layer via app.domains.users.client.user_exists.
    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, index=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"),
        primary_key=True,
    )


class UserPolicy(Base):
    """Edge: policies attached directly to a user, bypassing groups.

    Mirrors IAM's inline/attached user policies. Use sparingly—group membership
    is easier to reason about and audit—but it is occasionally needed for
    one-off grants that do not justify a dedicated group.
    """

    __tablename__ = "user_policies"

    #: Bare id, no cross-domain FK — see UserGroup.user_id.
    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, index=True)
    policy_id: Mapped[int] = mapped_column(
        ForeignKey("policies.id", ondelete="CASCADE"),
        primary_key=True,
    )
