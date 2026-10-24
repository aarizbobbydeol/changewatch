import base64
import logging
import os
from pathlib import Path
from typing import Dict, Optional, Tuple, Union
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from app.config import settings
from app.db import get_connection

logger = logging.getLogger(__name__)

CURRENT_VERSION = b"\x01"       # 1 byte
DEFAULT_KEY_ID = b"\x00\x01"     # 2 bytes

# In-memory cache of decrypted project data keys: project_id -> (data_key, cache_time)
_project_key_cache: Dict[int, bytes] = {}


class DecryptionError(Exception):
    """Raised when ciphertext cannot be decrypted or authentication tag fails."""
    pass


class MasterKeyMissingError(Exception):
    """Raised when a valid master key is not provided in production."""
    pass


def generate_master_key() -> str:
    """Generate a new 256-bit master key encoded as a URL-safe base64 string."""
    raw_key = AESGCM.generate_key(bit_length=256)
    return base64.urlsafe_b64encode(raw_key).decode("ascii")


def _parse_key(key_str: str) -> bytes:
    """Parse base64 or hex encoded key string into 32 bytes."""
    key_str = key_str.strip()
    if not key_str:
        return b""
    # Try URL-safe base64
    try:
        raw = base64.urlsafe_b64decode(key_str)
        if len(raw) == 32:
            return raw
    except Exception:
        pass
    # Try standard base64
    try:
        raw = base64.b64decode(key_str)
        if len(raw) == 32:
            return raw
    except Exception:
        pass
    # Try hex
    try:
        raw = bytes.fromhex(key_str)
        if len(raw) == 32:
            return raw
    except Exception:
        pass
    # If raw 32 bytes ASCII
    raw = key_str.encode("utf-8")
    if len(raw) == 32:
        return raw

    raise ValueError("Master key must be a valid 256-bit (32 byte) base64 or hex string")


def get_master_key_bytes() -> bytes:
    """Retrieve the primary master key from environment or file."""
    if settings.MASTER_KEY:
        return _parse_key(settings.MASTER_KEY)

    if settings.MASTER_KEY_FILE:
        path = Path(settings.MASTER_KEY_FILE)
        if path.exists():
            return _parse_key(path.read_text(encoding="utf-8"))

    # In dev or test mode, generate a deterministic fallback key if not specified
    if settings.SEED_DEV_PROJECT or os.getenv("PYTEST_CURRENT_TEST"):
        logger.debug("Using default dev master key for testing")
        return b"\x01" * 32

    raise MasterKeyMissingError(
        "No MASTER_KEY or MASTER_KEY_FILE configured. ChangeWatch refuses to start without encryption master key."
    )


def get_previous_master_key_bytes() -> Optional[bytes]:
    """Retrieve previous master key for rotation fallback if configured."""
    if settings.MASTER_KEY_PREVIOUS:
        try:
            return _parse_key(settings.MASTER_KEY_PREVIOUS)
        except Exception as exc:
            logger.warning("Failed parsing MASTER_KEY_PREVIOUS: %s", exc)
    return None


def encrypt_data_key(data_key: bytes, master_key: bytes) -> bytes:
    """
    Encrypt a 256-bit project data key with a master key.
    Format: version(1 byte) | nonce(12 bytes) | ciphertext+tag
    """
    aesgcm = AESGCM(master_key)
    nonce = os.urandom(12)
    # AAD binds key encryption to data key context
    aad = b"changewatch.envelope.datakey"
    ciphertext = aesgcm.encrypt(nonce, data_key, aad)
    return CURRENT_VERSION + nonce + ciphertext


def decrypt_data_key(data_key_enc: bytes, master_key: bytes) -> bytes:
    """Decrypt an encrypted project data key using the given master key."""
    if len(data_key_enc) < 13:
        raise DecryptionError("Encrypted data key is too short")

    version = data_key_enc[:1]
    if version != CURRENT_VERSION:
        raise DecryptionError(f"Unsupported data key envelope version: {version!r}")

    nonce = data_key_enc[1:13]
    ciphertext = data_key_enc[13:]
    aad = b"changewatch.envelope.datakey"

    aesgcm = AESGCM(master_key)
    try:
        return aesgcm.decrypt(nonce, ciphertext, aad)
    except InvalidTag as err:
        raise DecryptionError("Failed to decrypt data key: invalid authentication tag") from err


async def get_or_create_project_data_key(project_id: int) -> bytes:
    """
    Retrieve project data key, decrypting from projects.data_key_enc.
    If no data key exists yet, generates a new one, encrypts it, and saves it.
    """
    if project_id in _project_key_cache:
        return _project_key_cache[project_id]

    master_key = get_master_key_bytes()
    prev_master_key = get_previous_master_key_bytes()

    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT data_key_enc FROM projects WHERE id = $1",
            project_id,
        )
        if not row:
            raise DecryptionError(f"Project {project_id} does not exist")

        data_key_enc = row["data_key_enc"]

        if data_key_enc:
            # Attempt decryption with current master key
            try:
                data_key = decrypt_data_key(data_key_enc, master_key)
            except DecryptionError:
                # Fallback to previous master key if configured
                if prev_master_key:
                    logger.info("Attempting data key decryption with MASTER_KEY_PREVIOUS for project %d", project_id)
                    data_key = decrypt_data_key(data_key_enc, prev_master_key)
                else:
                    raise
        else:
            # Generate new 256-bit data key
            data_key = AESGCM.generate_key(bit_length=256)
            new_data_key_enc = encrypt_data_key(data_key, master_key)
            await conn.execute(
                "UPDATE projects SET data_key_enc = $1 WHERE id = $2",
                new_data_key_enc,
                project_id,
            )

    _project_key_cache[project_id] = data_key
    return data_key


def invalidate_project_data_key_cache(project_id: Optional[int] = None):
    """Clear cached data key for a project or all projects."""
    if project_id is not None:
        _project_key_cache.pop(project_id, None)
    else:
        _project_key_cache.clear()


async def encrypt(
    project_id: int,
    table: str,
    column: str,
    plaintext: Union[str, bytes],
) -> bytes:
    """
    Encrypt plaintext using AES-256-GCM envelope encryption.
    Ciphertext format:
      version(1 byte) | key_id(2 bytes) | nonce(12 bytes) | ciphertext+tag
    Associated data (AAD):
      "{table}.{column}.{project_id}"
    """
    if plaintext is None:
        return b""

    if isinstance(plaintext, str):
        payload_bytes = plaintext.encode("utf-8")
    else:
        payload_bytes = plaintext

    data_key = await get_or_create_project_data_key(project_id)
    aesgcm = AESGCM(data_key)
    nonce = os.urandom(12)
    aad = f"{table}.{column}.{project_id}".encode("utf-8")

    ciphertext_and_tag = aesgcm.encrypt(nonce, payload_bytes, aad)

    return CURRENT_VERSION + DEFAULT_KEY_ID + nonce + ciphertext_and_tag


async def decrypt_bytes(
    project_id: int,
    table: str,
    column: str,
    ciphertext: bytes,
) -> bytes:
    """
    Decrypt ciphertext using project data key and verify AAD binding.
    Returns decrypted raw bytes.
    """
    if not ciphertext:
        return b""

    # Minimum size: 1 (version) + 2 (key_id) + 12 (nonce) + 16 (tag) = 31 bytes
    if len(ciphertext) < 31:
        raise DecryptionError("Ciphertext payload is too short")

    version = ciphertext[:1]
    if version != CURRENT_VERSION:
        raise DecryptionError(f"Unsupported ciphertext envelope version: {version!r}")

    key_id = ciphertext[1:3]
    nonce = ciphertext[3:15]
    payload = ciphertext[15:]
    aad = f"{table}.{column}.{project_id}".encode("utf-8")

    data_key = await get_or_create_project_data_key(project_id)
    aesgcm = AESGCM(data_key)

    try:
        return aesgcm.decrypt(nonce, payload, aad)
    except InvalidTag as err:
        raise DecryptionError(
            f"Decryption failed for {table}.{column} (project {project_id}): authentication tag mismatch or corrupted data"
        ) from err


async def decrypt(
    project_id: int,
    table: str,
    column: str,
    ciphertext: bytes,
) -> str:
    """Decrypt ciphertext and decode to UTF-8 string."""
    raw_bytes = await decrypt_bytes(project_id, table, column, ciphertext)
    return raw_bytes.decode("utf-8")


async def encrypt_or_keep(
    project_id: int,
    table: str,
    column: str,
    plaintext: Optional[str],
) -> Tuple[Optional[str], Optional[bytes]]:
    """Prepare a sensitive value for storage as (plaintext_column, encrypted_column).

    Normally only the ciphertext is stored. If encryption fails (for example no master
    key), the plaintext is kept and a warning logged, so data is never silently lost.
    """
    if not plaintext:
        return None, None
    try:
        return None, await encrypt(project_id, table, column, plaintext)
    except Exception as exc:
        logger.warning("Storing %s.%s unencrypted for project %d: %s", table, column, project_id, exc)
        return plaintext, None


async def decrypt_or_plain(
    project_id: int,
    table: str,
    column: str,
    ciphertext: Optional[bytes],
    plaintext: Optional[str] = None,
) -> Optional[str]:
    """Read a sensitive value written by encrypt_or_keep (or a legacy plaintext row)."""
    if ciphertext:
        try:
            return await decrypt(project_id, table, column, ciphertext)
        except Exception as exc:
            logger.warning("Could not decrypt %s.%s for project %d: %s", table, column, project_id, exc)
    return plaintext
