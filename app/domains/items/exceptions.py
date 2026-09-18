"""Errors the items domain raises — the reference shape for a domain's errors.

Each error names a failure this domain can have, and pins the status and message
in one place. Callers raise ``ItemNotFoundError(item_id)`` rather than repeating
``NotFoundError(f"Item {item_id} not found")`` at every site, so the copy the
frontend renders cannot drift between two raises of the same condition.

They subclass the core hierarchy (``NotFoundError`` and friends), so the
registered handlers, the shared envelope, and the optional ``code`` field all
keep working untouched — this adds naming, not a second error system.

**Why the ``ItemsError`` base.** Nothing catches per-domain today, and that is
the point: it is the seam for the extraction ``client.py`` exists to make cheap.
When this domain moves behind HTTP, its client has to translate a 404 response
back into a local failure, and a caller wanting "any items failure" needs one
except clause that does not have to be revisited every time a subclass is added.
Adding a shared base afterwards means editing every raise site; adding it now
costs one line.
"""

from __future__ import annotations

import uuid

from app.core.exceptions import AppError, ForbiddenError, NotFoundError


class ItemsError(AppError):
    """Base for every error raised by the items domain.

    Not raised directly. It exists so one ``except ItemsError`` catches anything
    this domain can fail with, including subclasses added later.
    """


class ItemNotFoundError(ItemsError, NotFoundError):
    """No live item with this id (HTTP 404).

    Also raised when the caller may not know the item exists: 404 rather than
    403 keeps the endpoint from confirming an id, which would make it an
    enumeration oracle.
    """

    def __init__(self, item_id: uuid.UUID) -> None:
        super().__init__(f"Item {item_id} not found")


class ItemAccessDeniedError(ItemsError, ForbiddenError):
    """The caller neither owns the item nor holds the moderation permission
    (HTTP 403).

    Distinct from :class:`ItemNotFoundError` on purpose: this is for a resource
    the caller is allowed to *see* but not act on. Where seeing it is itself
    privileged, raise the 404 instead.
    """

    def __init__(self) -> None:
        super().__init__("You do not have access to this item")
