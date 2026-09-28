"""cbpupsis initial schema

Revision ID: f1a2b3c4d5e6
Revises: e5df35334433
Create Date: 2026-09-24 21:45:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'f1a2b3c4d5e6'
down_revision = 'e5df35334433'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0. EXTENSIONS & HELPER FUNCTIONS
    op.execute(sa.text('CREATE EXTENSION IF NOT EXISTS "pgcrypto";'))
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION gen_uuid_v7()
        RETURNS uuid AS $$
        DECLARE
          unix_time_ms bytea;
          uuid_bytes bytea;
        BEGIN
          unix_time_ms := substring(int8send(floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint) from 3 for 6);
          uuid_bytes := gen_random_bytes(10);
          RETURN encode(
            unix_time_ms ||
            set_byte(substring(uuid_bytes from 1 for 2), 0, (get_byte(substring(uuid_bytes from 1 for 1), 0) & 15) | 112) ||
            set_byte(substring(uuid_bytes from 3 for 8), 0, (get_byte(substring(uuid_bytes from 3 for 1), 0) & 63) | 128),
            'hex'
          )::uuid;
        END;
        $$ LANGUAGE plpgsql VOLATILE;
    """))

    # 1. AUTHENTICATION, ROLES, PERSONAS, MFA & SESSIONS
    op.create_table(
        'roles',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('code', sa.String(length=50), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code')
    )

    op.create_table(
        'role_permissions',
        sa.Column('role_id', sa.Uuid(), nullable=False),
        sa.Column('permission_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['permission_id'], ['permissions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['role_id'], ['roles.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('role_id', 'permission_id')
    )
    op.create_index('idx_fk_role_permissions_permission_id', 'role_permissions', ['permission_id'], unique=False)

    op.create_table(
        'user_roles',
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('role_id', sa.Uuid(), nullable=False),
        sa.Column('assigned_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['role_id'], ['roles.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'role_id')
    )
    op.create_index('idx_fk_user_roles_role_id', 'user_roles', ['role_id'], unique=False)

    op.create_table(
        'user_permission_overrides',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('permission_id', sa.Integer(), nullable=False),
        sa.Column('is_granted', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('granted_by', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['granted_by'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['permission_id'], ['permissions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'permission_id', name='uq_user_permission_overrides')
    )

    op.create_table(
        'user_profiles',
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('first_name', sa.String(length=100), nullable=False),
        sa.Column('middle_name', sa.String(length=100), nullable=True),
        sa.Column('last_name', sa.String(length=100), nullable=False),
        sa.Column('birthdate', sa.Date(), nullable=True),
        sa.Column('gender', sa.String(length=20), nullable=True),
        sa.Column('civil_status', sa.String(length=30), nullable=True),
        sa.Column('personal_email', sa.String(length=255), nullable=True),
        sa.Column('institutional_email', sa.String(length=255), nullable=True),
        sa.Column('phone_number', sa.String(length=25), nullable=True),
        sa.Column('avatar_url', sa.Text(), nullable=True),
        sa.Column('home_address', sa.Text(), nullable=True),
        sa.Column('current_address', sa.Text(), nullable=True),
        sa.Column('profile_completed', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('email_notifications', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id')
    )

    op.create_table(
        'admin_profiles',
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('employee_id', sa.String(length=30), nullable=True),
        sa.Column('position', sa.String(length=50), nullable=False),
        sa.Column('department', sa.String(length=100), nullable=True),
        sa.Column('college', sa.String(length=100), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id'),
        sa.UniqueConstraint('employee_id')
    )

    op.create_table(
        'faculty_profiles',
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('employee_id', sa.String(length=30), nullable=True),
        sa.Column('college', sa.String(length=100), nullable=True),
        sa.Column('department', sa.String(length=100), nullable=False),
        sa.Column('academic_rank', sa.String(length=50), nullable=True),
        sa.Column('employment_type', sa.String(length=30), server_default='full_time', nullable=False),
        sa.Column('office_location', sa.String(length=100), nullable=True),
        sa.Column('office_hours', sa.String(length=255), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id'),
        sa.UniqueConstraint('employee_id')
    )

    op.create_table(
        'user_mfa_credentials',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('mfa_type', sa.String(length=20), nullable=False),
        sa.Column('totp_secret', sa.String(length=255), nullable=True),
        sa.Column('credential_id', sa.String(length=255), nullable=True),
        sa.Column('public_key', sa.Text(), nullable=True),
        sa.Column('sign_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('is_enrolled', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_fk_user_mfa_credentials_user_id', 'user_mfa_credentials', ['user_id'], unique=False)
    op.create_index('ix_user_mfa_credential_id', 'user_mfa_credentials', ['credential_id'], unique=True, postgresql_where=sa.text("credential_id IS NOT NULL"))

    op.create_table(
        'user_mfa_recovery_codes',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('code_hash', sa.String(length=64), nullable=False),
        sa.Column('is_used', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_fk_user_mfa_recovery_codes_user_id', 'user_mfa_recovery_codes', ['user_id'], unique=False)

    op.create_table(
        'user_active_sessions',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('session_token_hash', sa.String(length=64), nullable=False),
        sa.Column('device_info', sa.Text(), nullable=False),
        sa.Column('ip_address', postgresql.INET(), nullable=False),
        sa.Column('is_current', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('last_activity_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_fk_user_active_sessions_user_id', 'user_active_sessions', ['user_id'], unique=False)

    op.create_table(
        'emergency_contacts',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('contact_name', sa.String(length=150), nullable=False),
        sa.Column('relationship', sa.String(length=50), nullable=False),
        sa.Column('phone_number', sa.String(length=25), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=True),
        sa.Column('address', sa.Text(), nullable=True),
        sa.Column('is_primary', sa.Boolean(), server_default='false', nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_fk_emergency_contacts_user_id', 'emergency_contacts', ['user_id'], unique=False)

    # Partitioned audit logs
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS auth_audit_logs (
          id uuid DEFAULT gen_uuid_v7(),
          user_id uuid,
          attempted_id varchar(50) NOT NULL,
          ip_address inet,
          user_agent text,
          success boolean NOT NULL,
          failure_reason varchar(100),
          is_suspicious boolean NOT NULL DEFAULT false,
          mfa_used boolean NOT NULL DEFAULT false,
          created_at timestamp NOT NULL DEFAULT clock_timestamp(),
          PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at)
    """))
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS admin_audit_trails (
          id uuid DEFAULT gen_uuid_v7(),
          user_id uuid NOT NULL,
          module varchar(50) NOT NULL,
          action_type varchar(50) NOT NULL,
          target_entity varchar(100) NOT NULL,
          target_entity_id uuid NOT NULL,
          previous_state jsonb,
          updated_state jsonb,
          created_at timestamp NOT NULL DEFAULT clock_timestamp(),
          PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at)
    """))
    op.execute(sa.text(
        "CREATE TABLE IF NOT EXISTS auth_audit_logs_default "
        "PARTITION OF auth_audit_logs DEFAULT"
    ))
    op.execute(sa.text(
        "CREATE TABLE IF NOT EXISTS admin_audit_trails_default "
        "PARTITION OF admin_audit_trails DEFAULT"
    ))

    # 2. ACADEMIC CALENDAR & TERMS
    op.create_table(
        'academic_terms',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('academic_year', sa.String(length=20), nullable=False),
        sa.Column('semester', sa.String(length=30), nullable=False),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('end_date', sa.Date(), nullable=False),
        sa.Column('is_current', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('early_enrollment_start', sa.DateTime(), nullable=True),
        sa.Column('early_enrollment_end', sa.DateTime(), nullable=True),
        sa.Column('regular_enrollment_start', sa.DateTime(), nullable=False),
        sa.Column('regular_enrollment_end', sa.DateTime(), nullable=False),
        sa.Column('late_enrollment_end', sa.DateTime(), nullable=True),
        sa.Column('add_drop_deadline', sa.DateTime(), nullable=False),
        sa.Column('grade_submission_deadline', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('academic_year', 'semester', name='uq_academic_terms_year_sem')
    )

    op.create_table(
        'academic_events',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('academic_term_id', sa.Uuid(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('end_date', sa.Date(), nullable=True),
        sa.Column('event_type', sa.String(length=50), nullable=False),
        sa.ForeignKeyConstraint(['academic_term_id'], ['academic_terms.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_fk_academic_events_term_id', 'academic_events', ['academic_term_id'], unique=False)

    # 3. CURRICULUM, COURSES & SECTIONS
    op.create_table(
        'programs',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('code', sa.String(length=20), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('department', sa.String(length=100), nullable=False),
        sa.Column('total_units_required', sa.Numeric(precision=5, scale=1), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code')
    )

    op.create_table(
        'curricula',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('program_id', sa.Uuid(), nullable=False),
        sa.Column('curriculum_year', sa.String(length=20), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.ForeignKeyConstraint(['program_id'], ['programs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('program_id', 'curriculum_year', name='uq_curricula_prog_year'),
        sa.UniqueConstraint('id', 'program_id', name='uq_curricula_id_prog')
    )

    op.create_table(
        'courses',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('course_code', sa.String(length=20), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('lecture_units', sa.Numeric(precision=3, scale=1), server_default='0', nullable=False),
        sa.Column('lab_units', sa.Numeric(precision=3, scale=1), server_default='0', nullable=False),
        sa.Column('total_units', sa.Numeric(precision=3, scale=1), sa.Computed('lecture_units + lab_units', persisted=True), nullable=True),
        sa.Column('grading_mode', sa.String(length=30), server_default='NUMERIC', nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('course_code')
    )

    op.create_table(
        'curriculum_courses',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('curriculum_id', sa.Uuid(), nullable=False),
        sa.Column('course_id', sa.Uuid(), nullable=False),
        sa.Column('year_level', sa.SmallInteger(), nullable=False),
        sa.Column('semester', sa.String(length=30), nullable=False),
        sa.Column('course_type', sa.String(length=30), server_default='core', nullable=False),
        sa.ForeignKeyConstraint(['course_id'], ['courses.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['curriculum_id'], ['curricula.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('curriculum_id', 'course_id', name='uq_curriculum_courses')
    )

    op.create_table(
        'course_prerequisites',
        sa.Column('course_id', sa.Uuid(), nullable=False),
        sa.Column('prerequisite_course_id', sa.Uuid(), nullable=False),
        sa.Column('min_grade', sa.String(length=10), server_default='3.00', nullable=True),
        sa.Column('is_corequisite', sa.Boolean(), server_default='false', nullable=False),
        sa.ForeignKeyConstraint(['course_id'], ['courses.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['prerequisite_course_id'], ['courses.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('course_id', 'prerequisite_course_id')
    )

    op.create_table(
        'course_sections',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('academic_term_id', sa.Uuid(), nullable=False),
        sa.Column('course_id', sa.Uuid(), nullable=False),
        sa.Column('faculty_id', sa.Uuid(), nullable=True),
        sa.Column('section_name', sa.String(length=30), nullable=False),
        sa.Column('capacity', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='open', nullable=False),
        sa.ForeignKeyConstraint(['academic_term_id'], ['academic_terms.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['course_id'], ['courses.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['faculty_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('academic_term_id', 'course_id', 'section_name', name='uq_course_sections_term_course_sec'),
        sa.UniqueConstraint('id', 'academic_term_id', name='uq_course_sections_id_term')
    )
    op.create_index('idx_fk_course_sections_course_id', 'course_sections', ['course_id'], unique=False)
    op.create_index('idx_fk_course_sections_faculty_id', 'course_sections', ['faculty_id'], unique=False)

    op.create_table(
        'section_schedules',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('section_id', sa.Uuid(), nullable=False),
        sa.Column('day_of_week', sa.SmallInteger(), nullable=False),
        sa.Column('start_time', sa.Time(), nullable=False),
        sa.Column('end_time', sa.Time(), nullable=False),
        sa.Column('room', sa.String(length=50), nullable=True),
        sa.Column('schedule_type', sa.String(length=20), server_default='lecture', nullable=False),
        sa.ForeignKeyConstraint(['section_id'], ['course_sections.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_fk_section_schedules_section_id', 'section_schedules', ['section_id'], unique=False)

    # 4. STUDENT REGISTRATION, ENROLLMENT & ACADEMIC REQUESTS
    op.create_table(
        'student_profiles',
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('student_number', sa.String(length=30), nullable=False),
        sa.Column('program_id', sa.Uuid(), nullable=False),
        sa.Column('curriculum_id', sa.Uuid(), nullable=True),
        sa.Column('year_level', sa.SmallInteger(), nullable=False),
        sa.Column('section', sa.String(length=20), nullable=True),
        sa.Column('enrollment_status', sa.String(length=30), server_default='pending', nullable=False),
        sa.Column('fhe_qualified', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('fhe_forfeited', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('fhe_forfeiture_reason', sa.String(length=255), nullable=True),
        sa.Column('is_graduating_senior', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('has_financial_hold', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('has_academic_hold', sa.Boolean(), server_default='false', nullable=False),
        sa.ForeignKeyConstraint(['curriculum_id', 'program_id'], ['curricula.id', 'curricula.program_id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['program_id'], ['programs.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id'),
        sa.UniqueConstraint('student_number')
    )
    op.create_index('idx_fk_student_profiles_program_id', 'student_profiles', ['program_id'], unique=False)
    op.create_index('ix_student_profiles_student_number', 'student_profiles', ['student_number'], unique=True)

    op.create_table(
        'term_enrollments',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('student_id', sa.Uuid(), nullable=False),
        sa.Column('academic_term_id', sa.Uuid(), nullable=False),
        sa.Column('enrollment_status', sa.String(length=30), server_default='enrolled', nullable=False),
        sa.Column('cor_downloaded', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('enrolled_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['academic_term_id'], ['academic_terms.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['student_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('student_id', 'academic_term_id', name='uq_term_enrollments_student_term'),
        sa.UniqueConstraint('id', 'academic_term_id', name='uq_term_enrollments_id_term')
    )
    op.create_index('idx_fk_term_enrollments_term_id', 'term_enrollments', ['academic_term_id'], unique=False)

    op.create_table(
        'cor_issuances',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('student_id', sa.Uuid(), nullable=False),
        sa.Column('term_enrollment_id', sa.Uuid(), nullable=False),
        sa.Column('verification_token_hash', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='CURRENT', nullable=False),
        sa.Column('issued_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.Column('superseded_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['student_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['term_enrollment_id'], ['term_enrollments.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('verification_token_hash')
    )
    op.create_index('idx_fk_cor_issuances_enrollment_id', 'cor_issuances', ['term_enrollment_id'], unique=False)

    op.create_table(
        'student_course_enrollments',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('term_enrollment_id', sa.Uuid(), nullable=False),
        sa.Column('academic_term_id', sa.Uuid(), nullable=False),
        sa.Column('section_id', sa.Uuid(), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='enrolled', nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['section_id', 'academic_term_id'], ['course_sections.id', 'course_sections.academic_term_id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['term_enrollment_id', 'academic_term_id'], ['term_enrollments.id', 'term_enrollments.academic_term_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('term_enrollment_id', 'section_id', name='uq_student_course_enrollments_item')
    )
    op.create_index('idx_fk_student_course_enrollments_section_id', 'student_course_enrollments', ['section_id'], unique=False)
    op.create_index('idx_student_course_enrollments_active_seats', 'student_course_enrollments', ['section_id'], unique=False, postgresql_where=sa.text("status = 'enrolled'"))

    op.create_table(
        'add_drop_requests',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('student_id', sa.Uuid(), nullable=False),
        sa.Column('section_id', sa.Uuid(), nullable=False),
        sa.Column('request_type', sa.String(length=20), nullable=False),
        sa.Column('reason', sa.String(length=100), nullable=False),
        sa.Column('comments', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), server_default='submitted', nullable=False),
        sa.Column('reviewed_by', sa.Uuid(), nullable=True),
        sa.Column('review_notes', sa.Text(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['reviewed_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['section_id'], ['course_sections.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['student_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_table(
        'overload_requests',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('student_id', sa.Uuid(), nullable=False),
        sa.Column('academic_term_id', sa.Uuid(), nullable=False),
        sa.Column('requested_units', sa.SmallInteger(), nullable=False),
        sa.Column('justification', sa.Text(), nullable=False),
        sa.Column('state', sa.String(length=30), server_default='PENDING_DEAN', nullable=False),
        sa.Column('reviewed_by', sa.Uuid(), nullable=True),
        sa.Column('review_remarks', sa.Text(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['academic_term_id'], ['academic_terms.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['reviewed_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['student_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_fk_overload_requests_student_id', 'overload_requests', ['student_id'], unique=False)

    op.create_table(
        'course_waivers',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('student_id', sa.Uuid(), nullable=False),
        sa.Column('section_id', sa.Uuid(), nullable=False),
        sa.Column('prerequisite_course_code', sa.String(length=20), nullable=False),
        sa.Column('condition', sa.String(length=50), nullable=False),
        sa.Column('justification', sa.Text(), nullable=False),
        sa.Column('state', sa.String(length=30), server_default='PENDING_CHAIR', nullable=False),
        sa.Column('chair_approved_by', sa.Uuid(), nullable=True),
        sa.Column('chair_approved_at', sa.DateTime(), nullable=True),
        sa.Column('dean_approved_by', sa.Uuid(), nullable=True),
        sa.Column('dean_approved_at', sa.DateTime(), nullable=True),
        sa.Column('vpaa_token', sa.String(length=128), nullable=True),
        sa.Column('vpaa_approved_at', sa.DateTime(), nullable=True),
        sa.Column('review_remarks', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['chair_approved_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['dean_approved_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['section_id'], ['course_sections.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['student_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_fk_course_waivers_student_id', 'course_waivers', ['student_id'], unique=False)

    # 5. GRADING, ASSESSMENTS, INCOMPLETES & GRADE CORRECTIONS
    op.create_table(
        'student_grades',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('enrollment_item_id', sa.Uuid(), nullable=False),
        sa.Column('raw_score', sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column('midterm_grade', sa.String(length=10), nullable=True),
        sa.Column('final_grade', sa.String(length=10), nullable=True),
        sa.Column('inc_expires_at', sa.DateTime(), nullable=True),
        sa.Column('completion_status', sa.String(length=30), nullable=True),
        sa.Column('remarks', sa.String(length=100), nullable=True),
        sa.Column('is_draft', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('is_locked', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('submitted_by', sa.Uuid(), nullable=True),
        sa.Column('submitted_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['enrollment_item_id'], ['student_course_enrollments.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['submitted_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('enrollment_item_id')
    )

    op.create_table(
        'grade_assessments',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('grade_id', sa.Uuid(), nullable=False),
        sa.Column('assessment_title', sa.String(length=100), nullable=False),
        sa.Column('weight_percentage', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('score', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('max_score', sa.Numeric(precision=5, scale=2), server_default='100', nullable=False),
        sa.ForeignKeyConstraint(['grade_id'], ['student_grades.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('grade_id', 'assessment_title', name='uq_grade_assessments_grade_title')
    )

    op.create_table(
        'incomplete_completions',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('grade_id', sa.Uuid(), nullable=False),
        sa.Column('proposed_mark', sa.String(length=10), nullable=False),
        sa.Column('completion_form_file_id', sa.String(length=100), nullable=False),
        sa.Column('remarks', sa.Text(), nullable=True),
        sa.Column('state', sa.String(length=30), server_default='PENDING_DEAN', nullable=False),
        sa.Column('version', sa.Integer(), server_default='1', nullable=False),
        sa.Column('reviewed_by', sa.Uuid(), nullable=True),
        sa.Column('review_remarks', sa.Text(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['grade_id'], ['student_grades.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['reviewed_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_fk_incomplete_completions_grade_id', 'incomplete_completions', ['grade_id'], unique=False)

    op.create_table(
        'grade_corrections',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('grade_id', sa.Uuid(), nullable=False),
        sa.Column('faculty_id', sa.Uuid(), nullable=False),
        sa.Column('current_mark', sa.String(length=10), nullable=False),
        sa.Column('proposed_mark', sa.String(length=10), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('grading_sheet_file_id', sa.String(length=100), nullable=False),
        sa.Column('state', sa.String(length=30), server_default='PENDING_CHAIR', nullable=False),
        sa.Column('version', sa.Integer(), server_default='1', nullable=False),
        sa.Column('chair_approved_by', sa.Uuid(), nullable=True),
        sa.Column('chair_approved_at', sa.DateTime(), nullable=True),
        sa.Column('dean_approved_by', sa.Uuid(), nullable=True),
        sa.Column('dean_approved_at', sa.DateTime(), nullable=True),
        sa.Column('registrar_approved_by', sa.Uuid(), nullable=True),
        sa.Column('registrar_approved_at', sa.DateTime(), nullable=True),
        sa.Column('rejection_remarks', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['chair_approved_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['dean_approved_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['faculty_id'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['grade_id'], ['student_grades.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['registrar_approved_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_fk_grade_corrections_grade_id', 'grade_corrections', ['grade_id'], unique=False)

    op.create_table(
        'student_term_gpa',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('student_id', sa.Uuid(), nullable=False),
        sa.Column('academic_term_id', sa.Uuid(), nullable=False),
        sa.Column('total_gpa_units', sa.Numeric(precision=4, scale=1), nullable=False),
        sa.Column('term_gpa', sa.Numeric(precision=4, scale=2), nullable=False),
        sa.Column('cumulative_gpa', sa.Numeric(precision=4, scale=2), nullable=False),
        sa.Column('academic_standing', sa.String(length=50), server_default='Good Standing', nullable=False),
        sa.Column('calculated_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['academic_term_id'], ['academic_terms.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['student_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('student_id', 'academic_term_id', name='uq_student_term_gpa_student_term')
    )

    op.create_table(
        'academic_standing_appeals',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('student_id', sa.Uuid(), nullable=False),
        sa.Column('academic_term_id', sa.Uuid(), nullable=False),
        sa.Column('current_standing', sa.String(length=50), nullable=False),
        sa.Column('appeal_reason', sa.String(length=100), nullable=False),
        sa.Column('details', sa.Text(), nullable=False),
        sa.Column('supporting_doc_url', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=30), server_default='SUBMITTED', nullable=False),
        sa.Column('reviewed_by', sa.Uuid(), nullable=True),
        sa.Column('review_notes', sa.Text(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['academic_term_id'], ['academic_terms.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['reviewed_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['student_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_fk_standing_appeals_student_id', 'academic_standing_appeals', ['student_id'], unique=False)

    # 6. FINANCIALS, INSTALLMENTS & PAYMENTS
    op.create_table(
        'fee_structures',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('program_id', sa.Uuid(), nullable=False),
        sa.Column('academic_term_id', sa.Uuid(), nullable=False),
        sa.Column('tuition_per_unit', sa.Numeric(precision=10, scale=2), server_default='0', nullable=False),
        sa.Column('registration_fee', sa.Numeric(precision=10, scale=2), server_default='0', nullable=False),
        sa.Column('lab_fee', sa.Numeric(precision=10, scale=2), server_default='0', nullable=False),
        sa.Column('misc_fee', sa.Numeric(precision=10, scale=2), server_default='0', nullable=False),
        sa.Column('late_fee_rate', sa.Numeric(precision=10, scale=2), server_default='0', nullable=False),
        sa.ForeignKeyConstraint(['academic_term_id'], ['academic_terms.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['program_id'], ['programs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('program_id', 'academic_term_id', name='uq_fee_structures_prog_term')
    )

    op.create_table(
        'installment_plans',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('installments_count', sa.SmallInteger(), nullable=False),
        sa.Column('interest_rate', sa.Numeric(precision=5, scale=2), server_default='0', nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_table(
        'student_billing_accounts',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('student_id', sa.Uuid(), nullable=False),
        sa.Column('academic_term_id', sa.Uuid(), nullable=False),
        sa.Column('selected_plan_id', sa.Uuid(), nullable=True),
        sa.Column('gross_amount', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('fhe_subsidy_amount', sa.Numeric(precision=10, scale=2), server_default='0', nullable=False),
        sa.Column('net_amount_due', sa.Numeric(precision=10, scale=2), sa.Computed('gross_amount - fhe_subsidy_amount', persisted=True), nullable=True),
        sa.Column('amount_paid', sa.Numeric(precision=10, scale=2), server_default='0', nullable=False),
        sa.Column('balance', sa.Numeric(precision=10, scale=2), sa.Computed('gross_amount - fhe_subsidy_amount - amount_paid', persisted=True), nullable=True),
        sa.Column('payment_status', sa.String(length=30), server_default='unpaid', nullable=False),
        sa.Column('due_date', sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(['academic_term_id'], ['academic_terms.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['selected_plan_id'], ['installment_plans.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['student_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('student_id', 'academic_term_id', name='uq_student_billing_student_term')
    )

    op.create_table(
        'billing_installments',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('billing_account_id', sa.Uuid(), nullable=False),
        sa.Column('installment_number', sa.SmallInteger(), nullable=False),
        sa.Column('amount_due', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('due_date', sa.Date(), nullable=False),
        sa.Column('status', sa.String(length=30), server_default='upcoming', nullable=False),
        sa.Column('paid_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['billing_account_id'], ['student_billing_accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('billing_account_id', 'installment_number', name='uq_billing_installments_acct_num')
    )

    op.create_table(
        'payments',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('billing_account_id', sa.Uuid(), nullable=False),
        sa.Column('gateway_reference', sa.String(length=150), nullable=False),
        sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('payment_method', sa.String(length=50), nullable=False),
        sa.Column('payment_status', sa.String(length=30), nullable=False),
        sa.Column('receipt_number', sa.String(length=50), nullable=True),
        sa.Column('failure_reason', sa.String(length=255), nullable=True),
        sa.Column('paid_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['billing_account_id'], ['student_billing_accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('gateway_reference'),
        sa.UniqueConstraint('receipt_number')
    )
    op.create_index('idx_fk_payments_billing_account_id', 'payments', ['billing_account_id'], unique=False)

    # 7. ANONYMOUS FACULTY EVALUATIONS
    op.create_table(
        'faculty_evaluation_periods',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('academic_term_id', sa.Uuid(), nullable=False),
        sa.Column('start_date', sa.DateTime(), nullable=False),
        sa.Column('end_date', sa.DateTime(), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.ForeignKeyConstraint(['academic_term_id'], ['academic_terms.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )

    # Evaluations are strictly anonymous so no student_id column is stored
    op.create_table(
        'faculty_evaluations',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('section_id', sa.Uuid(), nullable=False),
        sa.Column('faculty_id', sa.Uuid(), nullable=False),
        sa.Column('evaluation_period_id', sa.Uuid(), nullable=False),
        sa.Column('ratings', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
        sa.Column('comments', sa.Text(), nullable=True),
        sa.Column('submitted_at_coarsened', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['evaluation_period_id'], ['faculty_evaluation_periods.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['faculty_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['section_id'], ['course_sections.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_fk_faculty_evaluations_faculty_id', 'faculty_evaluations', ['faculty_id'], unique=False)
    op.create_index('idx_fk_faculty_evaluations_section_id', 'faculty_evaluations', ['section_id'], unique=False)

    # Separate unlinked participation tag to enforce duplicate guard without tracking responses
    op.create_table(
        'faculty_evaluation_tags',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('student_id', sa.Uuid(), nullable=False),
        sa.Column('section_id', sa.Uuid(), nullable=False),
        sa.Column('evaluation_period_id', sa.Uuid(), nullable=False),
        sa.Column('has_submitted', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['evaluation_period_id'], ['faculty_evaluation_periods.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['section_id'], ['course_sections.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['student_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('student_id', 'section_id', 'evaluation_period_id', name='uq_faculty_eval_tag')
    )

    # 8. SYSTEM HEALTH, BACKUPS (2-PERSON CONTROL), ANNOUNCEMENTS & NOTIFICATIONS
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS system_health_logs (
          id uuid DEFAULT gen_uuid_v7(),
          api_response_time_ms integer NOT NULL,
          db_query_time_ms integer NOT NULL,
          active_users_count integer NOT NULL,
          cpu_utilization_pct numeric(5,2) NOT NULL,
          memory_utilization_pct numeric(5,2) NOT NULL,
          disk_usage_pct numeric(5,2) NOT NULL,
          recorded_at timestamp NOT NULL DEFAULT clock_timestamp(),
          PRIMARY KEY (id, recorded_at)
        ) PARTITION BY RANGE (recorded_at)
    """))
    op.execute(sa.text(
        "CREATE TABLE IF NOT EXISTS system_health_logs_default "
        "PARTITION OF system_health_logs DEFAULT"
    ))

    op.create_table(
        'system_backups',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('backup_type', sa.String(length=20), nullable=False),
        sa.Column('byte_size', sa.BigInteger(), nullable=False),
        sa.Column('checksum_sha256', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('integrity_status', sa.String(length=30), server_default='unverified', nullable=False),
        sa.Column('storage_location', sa.Text(), nullable=False),
        sa.Column('initiated_by', sa.Uuid(), nullable=True),
        sa.Column('requested_restore_by', sa.Uuid(), nullable=True),
        sa.Column('approved_restore_by', sa.Uuid(), nullable=True),
        sa.Column('restore_status', sa.String(length=30), nullable=True),
        sa.Column('restore_requested_at', sa.DateTime(), nullable=True),
        sa.Column('restore_completed_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['approved_restore_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['initiated_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['requested_restore_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_table(
        'announcements',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('author_id', sa.Uuid(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('category', sa.String(length=30), server_default='GENERAL', nullable=False),
        sa.Column('banner_image_url', sa.Text(), nullable=True),
        sa.Column('target_role_id', sa.Uuid(), nullable=True),
        sa.Column('is_pinned', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('is_published', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('published_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['author_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['target_role_id'], ['roles.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_announcements_published_pinned', 'announcements', ['is_published', 'is_pinned', 'published_at'], unique=False)

    op.create_table(
        'cbpupsis_notifications',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('category', sa.String(length=50), server_default='general', nullable=True),
        sa.Column('link_url', sa.String(length=255), nullable=True),
        sa.Column('is_read', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_fk_cbpupsis_notifications_user_unread', 'cbpupsis_notifications', ['user_id'], unique=False, postgresql_where=sa.text('is_read = false'))

    # 9. IDEMPOTENCY KEYS
    op.create_table(
        'idempotency_keys',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_uuid_v7()'), nullable=False),
        sa.Column('idempotency_key', sa.String(length=255), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=True),
        sa.Column('request_method', sa.String(length=10), nullable=False),
        sa.Column('request_path', sa.String(length=255), nullable=False),
        sa.Column('request_payload_hash', sa.String(length=64), nullable=True),
        sa.Column('response_code', sa.Integer(), nullable=True),
        sa.Column('response_body', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=True),
        sa.Column('status', sa.String(length=30), server_default='processing', nullable=False),
        sa.Column('locked_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('idempotency_key', name='uq_idempotency_key')
    )
    op.create_index('ix_idempotency_keys_key_user', 'idempotency_keys', ['idempotency_key', 'user_id'], unique=True)
    op.create_index('ix_idempotency_keys_expires_at', 'idempotency_keys', ['expires_at'], unique=False)


def downgrade() -> None:
    # 9. Idempotency keys
    op.drop_index('ix_idempotency_keys_expires_at', table_name='idempotency_keys')
    op.drop_index('ix_idempotency_keys_key_user', table_name='idempotency_keys')
    op.drop_table('idempotency_keys')

    # 8. Notifications, announcements, backups & health logs
    op.drop_index('idx_fk_cbpupsis_notifications_user_unread', table_name='cbpupsis_notifications')
    op.drop_table('cbpupsis_notifications')
    op.drop_index('ix_announcements_published_pinned', table_name='announcements')
    op.drop_table('announcements')
    op.drop_table('system_backups')
    op.execute(sa.text("DROP TABLE IF EXISTS system_health_logs CASCADE;"))

    # 7. Faculty evaluations
    op.drop_table('faculty_evaluation_tags')
    op.drop_index('idx_fk_faculty_evaluations_section_id', table_name='faculty_evaluations')
    op.drop_index('idx_fk_faculty_evaluations_faculty_id', table_name='faculty_evaluations')
    op.drop_table('faculty_evaluations')
    op.drop_table('faculty_evaluation_periods')

    # 6. Financials, installments & payments
    op.drop_index('idx_fk_payments_billing_account_id', table_name='payments')
    op.drop_table('payments')
    op.drop_table('billing_installments')
    op.drop_table('student_billing_accounts')
    op.drop_table('installment_plans')
    op.drop_table('fee_structures')

    # 5. Grading, assessments & grade corrections
    op.drop_index('idx_fk_standing_appeals_student_id', table_name='academic_standing_appeals')
    op.drop_table('academic_standing_appeals')
    op.drop_table('student_term_gpa')
    op.drop_index('idx_fk_grade_corrections_grade_id', table_name='grade_corrections')
    op.drop_table('grade_corrections')
    op.drop_index('idx_fk_incomplete_completions_grade_id', table_name='incomplete_completions')
    op.drop_table('incomplete_completions')
    op.drop_table('grade_assessments')
    op.drop_table('student_grades')

    # 4. Student registration, enrollment & academic requests
    op.drop_index('idx_fk_course_waivers_student_id', table_name='course_waivers')
    op.drop_table('course_waivers')
    op.drop_index('idx_fk_overload_requests_student_id', table_name='overload_requests')
    op.drop_table('overload_requests')
    op.drop_table('add_drop_requests')
    op.drop_index('idx_student_course_enrollments_active_seats', table_name='student_course_enrollments')
    op.drop_index('idx_fk_student_course_enrollments_section_id', table_name='student_course_enrollments')
    op.drop_table('student_course_enrollments')
    op.drop_index('idx_fk_cor_issuances_enrollment_id', table_name='cor_issuances')
    op.drop_table('cor_issuances')
    op.drop_index('idx_fk_term_enrollments_term_id', table_name='term_enrollments')
    op.drop_table('term_enrollments')
    op.drop_index('ix_student_profiles_student_number', table_name='student_profiles')
    op.drop_index('idx_fk_student_profiles_program_id', table_name='student_profiles')
    op.drop_table('student_profiles')

    # 3. Curricula, courses, sections & schedules
    op.drop_index('idx_fk_section_schedules_section_id', table_name='section_schedules')
    op.drop_table('section_schedules')
    op.drop_index('idx_fk_course_sections_faculty_id', table_name='course_sections')
    op.drop_index('idx_fk_course_sections_course_id', table_name='course_sections')
    op.drop_table('course_sections')
    op.drop_table('course_prerequisites')
    op.drop_table('curriculum_courses')
    op.drop_table('courses')
    op.drop_table('curricula')
    op.drop_table('programs')

    # 2. Academic terms & events
    op.drop_index('idx_fk_academic_events_term_id', table_name='academic_events')
    op.drop_table('academic_events')
    op.drop_table('academic_terms')

    # 1. Personas, MFA, sessions, emergency contacts & roles
    op.execute(sa.text("DROP TABLE IF EXISTS admin_audit_trails CASCADE;"))
    op.execute(sa.text("DROP TABLE IF EXISTS auth_audit_logs CASCADE;"))
    op.drop_index('idx_fk_emergency_contacts_user_id', table_name='emergency_contacts')
    op.drop_table('emergency_contacts')
    op.drop_index('idx_fk_user_active_sessions_user_id', table_name='user_active_sessions')
    op.drop_table('user_active_sessions')
    op.drop_index('idx_fk_user_mfa_recovery_codes_user_id', table_name='user_mfa_recovery_codes')
    op.drop_table('user_mfa_recovery_codes')
    op.drop_index('ix_user_mfa_credential_id', table_name='user_mfa_credentials')
    op.drop_index('idx_fk_user_mfa_credentials_user_id', table_name='user_mfa_credentials')
    op.drop_table('user_mfa_credentials')
    op.drop_table('faculty_profiles')
    op.drop_table('admin_profiles')
    op.drop_table('user_profiles')
    op.drop_table('user_permission_overrides')
    op.drop_index('idx_fk_user_roles_role_id', table_name='user_roles')
    op.drop_table('user_roles')
    op.drop_index('idx_fk_role_permissions_permission_id', table_name='role_permissions')
    op.drop_table('role_permissions')
    op.drop_table('roles')
