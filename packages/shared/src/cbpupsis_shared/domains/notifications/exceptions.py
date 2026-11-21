"""Errors the notifications domain raises.

See ``cbpupsis_api_student.domains.items.exceptions`` for why each domain
owns its errors and why they share a base class.
"""

from __future__ import annotations

import uuid

from cbpupsis_core.exceptions import AppError, NotFoundError, ValidationError


class NotificationsError(AppError):
    """Base for every error raised by the notifications domain."""


class NotificationNotFoundError(NotificationsError, NotFoundError):
    """No notification with this id (HTTP 404).

    Also raised when the notification belongs to somebody else. 404 rather than
    403 for the same reason as items: a 403 confirms the id exists, which turns
    the endpoint into an oracle for how many notifications other people have.
    """

    def __init__(self, notification_id: uuid.UUID) -> None:
        super().__init__(f"Notification {notification_id} not found")


class UnknownNotificationTypeError(NotificationsError, ValidationError):
    """A preference referenced a type the application does not define (422).

    Silently accepting it would store a preference that suppresses nothing, and
    the user would believe they had turned something off.
    """

    def __init__(self, notification_type: str) -> None:
        super().__init__(f"Unknown notification type: {notification_type!r}")


class MandatoryNotificationError(NotificationsError, ValidationError):
    """An attempt to disable a notification that may not be switched off (422).

    Security notices — a password change, an account deactivation — are the
    signal that surfaces an account takeover to its victim. A user (or an
    attacker holding their session) must not be able to silence them.
    """

    def __init__(self, notification_type: str) -> None:
        super().__init__(
            f"{notification_type!r} is a security notice and cannot be disabled"
        )
