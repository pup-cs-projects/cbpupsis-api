"""Explicit academic metadata and waiver workflow integrity/IAM.

Revision ID: e099_academic_waivers
Revises: e170_program_college

The academic checks fail closed on a NULL classification, so existing courses
and terms are backfilled from their codes and semester names. A reviewed
manifest (scripts/seed_course_classifications.py) overrides these defaults.
graduating_final_term_id stays NULL until an owner sets it.
"""

import sqlalchemy as sa
from alembic import op

revision = "e099_academic_waivers"
down_revision = "e170_program_college"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("courses", sa.Column("pe_sequence", sa.SmallInteger(), nullable=True))
    op.add_column("courses", sa.Column("is_nstp", sa.Boolean(), nullable=True))
    op.create_check_constraint(
        "ck_courses_academic_classification",
        "courses",
        "pe_sequence IS NULL OR (pe_sequence BETWEEN 1 AND 4 AND is_nstp IS FALSE)",
    )
    op.add_column(
        "academic_terms", sa.Column("term_type", sa.String(20), nullable=True)
    )
    # Same patterns as cbpupsis_shared.domains.curriculum.classification, frozen
    # here so this revision keeps its meaning if that module changes.
    op.execute(r"""
        UPDATE courses SET
            is_nstp = regexp_replace(upper(course_code), '[^A-Z0-9]', '', 'g')
                ~ '^(NSTP|CWTS|LTS|ROTC)[0-9]*$',
            pe_sequence = CASE
                WHEN regexp_replace(upper(course_code), '[^A-Z0-9]', '', 'g')
                    ~ '^(PE|PED|PHED|PATHFIT)(10|0*)[1-4]$'
                THEN CAST(right(course_code, 1) AS smallint)
            END
    """)
    op.execute("""
        UPDATE academic_terms SET term_type = CASE
            WHEN lower(semester) LIKE '%summer%' THEN 'SUMMER'
            WHEN lower(semester) LIKE '%first%'
              OR lower(semester) LIKE '%second%'
              OR lower(semester) LIKE '1st%'
              OR lower(semester) LIKE '2nd%' THEN 'REGULAR'
        END
    """)
    op.add_column(
        "student_profiles",
        sa.Column(
            "graduating_final_term_id",
            sa.Uuid(),
            sa.ForeignKey("academic_terms.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "course_waivers",
        sa.Column(
            "registrar_approved_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    # Existing duplicate live edges require explicit operator review, not an
    # automatic deletion/approval/rejection hidden inside a migration.
    op.create_index(
        "uq_course_waivers_live_edge",
        "course_waivers",
        [
            "student_id",
            "section_id",
            "prerequisite_course_code",
        ],
        unique=True,
        postgresql_where=sa.text("state <> 'REJECTED'"),
    )
    bind = op.get_bind()
    for action, description in (
        ("CreateWaiverRequest", "File your own prerequisite waiver request"),
        ("ReadWaiverRequest", "Read position-scoped prerequisite waiver requests"),
        ("ApproveWaiverRequest", "Record the assigned prerequisite waiver decision"),
    ):
        bind.execute(
            sa.text("""
            INSERT INTO permissions(action, description) VALUES (:action, :description)
            ON CONFLICT (action) DO NOTHING
        """),
            {"action": action, "description": description},
        )
    for policy, actions, groups in (
        ("WaiverRequestAuthor", ["CreateWaiverRequest"], ["Students", "Superadmins"]),
        (
            "WaiverRequestAdmin",
            ["ReadWaiverRequest", "ApproveWaiverRequest"],
            ["Admins", "Superadmins"],
        ),
    ):
        bind.execute(
            sa.text(
                "INSERT INTO policies(name) VALUES (:name) "
                "ON CONFLICT (name) DO NOTHING"
            ),
            {"name": policy},
        )
        for action in actions:
            bind.execute(
                sa.text("""
                INSERT INTO policy_permissions(policy_id, permission_id)
                SELECT p.id, a.id FROM policies p CROSS JOIN permissions a
                WHERE p.name=:policy AND a.action=:action ON CONFLICT DO NOTHING
            """),
                {"policy": policy, "action": action},
            )
        for group in groups:
            bind.execute(
                sa.text("""
                INSERT INTO group_policies(group_id, policy_id)
                SELECT g.id,p.id FROM groups g CROSS JOIN policies p
                WHERE g.name=:group AND p.name=:policy ON CONFLICT DO NOTHING
            """),
                {"group": group, "policy": policy},
            )
    # No tables or sequences added: current runtime ACLs cover these columns.


def downgrade() -> None:
    # IAM grants are reproducible operational data; retain them on rollback,
    # as removing a policy could erase an operator's independently assigned grant.
    op.drop_index("uq_course_waivers_live_edge", table_name="course_waivers")
    op.drop_column("course_waivers", "registrar_approved_by")
    op.drop_column("student_profiles", "graduating_final_term_id")
    op.drop_column("academic_terms", "term_type")
    op.drop_constraint("ck_courses_academic_classification", "courses", type_="check")
    op.drop_column("courses", "is_nstp")
    op.drop_column("courses", "pe_sequence")
