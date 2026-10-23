"""Snake_case student contracts, with no Google credentials in responses."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TermSelection(Contract):
    """Omit both selectors for the current term; otherwise supply both, not blanks."""

    year: str | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        description="Stored academic year; pair with semester or omit both.",
        examples=["2026-2027"],
    )
    semester: str | None = Field(
        default=None,
        min_length=1,
        max_length=30,
        description="Stored semester identifier, not a numeric position.",
        examples=["first"],
    )


class OAuthCallback(Contract):
    state: SecretStr = Field(
        min_length=32,
        max_length=128,
        description="Single-use state from this session's browser consent redirect.",
    )
    code: SecretStr = Field(
        min_length=1,
        max_length=2048,
        description="Short-lived Google authorization code; never log or persist it.",
    )


class AuthorizationRead(Contract):
    authorization_url: str = Field(
        description="Open in a browser; contains private state and must not be logged."
    )
    expires_in: int = Field(default=600, description="Attempt lifetime, in seconds.")


class CalendarTargetRead(Contract):
    year: str
    semester: str
    provisioned: bool = Field(description="A calendar was assigned, not proof of sync.")


class ConnectionRead(Contract):
    enabled: bool
    connected: bool = Field(default=False, description="Usable refresh grant is saved.")
    disconnecting: bool = Field(
        default=False, description="Google token revocation is still pending."
    )
    last_sync_at: datetime | None = None
    next_sync_at: datetime | None = None
    last_error: str | None = None
    targets: list[CalendarTargetRead] = Field(
        default_factory=list,
        description="Pinned terms; empty after disconnect, with no Google IDs exposed.",
    )
