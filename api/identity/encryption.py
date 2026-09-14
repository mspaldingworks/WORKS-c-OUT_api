"""
Symmetric encryption for secrets stored at rest — currently users' own LLM API
keys (identity.LLMCredential). Fernet (AES-128-CBC + HMAC) via `cryptography`.

The Fernet key is derived from `settings.FIELD_ENCRYPTION_KEY` (or `SECRET_KEY`
when that's unset) through SHA-256, so any string is accepted as key material
and dev needs no extra config. Prod should set a dedicated FIELD_ENCRYPTION_KEY
so rotating SECRET_KEY doesn't orphan stored ciphertext.
"""

import base64
import hashlib

from django.conf import settings
from cryptography.fernet import Fernet


def _fernet():
    source = getattr(settings, "FIELD_ENCRYPTION_KEY", "") or settings.SECRET_KEY
    material = base64.urlsafe_b64encode(hashlib.sha256(source.encode()).digest())
    return Fernet(material)


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
