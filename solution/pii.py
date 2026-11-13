"""PII protection.

* phone  -> HMAC-SHA256 (keyed, so the small phone space can't be brute-forced from the
            DB alone) used for matching, plus the last 4 digits for report masking.
* other PII fields (customer name) -> Fernet (AES + HMAC) encrypted JSON blob.

The secret comes from $LEDGER_PII_KEY. If unset, a random key is generated once and kept
in a 0600 file next to the database (never in the repo; gitignored) so that matching
stays stable across runs.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import ENV_PII_KEY, LedgerError


def _derive(master: bytes, label: bytes) -> bytes:
    return hmac.new(master, label, hashlib.sha256).digest()


class Pii:
    def __init__(self, master: bytes):
        self._hmac_key = _derive(master, b"ledger/phone-hmac/v1")
        self._fernet = Fernet(base64.urlsafe_b64encode(_derive(master, b"ledger/pii-enc/v1")))

    @property
    def fingerprint(self) -> str:
        """Identifies the key (not secret-revealing); stored to detect a wrong key later."""
        return hmac.new(self._hmac_key, b"fingerprint", hashlib.sha256).hexdigest()[:16]

    def phone_hash(self, canonical_phone: str) -> str:
        return hmac.digest(self._hmac_key, canonical_phone.encode(), "sha256").hex()

    @staticmethod
    def phone_last4(canonical_phone: str) -> str:
        return canonical_phone[-4:]

    @staticmethod
    def mask_phone(last4: str) -> str:
        return "+91******" + last4

    def encrypt_fields(self, values: dict) -> bytes:
        return self._fernet.encrypt(json.dumps(values, sort_keys=True).encode())

    def decrypt_fields(self, token: bytes) -> dict:
        try:
            return json.loads(self._fernet.decrypt(token))
        except InvalidToken as exc:
            raise LedgerError("cannot decrypt stored PII: wrong LEDGER_PII_KEY?") from exc


def load_pii(db_path: str) -> Pii:
    """Key from $LEDGER_PII_KEY, else the key file beside the DB (created on first use)."""
    env = os.environ.get(ENV_PII_KEY)
    if env:
        return Pii(hashlib.sha256(env.encode()).digest())
    if db_path == ":memory:":
        return Pii(os.urandom(32))
    key_path = Path(str(db_path) + ".key")
    if key_path.exists():
        return Pii(bytes.fromhex(key_path.read_text().strip()))
    key = os.urandom(32)
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key.hex())
    return Pii(key)
