"""Application configuration.

All settings are read from the environment (via ``.env`` locally, or injected by
the platform in staging/production). This module is the single place secrets and
configuration are read; import ``settings`` elsewhere rather than reading the
environment directly. Deployed, the secrets themselves come from AWS Secrets
Manager and are copied into the environment first (:func:`load_deployed_secrets`).
"""

from __future__ import annotations

import json
import os
from collections.abc import MutableMapping
from functools import lru_cache
from typing import Any, Literal, Protocol

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Names the deployed secret: set on every Lambda function (by cbpupsis-infra) to
#: the ARN of one Secrets Manager secret, a JSON object such as
#: ``{"DATABASE_URL": "...", "JWT_SECRET": "..."}``. Unset locally and in tests,
#: where nothing is fetched.
APP_SECRETS_ARN = "APP_SECRETS_ARN"

#: Where each role's portal runs locally, matching the dev stack's ports.
DEV_FRONTEND_URLS: dict[str, str] = {
    "student": "http://localhost:3000",
    "faculty": "http://localhost:3001",
    "admin": "http://localhost:3002",
    "superadmin": "http://localhost:3003",
}


class Settings(BaseSettings):
    """Typed application settings, validated at startup."""

    # The app always runs in a container, and compose passes env/<app>/env.<stage> in as
    # real environment variables — which pydantic-settings reads before any file.
    # env_file is the admin file, so a host-side script (a migration, a seed) picks up
    # the same values without duplicating them. There is deliberately no `.env`
    # fallback: two places to look for config is how a value gets changed in one
    # of them and nobody can explain why the app disagrees.
    model_config = SettingsConfigDict(env_file="env/admin/env.dev", extra="ignore")

    # --- Application ---
    app_name: str = "CBPUPSIS API"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    #: Origins allowed to call this API from a browser. Never "*" in production
    #: when credentials are involved — the browser rejects that combination.
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # --- Logging ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    #: JSON for machines (deployed), plain text for humans (local). Log
    #: aggregators query fields; a formatted string is not queryable.
    log_json: bool = False

    # --- Database (Neon) ---
    #: Pooled connection string (host contains ``-pooler``); used by the app.
    database_url: str
    #: Direct (non-pooled) connection string; used by Alembic migrations.
    #: Neon's PgBouncer runs in transaction mode, so a connection returns to the
    #: pool at each transaction boundary and session state does not survive:
    #: ``SET``, session-level advisory locks, and temporary tables are all lost.
    #: Alembic relies on those. (Protocol-level prepared statements — what
    #: asyncpg actually uses — *are* supported by the pooler, which is why the
    #: app's own traffic is fine on the pooled URL.)
    direct_database_url: str | None = None
    #: Restricted API/worker login; never the migration owner.
    audit_runtime_role: str | None = None

    # --- Connection pool ---
    #: Connections held open per worker process. The arithmetic that matters is
    #: ``workers x (pool_size + max_overflow) <= the database connection limit``
    #: — exceeded, the extra workers fail at connect time under load, which is
    #: exactly when you need them. See env.example for the worked example.
    #:
    #: 5 rather than SQLAlchemy's default of 5+10: the default is not wrong, but
    #: leaving it implicit means nobody does the multiplication until an outage.
    db_pool_size: int = 5
    #: Extra connections opened above ``db_pool_size`` under burst, closed again
    #: when returned. Counts against the database limit while open.
    db_max_overflow: int = 10
    #: Recycle a connection after this many seconds. Defends against a
    #: connection killed silently by a proxy, load balancer, or Neon idle timeout
    #: — the failure mode is a stale socket that only errors on first use.
    #: Under any infrastructure idle timeout, which is why it is well under an
    #: hour by default.
    db_pool_recycle_seconds: int = 1800

    # --- Authentication ---
    #: Secret used to sign JWTs. Required: there is deliberately no default, so
    #: a misconfigured deployment fails at startup rather than signing tokens
    #: with a value an attacker could guess from this repository.
    #:
    #: SecretStr renders as "**********" in repr() and logs, so dumping the
    #: settings object during debugging cannot leak the signing key. Read the
    #: real value with settings.jwt_secret.get_secret_value().
    jwt_secret: SecretStr
    jwt_algorithm: str = "HS256"
    #: Short by design — a leaked access token stays useful only this long.
    access_token_ttl_minutes: int = 15
    #: Long-lived but single-use: rotated on every refresh (see auth.service).
    refresh_token_ttl_days: int = 30
    #: Every role's session ends after this much inactivity, server-side.
    session_idle_timeout_minutes: int = Field(default=15, ge=1)

    # --- Sign-in MFA and lockout (every role) ---
    #: Short-lived and non-privileged: a challenge proves only that the password
    #: step completed, never that the holder may call a protected route.
    mfa_challenge_ttl_minutes: int = 5
    #: Failed password or MFA steps per lockout subject before a temporary lock.
    mfa_lockout_threshold: int = 5
    mfa_lockout_window_minutes: int = 15
    mfa_lockout_minutes: int = 15
    #: A dedicated production value is recommended. When omitted, a separate
    #: 256-bit sub-key is derived from JWT_SECRET so local/test setups remain
    #: operable without weakening AES-256-GCM.
    mfa_encryption_key: SecretStr | None = None
    webauthn_rp_id: str = "admin.cbpupsis"
    webauthn_origin: str = "https://admin.cbpupsis"

    # --- One-time tokens (email verification, password reset) ---
    #: Verification links are low-risk and mailed once, so a day is generous
    #: enough to survive a delayed inbox without leaving the link useful for long.
    email_verification_ttl_hours: int = 24
    #: Configurable, with the story's default lifetime of 24 hours.
    password_reset_ttl_minutes: int = Field(default=1440, ge=1)
    bcrypt_rounds: int = Field(default=12, ge=12, le=16)
    #: Shared-store address limits apply equally to known and unknown addresses.
    password_reset_request_limit: int = Field(default=3, ge=1)
    password_reset_request_window_seconds: int = Field(default=3600, ge=1)

    # --- Superadmin-created accounts ---
    #: How long a system-generated temporary password can still sign in.
    temporary_password_ttl_days: int = Field(default=7, ge=1)
    #: CSV import bounds. Every row costs one bcrypt hash (about 37 ms each on 8
    #: threads at 12 rounds), so 500 rows is roughly 20 seconds of hashing in one
    #: request; 5000 would be minutes and outlive any gateway timeout.
    user_import_max_rows: int = Field(default=500, ge=1)
    user_import_max_bytes: int = Field(default=2 * 1024 * 1024, ge=1)

    # --- Outbox (durable side effects) ---
    #: How long the worker waits between claim queries when nothing wakes it.
    #: This is a FLOOR, not the primary trigger: the worker is woken by
    #: LISTEN/NOTIFY, and this interval exists so a missed notification — or a
    #: LISTEN connection silently dropped by a firewall or NAT — is noticed
    #: within a bounded time rather than never. Short values are expensive
    #: against a remote database: an empty claim query measured ~47ms against
    #: Neon, so a 1-second interval is ~68 minutes of database time per day
    #: doing nothing, and it prevents scale-to-zero entirely.
    outbox_poll_interval_seconds: float = 30.0
    #: Optional unpooled URL using the restricted runtime role, never the owner.
    outbox_listener_database_url: str | None = None
    #: Messages claimed per tick. Larger batches amortise the query; too large
    #: and one slow message delays every other in the batch.
    outbox_batch_size: int = 50
    #: Attempts before a message is dead-lettered (status ``failed``). With the
    #: backoff below this is roughly a day of retries — long enough to ride out
    #: an SES outage, short enough that a poison message stops burning attempts.
    outbox_max_attempts: int = 8
    #: First retry delay; doubles each attempt.
    outbox_backoff_base_seconds: int = 30
    #: Ceiling on the doubling. Without it the eighth retry is days away, long
    #: after anyone still wants the message delivered.
    outbox_backoff_cap_seconds: int = 3600

    # --- Retention (the pruning job) ---
    #: How long a delivered outbox message is kept. Long enough to investigate
    #: "did that notification actually go out last week?", short enough that the
    #: table does not become the largest thing in the database. The delivery
    #: receipt outlives it and is the lasting record.
    outbox_dispatched_retention_days: int = 7
    #: How long a dead-lettered message is kept. Longer, because a failed row is
    #: the only evidence of what went wrong — but not forever, or nobody ever
    #: looks and it silently becomes permanent storage.
    outbox_failed_retention_days: int = 30
    #: Receipts must outlive the messages they guard: deleting one while its
    #: message is still pending re-opens the duplicate-send window.
    delivery_receipt_retention_days: int = 60
    #: How long a read notification stays in the bell. The bell is not an
    #: archive; unread ones are never pruned regardless of age.
    notification_read_retention_days: int = 90
    #: Rows deleted per statement. Bounded because an unbounded DELETE takes a
    #: long-lived lock and can time out on exactly the table large enough to
    #: need pruning in the first place.
    retention_batch_size: int = 5000

    # --- Rate limiting ---
    #: Master switch. On everywhere real traffic lands; the test suite turns it
    #: off, because one limiter shared across a session carries counters between
    #: tests and makes them order-dependent — the whole suite then fails
    #: depending on which test ran first.
    rate_limit_enabled: bool = True
    #: Where the counters live. ``memory://`` keeps them in the process, so the
    #: limit is enforced **per instance**: on Lambda every concurrent instance
    #: has its own counters, which reset on every cold start, so a caller gets
    #: the allowance many times over. Fine for local development and tests;
    #: production must use shared storage (``redis://host:6379``), and settings
    #: validation refuses to start production on ``memory://``.
    rate_limit_storage_uri: str = "memory://"
    #: Per-endpoint limits, in slowapi/limits syntax ("5/minute", "100/hour").
    #: Settings rather than literals in the decorators so an operator can retune
    #: them under an attack without a code deploy.
    #:
    #: The two that send mail on demand are strictest: each request costs money
    #: and puts a message in someone else's inbox, so an unauthenticated caller
    #: can otherwise use them to mail-bomb a third party at your domain's
    #: reputation.
    rate_limit_resend_verification: str = "3/hour"
    rate_limit_forgot_password: str = "60/minute"
    #: Credential stuffing is the threat here, not a forgetful user. Ten
    #: attempts a minute is far above what a human retyping a password needs and
    #: far below what makes an online guessing attack worthwhile.
    rate_limit_login: str = "10/minute"

    # --- Email (AWS SES) ---
    #: SES region. Only consulted when ``ses_from_email`` is set.
    aws_region: str = "us-east-1"
    #: Verified SES identity messages are sent from. Leaving this unset is the
    #: switch that selects the console sender, so development and tests never
    #: need AWS credentials and can never send real mail.
    ses_from_email: str | None = None
    #: Optional SES configuration set, for bounce/complaint event publishing.
    ses_configuration_set: str | None = None
    #: SMTP host of a local mail catcher (Mailpit in the compose stack). Set, it
    #: replaces both the console sender and SES, so every message, temporary
    #: passwords and links included, lands whole in the catcher's inbox instead
    #: of a real one. Development only: settings refuse it anywhere else.
    dev_smtp_host: str | None = None
    dev_smtp_port: int = 1025
    #: Base URL of the frontend that hosts the verification and reset pages.
    #: The emailed link points here, not at the API, because a human clicks it.
    #: Now the fallback: each role's portal has its own key below.
    frontend_base_url: str = "http://localhost:3000"
    #: One portal per role. Unset, a role falls back to ``frontend_base_url``
    #: when that is configured, else to its local dev port (see
    #: :meth:`frontend_url_for`).
    frontend_base_url_student: str | None = None
    frontend_base_url_faculty: str | None = None
    frontend_base_url_admin: str | None = None
    frontend_base_url_superadmin: str | None = None

    @property
    def email_enabled(self) -> bool:
        """Whether a real SES sender should be used.

        Expressed once here so the fallback rule — no verified sender identity
        means no real mail — cannot be applied inconsistently.
        """
        return self.ses_from_email is not None

    def frontend_url_for(self, role: str | None) -> str:
        """Return the base URL of the portal an emailed link for ``role`` opens.

        A role's own key wins. Without it, an explicitly set
        ``FRONTEND_BASE_URL`` wins over the dev port, so an env file written
        before the per-role keys existed keeps sending every link where it did.
        ``None`` or an unknown role (an account in no role group) gets the
        fallback.
        """
        configured = {
            "student": self.frontend_base_url_student,
            "faculty": self.frontend_base_url_faculty,
            "admin": self.frontend_base_url_admin,
            "superadmin": self.frontend_base_url_superadmin,
        }.get(role or "")
        if configured:
            return configured
        if "frontend_base_url" in self.model_fields_set:
            return self.frontend_base_url
        return DEV_FRONTEND_URLS.get(role or "", self.frontend_base_url)

    @property
    def docs_enabled(self) -> bool:
        """Whether to expose interactive API documentation.

        Development only. Docs advertise every endpoint, schema, and example in
        the app — reconnaissance you need not hand an attacker. Expressed once
        here so a new docs UI cannot be mounted past the gate by accident.
        """
        return self.environment == "development"

    @field_validator("dev_smtp_host", mode="before")
    @classmethod
    def blank_mail_catcher_is_off(cls, value: object) -> object:
        """``DEV_SMTP_HOST=`` means no catcher, so a file or test can switch it off."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("jwt_secret")
    @classmethod
    def reject_weak_secret(cls, value: SecretStr) -> SecretStr:
        """Reject secrets too short to resist offline brute-forcing.

        HS256 security rests entirely on this value; a 12-character secret is
        crackable, so the check runs at startup rather than being left to a
        code review that may never happen.
        """
        if len(value.get_secret_value()) < 32:
            raise ValueError(
                "jwt_secret must be at least 32 characters. "
                'Generate one with: python -c "import secrets; '
                'print(secrets.token_urlsafe(48))"'
            )
        return value

    @model_validator(mode="after")
    def keep_the_mail_catcher_in_development(self) -> Settings:
        """Refuse a mail catcher outside development.

        It takes precedence over SES, so in staging or production it would
        silently swallow every real email, password resets included.
        """
        if self.dev_smtp_host is not None and self.environment != "development":
            raise ValueError(
                "dev_smtp_host is for development only; unset it in "
                f"{self.environment} and configure ses_from_email instead."
            )
        return self

    @model_validator(mode="after")
    def require_shared_rate_limit_storage(self) -> Settings:
        """Refuse production with per-instance rate-limit counters."""
        if self.environment == "production" and self.rate_limit_storage_uri.startswith(
            "memory://"
        ):
            raise ValueError(
                "rate_limit_storage_uri must be shared storage in production "
                "(for example redis://host:6379), not memory://: each instance "
                "would keep its own counters."
            )
        return self


class SecretsClient(Protocol):
    """The one Secrets Manager call the loader makes (a boto3 client fits)."""

    def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:
        """Return the secret, with its text under ``SecretString``."""
        ...


def load_deployed_secrets(
    environ: MutableMapping[str, str] = os.environ,
    client: SecretsClient | None = None,
) -> list[str]:
    """Copy the deployed secret into the environment, before :class:`Settings` reads it.

    Deployed, secrets are not environment variables: the function is given only
    :data:`APP_SECRETS_ARN`, and anyone who can read a Lambda's configuration
    would otherwise read the database password too. This turns the secret back
    into environment variables inside the process, so :class:`Settings` stays
    the one place configuration is read and needs no AWS-specific source.

    A variable that is already set wins over the secret's copy, the same way an
    explicit value wins everywhere else in configuration.

    Fails loudly: with :data:`APP_SECRETS_ARN` set, a secret that cannot be
    fetched or parsed stops startup rather than letting the app run half
    configured. No error message ever includes a secret's value.

    Returns the names of the variables it set, never their values.
    """
    arn = environ.get(APP_SECRETS_ARN)
    if not arn:
        return []

    if client is None:
        # Imported lazily so only a deployed process, which has a secret to
        # fetch, needs boto3 and AWS credentials; tests and local runs never
        # import it (the same reasoning as the SES sender).
        import boto3  # noqa: PLC0415 - see the comment above

        client = boto3.client("secretsmanager")

    text = client.get_secret_value(SecretId=arn)["SecretString"]
    try:
        secret = json.loads(text)
    except json.JSONDecodeError:
        # `from None`: the decode error carries the whole secret text.
        raise ValueError(
            f"The secret named by {APP_SECRETS_ARN} is not JSON."
        ) from None
    if not isinstance(secret, dict) or not all(
        isinstance(value, str) for value in secret.values()
    ):
        raise ValueError(
            f"The secret named by {APP_SECRETS_ARN} must be a JSON object of "
            "string values."
        )

    loaded: list[str] = []
    for name, value in secret.items():
        if name not in environ:
            environ[name] = value
            loaded.append(name)
    return loaded


@lru_cache
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance, loading deployed secrets first."""
    load_deployed_secrets()
    return Settings()


settings = get_settings()
