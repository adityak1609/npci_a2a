"""Tests for password policy and the SHA-256-prehashed bcrypt scheme."""

import bcrypt
import pytest
from fastapi import HTTPException

from security.auth import (
    _PREHASH_PREFIX,
    MAX_PASSWORD_LENGTH,
    hash_password,
    validate_password_strength,
    verify_password,
)


def test_rejects_too_short_password():
    with pytest.raises(HTTPException) as exc:
        validate_password_strength("Ab1!xyz")  # 7 chars
    assert exc.value.status_code == 400


def test_rejects_low_complexity_password():
    with pytest.raises(HTTPException):
        validate_password_strength("alllowercaseletters")  # one class only


def test_rejects_overlong_password():
    with pytest.raises(HTTPException):
        validate_password_strength("Aa1!" + "x" * (MAX_PASSWORD_LENGTH + 1))


def test_accepts_strong_password():
    # three of four classes is enough
    validate_password_strength("G00dPassw0rd!x")


def test_hash_is_versioned_and_roundtrips():
    hashed = hash_password("G00dPassw0rd!x")
    assert hashed.startswith(_PREHASH_PREFIX)
    assert verify_password("G00dPassw0rd!x", hashed)
    assert not verify_password("wrong-password", hashed)


def test_long_password_beyond_bcrypt_72_bytes_uses_full_entropy():
    """Two long passwords differing only after byte 72 must NOT collide.

    Raw bcrypt truncates at 72 bytes, so both would verify against either hash.
    The SHA-256 pre-hash makes the whole password contribute.
    """
    base = "Str0ng!Passphrase-" + "a" * 70
    p1 = base + "AAAA"
    p2 = base + "BBBB"
    h1 = hash_password(p1)
    assert verify_password(p1, h1)
    assert not verify_password(p2, h1)


def test_legacy_raw_bcrypt_hash_still_verifies():
    legacy = bcrypt.hashpw(b"OldPassw0rd!", bcrypt.gensalt()).decode("utf-8")
    assert not legacy.startswith(_PREHASH_PREFIX)
    assert verify_password("OldPassw0rd!", legacy)
    assert not verify_password("nope", legacy)


def test_verify_password_is_safe_on_garbage_hash():
    assert not verify_password("whatever", "not-a-real-bcrypt-hash")
