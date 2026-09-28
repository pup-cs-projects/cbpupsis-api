"""Application configuration.

All settings are read from the environment (via ``.env`` locally, or injected by
the platform in staging/production). This module is the single place secrets and
configuration are read; import ``settings`` elsewhere rather than reading the
environment directly.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # --- One-time tokens (email verification, password reset) ---
    #: Verification links are low-risk and mailed once, so a day is generous
    #: enough to survive a delayed inbox without leaving the link useful for long.
    email_verification_ttl_hours: int = 24
    #: Reset links change a credential, so they expire far sooner: the window in
    #: which a forwarded or logged link is dangerous should be minutes, not days.
    password_reset_ttl_minutes: int = 30

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
    #: Where the counters live. ``memory://`` keeps them in the process, which
    #: means the limit is enforced **per replica**: with N replicas behind a load
    #: balancer a caller effectively gets N times the configured allowance, and
    #: counters reset on every deploy. That is fine for a single instance and for
    #: local development. A multi-replica deployment that needs the limit to be
    #: global must point this at shared storage — ``redis://host:6379`` — which
    #: is a configuration change only, no code change.
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
    rate_limit_forgot_password: str = "3/hour"
    #: Credential stuffing is the threat here, not a forgetful user. Ten
    #: attempts a minute is far above what a human retyping a password needs and
    #: far below what makes an online guessing attack worthwhile.
    rate_limit_login: str = "10/minute"
    #: Signup spam: enough for a shared office NAT to register a few accounts,
    #: too few to script thousands.
    rate_limit_register: str = "5/hour"

    # --- Email (AWS SES) ---
    #: SES region. Only consulted when ``ses_from_email`` is set.
    aws_region: str = "us-east-1"
    #: Verified SES identity messages are sent from. Leaving this unset is the
    #: switch that selects the console sender, so development and tests never
    #: need AWS credentials and can never send real mail.
    ses_from_email: str | None = None
    #: Optional SES configuration set, for bounce/complaint event publishing.
    ses_configuration_set: str | None = None
    #: Base URL of the frontend that hosts the verification and reset pages.
    #: The emailed link points here, not at the API, because a human clicks it.
    frontend_base_url: str = "http://localhost:3000"

    @property
    def email_enabled(self) -> bool:
        """Whether a real SES sender should be used.

        Expressed once here so the fallback rule — no verified sender identity
        means no real mail — cannot be applied inconsistently.
        """
        return self.ses_from_email is not None

    @property
    def docs_enabled(self) -> bool:
        """Whether to expose interactive API documentation.

        Development only. Docs advertise every endpoint, schema, and example in
        the app — reconnaissance you need not hand an attacker. Expressed once
        here so a new docs UI cannot be mounted past the gate by accident.
        """
        return self.environment == "development"

    @field_validator("jwt_secret")
    @classmethod
    def _reject_weak_secret(cls, value: SecretStr) -> SecretStr:
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


@lru_cache
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance."""
    return Settings()


settings = get_settings()
