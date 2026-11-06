"""Render an event into the words a user reads.

Separate from the channels so every channel shows the same text, and so a
template failure happens once per message rather than once per channel.

Plain strings in a dict, in the spirit of ``core/emails/templates.py``: a
template engine would be a dependency and a file-loading concern for a handful
of fixed messages.
"""

from __future__ import annotations

from cbpupsis_core.events import Event
from cbpupsis_shared.domains.notifications.channels import RenderedNotification

#: event name -> (title, body). A type absent here falls back to a generic
#: message rather than raising: a missing template must not stop a security
#: notice from going out, and an ugly notification beats a silent one.
MESSAGES_BY_EVENT: dict[str, tuple[str, str]] = {
    "enrollment.waiver_decided": (
        "Your waiver request was reviewed",
        "A decision was recorded on your prerequisite waiver request.",
    ),
    "item.created": (
        "New item created",
        "An item you own was created successfully.",
    ),
    "user.deactivated": (
        "Your account was deactivated",
        "Your account has been deactivated. If this was not you, contact support "
        "immediately.",
    ),
    "user.reactivated": (
        "Your account was reactivated",
        "Your account has been reactivated and you can sign in again.",
    ),
    "auth.password_changed": (
        "Your password was changed",
        "Your password was just changed. If this was not you, reset your "
        "password immediately and contact support.",
    ),
    "auth.account_locked": (
        "Your account was temporarily locked",
        "Your account was locked for 15 minutes after repeated failed sign-in "
        "attempts. If this was not you, contact support immediately.",
    ),
    "notifications.digest": (
        "Your periodic summary",
        "Here is what happened since your last summary.",
    ),
    "enrollment.confirmed": (
        "Enrollment submitted",
        "Your selected course sections have been reserved and are pending "
        "finalization.",
    ),
    "enrollment.overload_decided": (
        "Overload request decided",
        "A College Dean has decided your overload request.",
    ),
}


def render(event: Event) -> RenderedNotification:
    """Return the title and body for ``event``."""
    title, body = MESSAGES_BY_EVENT.get(
        event.name, ("Notification", f"Something happened: {event.name}")
    )
    if event.name == "enrollment.overload_decided":
        state = str(event.payload.get("state", "DECIDED")).replace("_", " ").title()
        remarks = event.payload.get("remarks")
        title = f"Overload request {state.lower()}"
        body = f"Your overload request was {state.lower()}."
        if remarks:
            body += f" Remarks: {remarks}"
    return RenderedNotification(
        notification_type=event.name,
        title=title,
        body=body,
        payload=dict(event.payload),
    )
