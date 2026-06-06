"""Fernet-based symmetric encryption for OSS access key secrets.

Each OssMonitor row carries its own AK/SK pair (no global default), and
the secret is stored as Fernet ciphertext in oss_monitors.access_key_secret_enc.
The encryption key lives in env var OSS_ENC_KEY; on first start without
the env var a fresh key is generated and a warning is logged telling the
operator to persist it to .env. With the ephemeral key, secrets can be
decrypted in-process but won't survive a restart — this is intentional
fail-loud behavior.
"""
import os
import logging
from cryptography.fernet import Fernet


_KEY_ENV = "OSS_ENC_KEY"
_warned = False


def get_fernet() -> Fernet:
    global _warned
    key = os.getenv(_KEY_ENV)
    if not key:
        key = Fernet.generate_key().decode()
        if not _warned:
            logging.warning(
                f"[oss_crypto] {_KEY_ENV} not set, generated an ephemeral key. "
                f"Persisted OSS monitor secrets will be unreadable on restart. "
                f"Set {_KEY_ENV}={key} in your .env to make it permanent."
            )
            _warned = True
        return Fernet(key.encode())
    if isinstance(key, str):
        key = key.encode()
    return Fernet(key)


def encrypt_secret(plain: str) -> str:
    if plain is None:
        return None
    return get_fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_secret(cipher: str) -> str:
    if cipher is None:
        return None
    return get_fernet().decrypt(cipher.encode("ascii")).decode("utf-8")


def mask_secret(plain: str) -> str:
    """Return a non-reversible preview like '***abcd' for API responses."""
    if not plain:
        return ""
    if len(plain) <= 4:
        return "***"
    return "***" + plain[-4:]
