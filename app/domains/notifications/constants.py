"""Fixed values for the notifications domain.

Permission actions are constants because a typo in a literal at a call site 403s
for every user, silently. As an import it fails at startup instead.

Lengths are shared by models.py and schemas.py: a schema admitting more than the
column holds is a 500 at INSERT.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

#: Reading and clearing your OWN notifications needs no permission — it is what
#: having an account means, the same way ``GET /users/me`` is not gated. Only
#: crossing ownership does, so these are the only two actions here.
READ_ALL_NOTIFICATION = "ReadAllNotification"
MANAGE_NOTIFICATION_DELIVERY = "ManageNotificationDelivery"

TITLE_MAX_LENGTH = 200
BODY_MAX_LENGTH = 2000
TYPE_MAX_LENGTH = 100


class Channel(StrEnum):
    """Where a notification can be delivered.

    A ``StrEnum`` so it compares equal to the strings stored in preference rows
    and delivery receipts, without those columns needing a database enum that
    would make adding a channel a migration.
    """

    EMAIL = "email"
    IN_APP = "in_app"
    # PUSH and SMS are deliberately absent. Adding one is a new Channel
    # implementation plus an entry here — see channels.py.


@dataclass(frozen=True)
class NotificationTypeSpec:
    """What a notification type is and how it is delivered by default."""

    #: Channels used when the user has expressed no preference. Absence of a
    #: preference row means "the default", so a type added later applies
    #: immediately to every existing user without a backfill.
    default_channels: tuple[Channel, ...]
    #: Whether a user may switch it off. **False for security notices**: a
    #: password-changed alert is the signal that surfaces an account takeover to
    #: its victim, so it must not be silenceable. Encoding that as data next to
    #: the channel list stops an opt-out UI from accidentally covering it.
    optional: bool = True


#: Every notification type the application understands, and how it is delivered.
#:
#: The dict is the source of truth for both the channel default and whether the
#: type is opt-outable — the same "declare it as data" approach the IAM seed
#: takes with permissions.
NOTIFICATION_TYPES: dict[str, NotificationTypeSpec] = {
    "item.created": NotificationTypeSpec(default_channels=(Channel.IN_APP,)),
    "user.deactivated": NotificationTypeSpec(
        default_channels=(Channel.EMAIL, Channel.IN_APP), optional=False
    ),
    "user.reactivated": NotificationTypeSpec(
        default_channels=(Channel.EMAIL, Channel.IN_APP), optional=False
    ),
    # The signal that surfaces an account takeover to its victim, so it is not
    # opt-outable and never will be.
    "auth.password_changed": NotificationTypeSpec(
        default_channels=(Channel.EMAIL, Channel.IN_APP), optional=False
    ),
    #: The periodic summary. Optional, unlike the security notices — a digest
    #: is a convenience, and a user who does not want one should be able to say
    #: so.
    "notifications.digest": NotificationTypeSpec(default_channels=(Channel.EMAIL,)),
}

#: The outbox event names this domain handles. Registered by subscribers.py, and
#: kept explicit rather than "everything" for the same reason AUDITED_EVENTS is:
#: notifying on an event nobody chose to notify on is how users get spammed.
NOTIFIED_EVENTS: tuple[str, ...] = tuple(NOTIFICATION_TYPES)
