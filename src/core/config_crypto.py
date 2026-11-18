"""Symmetric encryption for the secrets kept in the database.

A secret never leaves this module in clear. It is sealed with AES-256-GCM, which
authenticates the ciphertext, and the associated data binds each ciphertext to
the field that owns it and to the key version that produced it. A ciphertext
copied into another row, or produced under another key, fails to open instead of
decrypting into a plausible wrong value.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: Version of the key in force. It travels with every ciphertext, so a later
#: rotation can tell which key opens which row.
CURRENT_KEY_VERSION = 1

#: Purpose of a ciphertext holding a configuration secret. The schema accepts
#: only this value in ``config_secrets.aad``.
CONFIG_SECRET_PURPOSE = "config_secret"

NONCE_BYTES = 12
KEY_BYTES = 32


class ConfigCryptoError(RuntimeError):
    """A secret could not be sealed or opened; the caller must not guess."""


class EncryptionKeyMissing(ConfigCryptoError):
    """No usable key, so every secret operation fails closed."""


@dataclass(frozen=True)
class SealedSecret:
    """One encrypted value, with everything needed to open it again."""

    ciphertext: bytes
    nonce: bytes
    key_version: int
    purpose: str


def associated_data(purpose: str, row_id: str, key_version: int) -> bytes:
    """Bind a ciphertext to its field and to its key version.

    Without this, a ciphertext lifted from another row decrypts happily and the
    application trusts a value that was never meant for that field.
    """
    return f"{purpose}:{row_id}:{key_version}".encode()


class SecretCipher:
    """Seals and opens secrets with one AES-256-GCM key."""

    def __init__(self, key: bytes, key_version: int = CURRENT_KEY_VERSION) -> None:
        if len(key) != KEY_BYTES:
            raise EncryptionKeyMissing(
                f"encryption key must be {KEY_BYTES} bytes, got {len(key)}"
            )
        self._aead = AESGCM(key)
        self.key_version = key_version

    @staticmethod
    def decode_key(raw: str) -> bytes:
        """Turn the base64 value of ``APP_ENCRYPTION_KEY`` into key bytes."""
        try:
            key = base64.b64decode(raw.strip(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise EncryptionKeyMissing("APP_ENCRYPTION_KEY is not valid base64") from exc
        if len(key) != KEY_BYTES:
            raise EncryptionKeyMissing(
                f"APP_ENCRYPTION_KEY must decode to {KEY_BYTES} bytes, got {len(key)}"
            )
        return key

    @classmethod
    def from_setting(cls, raw: str | None, key_version: int = CURRENT_KEY_VERSION) -> SecretCipher:
        if not raw:
            raise EncryptionKeyMissing(
                "APP_ENCRYPTION_KEY is not set; secrets cannot be stored or read"
            )
        return cls(cls.decode_key(raw), key_version)

    @classmethod
    def optional(cls, raw: str | None) -> SecretCipher | None:
        """Return a cipher, or ``None`` when no key is configured.

        ``None`` is not a fallback to plaintext: it is the state in which every
        secret operation refuses to run.
        """
        if not raw:
            return None
        return cls.from_setting(raw)

    def seal(self, plaintext: str, *, purpose: str, row_id: str) -> SealedSecret:
        nonce = os.urandom(NONCE_BYTES)
        aad = associated_data(purpose, row_id, self.key_version)
        ciphertext = self._aead.encrypt(nonce, plaintext.encode(), aad)
        return SealedSecret(
            ciphertext=ciphertext,
            nonce=nonce,
            key_version=self.key_version,
            purpose=purpose,
        )

    def open(self, sealed: SealedSecret, *, purpose: str, row_id: str) -> str:
        if sealed.key_version != self.key_version:
            raise ConfigCryptoError(
                f"secret {row_id} was sealed with key version {sealed.key_version}; "
                f"this build holds version {self.key_version}"
            )
        aad = associated_data(purpose, row_id, sealed.key_version)
        try:
            return self._aead.decrypt(sealed.nonce, sealed.ciphertext, aad).decode()
        except InvalidTag as exc:
            raise ConfigCryptoError(
                f"secret {row_id} failed authentication: wrong key, wrong field or tampered value"
            ) from exc


__all__ = [
    "CONFIG_SECRET_PURPOSE",
    "CURRENT_KEY_VERSION",
    "ConfigCryptoError",
    "EncryptionKeyMissing",
    "SealedSecret",
    "SecretCipher",
    "associated_data",
]
