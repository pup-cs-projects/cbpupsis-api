"""Fixed values for the Admin sign-in domain."""

from __future__ import annotations

#: The Admin panel's access-token cookie. It carries the short-lived access
#: token for browser navigation under /api/v1/admin; the refresh token uses the
#: shared ``refresh_token`` cookie like every other role.
ADMIN_SESSION_COOKIE = "admin_session"
ADMIN_SESSION_COOKIE_PATH = "/api/v1/admin"
