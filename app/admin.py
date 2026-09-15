"""SQLAdmin registrations — an auto-generated admin UI over the models.

Provides a Django-admin-style panel for inspecting and editing records. Each
``ModelView`` subclass registers one table. ``main.py`` calls
:func:`setup_admin` from inside its ``settings.docs_enabled`` gate, so the panel
exists only in development.

``column_list`` and its siblings are part of SQLAdmin's declarative API (class
attributes by design), so the ``RUF012`` mutable-default-attribute rule is not
meaningful here and is suppressed per line.
"""

from __future__ import annotations

import uuid

from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.config import settings
from app.database import AsyncSessionLocal, engine
from app.domains.auth.security import verify_password
from app.domains.iam import service as iam_service
from app.domains.iam.constants import MANAGE_IAM
from app.domains.iam.models import Group, Permission, Policy
from app.domains.items.models import Item
from app.domains.users import service as users_service
from app.domains.users.models import User

#: Key holding the authenticated admin's user id in the signed session cookie.
SESSION_USER_ID = "admin_user_id"


class AdminAuth(AuthenticationBackend):
    """Session-cookie login for the admin panel, gated on ``ManageIAM``.

    Runs its own :class:`AsyncSessionLocal` because SQLAdmin mounts a separate
    Starlette app: the ``get_db`` request dependency does not reach these
    handlers.
    """

    async def login(self, request: Request) -> bool:
        form = await request.form()
        email = str(form.get("username") or "").strip().lower()
        password = str(form.get("password") or "")
        if not email or not password:
            return False

        async with AsyncSessionLocal() as db:
            user = await users_service.get_by_email(db, email)
            # Verify before any other check so a missing account and a wrong
            # password cost the same work and give the same answer.
            if user is None or not verify_password(password, user.password_hash):
                return False
            if not user.is_active or user.deleted_at is not None:
                return False
            if not await self._may_administer(db, user.id):
                return False
            user_id = user.id

        # Only the id goes in the cookie; permissions are re-read per request.
        request.session.update({SESSION_USER_ID: str(user_id)})
        return True

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> Response | bool:
        """Re-check the session holder against the database on every request.

        The cookie carries identity only. Permissions are resolved from the
        database here, for the same reason they are kept out of the JWTs: a
        revoked group or a deactivated account must lock the panel immediately
        rather than at the next cookie expiry.
        """
        raw_id = request.session.get(SESSION_USER_ID)
        if not raw_id:
            return RedirectResponse(request.url_for("admin:login"), status_code=302)

        try:
            user_id = uuid.UUID(raw_id)
        except ValueError:
            request.session.clear()
            return RedirectResponse(request.url_for("admin:login"), status_code=302)

        async with AsyncSessionLocal() as db:
            user = await users_service.get_active_user(db, user_id)
            if user is None or not await self._may_administer(db, user_id):
                request.session.clear()
                return RedirectResponse(request.url_for("admin:login"), status_code=302)

        return True

    @staticmethod
    async def _may_administer(db: AsyncSession, user_id: uuid.UUID) -> bool:
        granted = await iam_service.get_effective_permissions(db, user_id)
        return MANAGE_IAM in granted


class UserAdmin(ModelView, model=User):
    """Admin view for users.

    The detail view and the edit form default to *every* mapped column, so
    ``password_hash`` has to be excluded from each explicitly — restricting
    ``column_list`` alone keeps the digest off the index page while still
    rendering it one click away.
    """

    column_list = [User.id, User.email, User.full_name, User.is_active]  # noqa: RUF012
    column_details_exclude_list = [User.password_hash]  # noqa: RUF012
    form_excluded_columns = [User.password_hash]  # noqa: RUF012
    column_export_exclude_list = [User.password_hash]  # noqa: RUF012


class ItemAdmin(ModelView, model=Item):
    """Admin view for items."""

    column_list = [Item.id, Item.name, Item.owner_id, Item.price]  # noqa: RUF012


class PermissionAdmin(ModelView, model=Permission):
    """Admin view for IAM permissions."""

    column_list = [Permission.id, Permission.action]  # noqa: RUF012


class PolicyAdmin(ModelView, model=Policy):
    """Admin view for IAM policies."""

    column_list = [Policy.id, Policy.name]  # noqa: RUF012


class GroupAdmin(ModelView, model=Group):
    """Admin view for IAM groups."""

    column_list = [Group.id, Group.name]  # noqa: RUF012


def setup_admin(app) -> None:
    """Attach the admin panel and all model views to ``app``.

    Call only behind the development gate: the panel edits records directly,
    bypassing every service-layer rule, so it must not exist in a deployed
    environment.
    """
    # Signing key for the session cookie. Validated at startup as >= 32 chars,
    # and constructing the backend is what installs SessionMiddleware -- on the
    # mounted admin app only, so gated environments gain no cookie.
    authentication_backend = AdminAuth(
        secret_key=settings.jwt_secret.get_secret_value()
    )
    admin = Admin(app, engine, authentication_backend=authentication_backend)
    admin.add_view(UserAdmin)
    admin.add_view(ItemAdmin)
    admin.add_view(PermissionAdmin)
    admin.add_view(PolicyAdmin)
    admin.add_view(GroupAdmin)
