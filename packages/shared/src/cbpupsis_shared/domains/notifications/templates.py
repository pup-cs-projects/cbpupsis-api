"""Render an event into the words a user reads.

Separate from the channels so every channel shows the same text, and so a
template failure happens once per message rather than once per channel.

Plain functions over a dict, in the spirit of ``core/email_templates.py``: a
template engine would be a dependency and a file-loading concern for what is
currently three strings.
"""

from __future__ import annotations

from cbpupsis_core.events import Event
from cbpupsis_shared.domains.notifications.channels import RenderedNotification


def _item_created(event: Event) -> tuple[str, str]:
    return ("New item created", "An item you own was created successfully.")


def _user_deactivated(event: Event) -> tuple[str, str]:
    return (
        "Your account was deactivated",
        "Your account has been deactivated. If this was not you, contact support "
        "immediately.",
    )


def _user_reactivated(event: Event) -> tuple[str, str]:
    return (
        "Your account was reactivated",
        "Your account has been reactivated and you can sign in again.",
    )


def _password_changed(event: Event) -> tuple[str, str]:
    return (
        "Your password was changed",
        "Your password was just changed. If this was not you, reset your "
        "password immediately and contact support.",
    )


def _account_locked(event: Event) -> tuple[str, str]:
    return (
        "Your account was temporarily locked",
        "Your account was locked for 15 minutes after repeated failed sign-in "
        "attempts. If this was not you, contact support immediately.",
    )


def _digest(event: Event) -> tuple[str, str]:
    return (
        "Your periodic summary",
        "Here is what happened since your last summary.",
    )


#: event name -> renderer. A type absent here falls back to a generic message
#: rather than raising: a missing template must not stop a security notice from
#: going out, and an ugly notification beats a silent one.
_RENDERERS = {
    "item.created": _item_created,
    "user.deactivated": _user_deactivated,
    "user.reactivated": _user_reactivated,
    "auth.password_changed": _password_changed,
    "auth.account_locked": _account_locked,
    "notifications.digest": _digest,
}


def render(event: Event) -> RenderedNotification:
    """Return the title and body for ``event``."""
    renderer = _RENDERERS.get(event.name)
    if renderer is None:
        title, body = ("Notification", f"Something happened: {event.name}")
    else:
        title, body = renderer(event)

    return RenderedNotification(
        notification_type=event.name,
        title=title,
        body=body,
        payload=dict(event.payload),
    )
