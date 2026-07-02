"""
tests/test_mfa.py — Unit tests for the TOTP MFA flow.
"""
import pytest
import pyotp
from unittest.mock import MagicMock, patch

from auth import (
    generate_totp_secret,
    get_totp_provisioning_uri,
    verify_totp,
    create_mfa_pending_token,
    decode_mfa_pending_token,
)


class TestTotpHelpers:
    def test_generate_totp_secret_is_base32(self):
        secret = generate_totp_secret()
        assert len(secret) >= 16
        # Valid Base32 characters only
        import base64
        base64.b32decode(secret)  # Raises if invalid

    def test_provisioning_uri_format(self):
        secret = generate_totp_secret()
        uri = get_totp_provisioning_uri("admin@example.com", secret)
        assert uri.startswith("otpauth://totp/")
        assert "Mindgate%20Policy%20Bot" in uri or "Mindgate Policy Bot" in uri
        assert "admin%40example.com" in uri or "admin@example.com" in uri

    def test_verify_totp_valid_code(self):
        secret = generate_totp_secret()
        totp = pyotp.TOTP(secret)
        valid_code = totp.now()

        mock_user = MagicMock()
        mock_user.totp_secret = secret

        assert verify_totp(mock_user, valid_code) is True

    def test_verify_totp_invalid_code(self):
        secret = generate_totp_secret()
        mock_user = MagicMock()
        mock_user.totp_secret = secret

        assert verify_totp(mock_user, "000000") is False

    def test_verify_totp_no_secret_returns_false(self):
        mock_user = MagicMock()
        mock_user.totp_secret = None

        assert verify_totp(mock_user, "123456") is False


class TestMfaPendingToken:
    def test_roundtrip_valid_token(self):
        """Creating and decoding an MFA pending token returns the correct email."""
        email = "admin@example.com"

        with patch.dict("os.environ", {"SECRET_KEY": "x" * 64}):
            token = create_mfa_pending_token(email)
            decoded_email = decode_mfa_pending_token(token)

        assert decoded_email == email

    def test_full_access_token_rejected_by_mfa_decoder(self):
        """A regular access token must not be accepted as an MFA token."""
        from fastapi import HTTPException
        from auth import create_access_token

        with patch.dict("os.environ", {"SECRET_KEY": "x" * 64}):
            regular_token = create_access_token({"sub": "admin@example.com"})
            with pytest.raises(HTTPException) as exc_info:
                decode_mfa_pending_token(regular_token)

        assert exc_info.value.status_code == 401
        assert "Invalid MFA token" in exc_info.value.detail
