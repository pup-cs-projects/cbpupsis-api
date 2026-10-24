"""Snake_case student contracts, with no Google credentials in responses."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TermSelection(Contract):
    """Send {} for the current configured term, or provide both stored selectors.

    Blank strings are invalid. This selects a term; it does not change SIS
    enrollment, term configuration or the student's Google account.
    """

    year: str | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        description="Stored academic year, not a calendar date. Pair with semester "
        "or omit both for the current configured term; do not send an empty string.",
        examples=["2026-2027"],
    )
    semester: str | None = Field(
        default=None,
        min_length=1,
        max_length=30,
        description="Stored semester identifier, such as first, not a numeric "
        "position. Supply with year or omit both; do not send an empty string.",
        examples=["first"],
    )


class OAuthCallback(Contract):
    """Forward only the new redirect's decoded state/code using the same SIS session.

    Each value must be URL-decoded once locally, not via an online decoder.
    Do not paste the entire redirect URL. Attempts expire after ten minutes and
    are single-use; never log, share or replay these values.
    """

    state: SecretStr = Field(
        min_length=32,
        max_length=128,
        description="Exact state from the new consent redirect, URL-decoded once. "
        "Bound to the student/session that called authorization; not a bearer token.",
    )
    code: SecretStr = Field(
        min_length=1,
        max_length=2048,
        description="Short-lived code from that same new Google redirect, decoded "
        "once. Not the whole URL or a SIS token. Never log, persist or replay it.",
    )


class AuthorizationRead(Contract):
    """Browser consent instructions, not a connection or delivery confirmation."""

    authorization_url: str = Field(
        description="Open in a browser and approve the requested Calendar access. "
        "Contains private single-use state; never log/share it or fetch it as "
        "a Scalar API request. Google redirects to the configured frontend URI."
    )
    expires_in: int = Field(default=600, description="Attempt lifetime, in seconds.")


class CalendarTargetRead(Contract):
    """One pinned academic term, with no Google calendar/event identifiers exposed."""

    year: str = Field(description="Stored academic year selected for synchronization.")
    semester: str = Field(description="Stored semester selected for synchronization.")
    provisioned: bool = Field(
        description="An app-created calendar was assigned to this term. "
        "Does not prove all meetings were delivered; inspect sync status and Google."
    )


class ConnectionRead(Contract):
    """Direct private/no-store status; no status/data wrapper or Google credentials."""

    enabled: bool = Field(
        description="Deployment-level Google integration flag, not student consent. "
        "Manual SIS schedule/PDF/ICS access remains separate."
    )
    connected: bool = Field(
        default=False,
        description="A refresh grant is saved and not marked reconnectRequired. "
        "Not proof of recent sync, current Google availability or event delivery.",
    )
    disconnecting: bool = Field(
        default=False,
        description="Google authorization revocation is pending. Keep the worker "
        "running and wait for false before beginning a fresh connection.",
    )
    last_sync_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the last successful reconciliation, including "
        "unchanged checks. May predate the current queued request; null before "
        "the first successful reconciliation. Not a Google-write timestamp.",
    )
    next_sync_at: datetime | None = Field(
        default=None,
        description="UTC due time for a worker attempt or revocation retry, not "
        "a completion deadline. Null when no attempt is scheduled, including "
        "completed disconnect or a terminal sync error.",
    )
    last_error: str | None = Field(
        default=None,
        description="Sanitized background error category, never Google payloads. "
        "reconnectRequired needs fresh consent; other terminal categories need "
        "operator review. Null alone does not prove a queued request completed.",
    )
    targets: list[CalendarTargetRead] = Field(
        default_factory=list,
        description="Pinned terms for this connection, up to ten. Empty after "
        "disconnect even when private same-account reconnect mappings remain. "
        "No Google calendar or event IDs are exposed.",
    )
