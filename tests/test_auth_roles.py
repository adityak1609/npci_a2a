from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from security.auth import authenticate_user_for_role


def test_standard_user_authentication_helper_accepts_standard_user():
    user = SimpleNamespace(is_admin=False)
    with patch("security.auth.authenticate_user", return_value=user):
        assert authenticate_user_for_role(
            None, "user@example.com", "secret", require_admin=False
        ) is user


def test_admin_login_accepts_admin():
    admin = SimpleNamespace(is_admin=True)
    with patch("security.auth.authenticate_user", return_value=admin):
        assert authenticate_user_for_role(
            None, "admin@example.com", "secret", require_admin=True
        ) is admin


@pytest.mark.parametrize(
    ("is_admin", "require_admin", "message"),
    [
        (True, False, "must use Admin Login"),
        (False, True, "does not have administrator access"),
    ],
)
def test_role_mismatch_is_rejected(is_admin, require_admin, message):
    account = SimpleNamespace(is_admin=is_admin)
    with patch("security.auth.authenticate_user", return_value=account):
        with pytest.raises(HTTPException) as exc:
            authenticate_user_for_role(
                None, "person@example.com", "secret", require_admin=require_admin
            )

    assert exc.value.status_code == 403
    assert message in exc.value.detail


def test_admin_login_rejects_standard_user():
    user = SimpleNamespace(is_admin=False)
    with patch("security.auth.authenticate_user", return_value=user):
        with pytest.raises(HTTPException) as exc:
            authenticate_user_for_role(
                None, "user@example.com", "secret", require_admin=True
            )

    assert exc.value.status_code == 403
