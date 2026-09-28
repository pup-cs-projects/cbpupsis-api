"""Outbound email: the transport and the messages it carries.

Two concerns that change for unrelated reasons, so they are two modules::

    sender.py      the EmailSender protocol, the SES and console backends
    templates.py   the bodies — subject and text for each transactional email

Wording changes weekly and is reviewed by whoever owns the copy; transport
changes rarely and breaks everything when it is wrong. Keeping them apart means
editing a sentence can never touch send logic, and it is why ``sender.py`` has
no string a user ever reads.

It lives in ``core`` rather than a domain because several domains send mail —
``auth`` sends verification and reset messages, ``notifications`` sends digests
through the same protocol. Putting it inside one of them would make the other
depend on a feature it does not use.

Import from the package, not the submodules::

    from cbpupsis_core.emails import send_email, verification_email

**Sending never fails the caller's request** — see ``sender.py`` for why, and
what that costs.
"""

from __future__ import annotations

from cbpupsis_core.emails.sender import (
    ConsoleEmailSender,
    EmailSender,
    SESEmailSender,
    get_email_sender,
    send_email,
)
from cbpupsis_core.emails.templates import (
    PASSWORD_CHANGED_SUBJECT,
    PASSWORD_RESET_SUBJECT,
    VERIFICATION_SUBJECT,
    password_changed_email,
    password_reset_email,
    verification_email,
)

__all__ = [
    "PASSWORD_CHANGED_SUBJECT",
    "PASSWORD_RESET_SUBJECT",
    "VERIFICATION_SUBJECT",
    "ConsoleEmailSender",
    "EmailSender",
    "SESEmailSender",
    "get_email_sender",
    "password_changed_email",
    "password_reset_email",
    "send_email",
    "verification_email",
]
