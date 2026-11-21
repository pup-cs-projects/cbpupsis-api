"""Purpose-bound encryption for stored tokens and short-lived OAuth material."""

import base64
import hashlib
import secrets
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from cbpupsis_api_student.domains.google_calendar.exceptions import CalendarUnavailable
from cbpupsis_core.config import settings


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def crypt(value: str, student_id: uuid.UUID, purpose: str, *, decrypt=False) -> str:
    try:
        key = base64.urlsafe_b64decode(
            settings.google_calendar_encryption_key.get_secret_value()
        )
        aad = f"google-calendar:{student_id}:{purpose}".encode()
        if decrypt:
            raw = base64.urlsafe_b64decode(value)
            return AESGCM(key).decrypt(raw[:12], raw[12:], aad).decode()
        nonce = secrets.token_bytes(12)
        return base64.urlsafe_b64encode(
            nonce + AESGCM(key).encrypt(nonce, value.encode(), aad)
        ).decode()
    except (ValueError, TypeError, AttributeError, InvalidTag, UnicodeError):
        raise CalendarUnavailable from None
