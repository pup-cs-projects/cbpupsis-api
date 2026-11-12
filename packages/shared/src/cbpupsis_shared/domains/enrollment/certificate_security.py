"""Purpose/issuance-bound authenticated encryption, independent of JWT secrets."""

import base64
import hashlib
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from cbpupsis_core.config import settings
from cbpupsis_shared.domains.enrollment.exceptions import CertificateUnavailable


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def crypt(value: str, certificate_id, purpose: str, *, decrypt=False) -> str:
    try:
        key = base64.b64decode(
            settings.cor_encryption_key.get_secret_value(),
            altchars=b"-_",
            validate=True,
        )
        if len(key) != 32:
            raise ValueError
        aad = f"cor:v1:{certificate_id}:{purpose}".encode()
        cipher = AESGCM(key)
        if decrypt:
            raw = base64.b64decode(value, altchars=b"-_", validate=True)
            return cipher.decrypt(raw[:12], raw[12:], aad).decode("utf-8")
        nonce = secrets.token_bytes(12)
        return base64.urlsafe_b64encode(
            nonce + cipher.encrypt(nonce, value.encode("utf-8"), aad)
        ).decode("ascii")
    except (ValueError, TypeError, AttributeError, InvalidTag, UnicodeError):
        raise CertificateUnavailable from None
