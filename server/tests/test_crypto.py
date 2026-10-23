import base64
import os
from unittest.mock import AsyncMock, patch
import pytest
from app.crypto import (
    CURRENT_VERSION,
    DecryptionError,
    decrypt,
    decrypt_bytes,
    decrypt_data_key,
    encrypt,
    encrypt_data_key,
    generate_master_key,
    get_master_key_bytes,
    invalidate_project_data_key_cache,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@pytest.fixture(autouse=True)
def clean_crypto_cache():
    invalidate_project_data_key_cache()
    yield
    invalidate_project_data_key_cache()


def test_generate_master_key():
    key_str = generate_master_key()
    raw = base64.urlsafe_b64decode(key_str)
    assert len(raw) == 32


def test_data_key_envelope_wrap_and_unwrap():
    master_key = AESGCM.generate_key(bit_length=256)
    data_key = AESGCM.generate_key(bit_length=256)

    wrapped = encrypt_data_key(data_key, master_key)
    assert len(wrapped) > 32
    assert wrapped[:1] == CURRENT_VERSION

    unwrapped = decrypt_data_key(wrapped, master_key)
    assert unwrapped == data_key


def test_data_key_tamper_fails():
    master_key = AESGCM.generate_key(bit_length=256)
    data_key = AESGCM.generate_key(bit_length=256)

    wrapped = bytearray(encrypt_data_key(data_key, master_key))
    # Flip one byte in the tag/ciphertext
    wrapped[-1] ^= 0xFF

    with pytest.raises(DecryptionError):
        decrypt_data_key(bytes(wrapped), master_key)


@pytest.mark.asyncio
async def test_crypto_round_trip():
    fake_data_key = AESGCM.generate_key(bit_length=256)

    with patch("app.crypto.get_or_create_project_data_key", new_callable=AsyncMock) as mock_dk:
        mock_dk.return_value = fake_data_key

        plaintext = "Critical stack trace: ZeroDivisionError at line 42 in /app/main.py"
        ciphertext = await encrypt(
            project_id=1,
            table="events",
            column="stack_trace",
            plaintext=plaintext,
        )

        assert ciphertext != plaintext.encode("utf-8")
        assert len(ciphertext) > len(plaintext)
        assert ciphertext[:1] == CURRENT_VERSION

        decrypted = await decrypt(
            project_id=1,
            table="events",
            column="stack_trace",
            ciphertext=ciphertext,
        )
        assert decrypted == plaintext


@pytest.mark.asyncio
async def test_crypto_tamper_detection():
    fake_data_key = AESGCM.generate_key(bit_length=256)

    with patch("app.crypto.get_or_create_project_data_key", new_callable=AsyncMock) as mock_dk:
        mock_dk.return_value = fake_data_key

        ciphertext = bytearray(await encrypt(
            project_id=1,
            table="events",
            column="stack_trace",
            plaintext="secret database password",
        ))

        # Tamper with the last byte (authentication tag)
        ciphertext[-1] ^= 0xAA

        with pytest.raises(DecryptionError):
            await decrypt(
                project_id=1,
                table="events",
                column="stack_trace",
                ciphertext=bytes(ciphertext),
            )


@pytest.mark.asyncio
async def test_crypto_aad_context_binding():
    """Ensure ciphertext cannot be swapped between projects, tables, or columns."""
    fake_data_key = AESGCM.generate_key(bit_length=256)

    with patch("app.crypto.get_or_create_project_data_key", new_callable=AsyncMock) as mock_dk:
        mock_dk.return_value = fake_data_key

        ciphertext = await encrypt(
            project_id=1,
            table="events",
            column="stack_trace",
            plaintext="sensitive info",
        )

        # 1. Swapping project_id must fail
        with pytest.raises(DecryptionError):
            await decrypt(project_id=2, table="events", column="stack_trace", ciphertext=ciphertext)

        # 2. Swapping column must fail
        with pytest.raises(DecryptionError):
            await decrypt(project_id=1, table="events", column="extra", ciphertext=ciphertext)

        # 3. Swapping table must fail
        with pytest.raises(DecryptionError):
            await decrypt(project_id=1, table="deploys", column="diff", ciphertext=ciphertext)


@pytest.mark.asyncio
async def test_master_key_rotation_preserves_data():
    """Re-wrapping the project data key with a new master key allows decrypting old data."""
    old_master_key = AESGCM.generate_key(bit_length=256)
    new_master_key = AESGCM.generate_key(bit_length=256)
    project_data_key = AESGCM.generate_key(bit_length=256)

    # Wrap with old master key
    wrapped_old = encrypt_data_key(project_data_key, old_master_key)

    # Re-wrap with new master key (rotation)
    unwrapped = decrypt_data_key(wrapped_old, old_master_key)
    wrapped_new = encrypt_data_key(unwrapped, new_master_key)

    # Encrypt data with the data key
    with patch("app.crypto.get_or_create_project_data_key", new_callable=AsyncMock) as mock_dk:
        mock_dk.return_value = project_data_key
        ct = await encrypt(1, "deploys", "diff", "important git diff")

    # Decrypt unwrapped data key with new master key
    recovered_data_key = decrypt_data_key(wrapped_new, new_master_key)
    assert recovered_data_key == project_data_key

    # Decrypt data using the recovered key
    with patch("app.crypto.get_or_create_project_data_key", new_callable=AsyncMock) as mock_dk:
        mock_dk.return_value = recovered_data_key
        res = await decrypt(1, "deploys", "diff", ct)
        assert res == "important git diff"


@pytest.mark.asyncio
async def test_crypto_shredding_makes_data_unrecoverable():
    """Deleting the project data key makes the ciphertext permanently unrecoverable."""
    original_data_key = AESGCM.generate_key(bit_length=256)

    with patch("app.crypto.get_or_create_project_data_key", new_callable=AsyncMock) as mock_dk:
        mock_dk.return_value = original_data_key
        ct = await encrypt(1, "events", "stack_trace", "Traceback in user code")

    # Simulate crypto-shred: data key deleted, replacement key generated
    different_key = AESGCM.generate_key(bit_length=256)
    with patch("app.crypto.get_or_create_project_data_key", new_callable=AsyncMock) as mock_dk:
        mock_dk.return_value = different_key
        with pytest.raises(DecryptionError):
            await decrypt(1, "events", "stack_trace", ct)
