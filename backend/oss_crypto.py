"""Fernet-based symmetric encryption for OSS access key secrets.

Each OssMonitor row carries its own AK/SK pair (no global default), and
the secret is stored as Fernet ciphertext in oss_monitors.access_key_secret_enc.

Key resolution priority (zero-config friendly):
  1. OSS_ENC_KEY env var          (operator-supplied, highest priority)
  2. File at OSS_ENC_KEY_FILE      (default /app/data/oss_fernet.key)
  3. Auto-generate + persist       (creates the file with mode 0600, logs an info line)

The Fernet instance is cached at module level so the key file is only
read once per process.
"""
import os
import logging
import pathlib
from cryptography.fernet import Fernet, InvalidToken

_KEY_ENV = "OSS_ENC_KEY"
_KEY_FILE_ENV = "OSS_ENC_KEY_FILE"
_DEFAULT_KEY_FILE = "/app/data/oss_fernet.key"

_cached_fernet: Fernet | None = None


def _resolve_key() -> bytes:
    """Resolve the Fernet key bytes."""
    env_key = os.getenv(_KEY_ENV)
    if env_key:
        return env_key.encode() if isinstance(env_key, str) else env_key

    key_file = os.getenv(_KEY_FILE_ENV, _DEFAULT_KEY_FILE)
    p = pathlib.Path(key_file)

    if p.exists():
        return p.read_bytes().strip()

    # Auto-generate and persist. Best-effort chmod; on some volume mounts
    # (Windows, certain FUSE) chmod may not be supported and is non-fatal.
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        p.write_bytes(key)
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass
        logging.info(
            f"[oss_crypto] Generated and persisted Fernet key to {key_file}. "
            f"This key will survive container restarts. "
            f"Set { _KEY_ENV } env var to override."
        )
        return key
    except OSError as e:
        raise RuntimeError(
            f"[oss_crypto] { _KEY_ENV } not set, and cannot write key file "
            f"{key_file}: {e}. Either set { _KEY_ENV } env var, or mount a "
            f"writable volume at {p.parent} (or change { _KEY_FILE_ENV })."
        ) from e


def get_fernet() -> Fernet:
    global _cached_fernet
    if _cached_fernet is None:
        _cached_fernet = Fernet(_resolve_key())
    return _cached_fernet


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
