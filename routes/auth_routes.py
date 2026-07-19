"""Authentication and login routes."""

import logging
import os
import secrets as token_secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.config import env_bool, env_int
from core.metrics import inc_counter
from database import AuditLog, get_db
from security.auth import (
    AUTH_COOKIE_NAME,
    Token,
    User,
    UserLogin,
    authenticate_user,
    authenticate_user_for_role,
    create_access_token,
    create_mfa_pending_token,
    decode_mfa_pending_token,
    generate_totp_secret,
    get_current_admin,
    get_current_user,
    get_totp_provisioning_uri,
    revoke_token,
    verify_totp,
)
from security.rate_limit import check_login_lockout, check_rate_limit, record_login_failure

router = APIRouter()
logger = logging.getLogger(__name__)
_metrics_bearer = HTTPBearer(auto_error=False)

def _write_audit_log(db: Session, admin_id: Optional[int], action: str, target: str = None, detail: str = None) -> None:
    try:
        entry = AuditLog(admin_id=admin_id, action=action, target=target, detail=detail)
        db.add(entry)
        db.commit()
    except Exception as e:
        logger.exception("Failed to write auth audit log", exc_info=e)


def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        max_age=env_int("ACCESS_TOKEN_EXPIRE_MINUTES", 60) * 60,
        httponly=True,
        secure=env_bool("AUTH_COOKIE_SECURE", default=False),
        samesite="strict",
        path="/",
    )


def _clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(
        key=AUTH_COOKIE_NAME,
        httponly=True,
        secure=env_bool("AUTH_COOKIE_SECURE", default=False),
        samesite="strict",
        path="/",
    )


def _login_response(user: User, response: Response) -> Token:
    """Create the shared token response returned by all login routes."""
    token = create_access_token({"sub": user.email})
    _set_auth_cookie(response, token)
    return Token(
        access_token=token,
        user_email=user.email,
        user_name=user.full_name,
        is_admin=user.is_admin,
    )


@router.post("/auth/admin/login", tags=["Auth"])
def admin_login(
    data: UserLogin,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Authenticate an administrator account. If MFA is enabled, returns an mfa_token instead."""
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit("login_ip", client_ip, limit=env_int("LOGIN_RATE_LIMIT_PER_MINUTE", 10))
    check_rate_limit("login_email", data.email.lower(), limit=env_int("LOGIN_EMAIL_RATE_LIMIT_PER_MINUTE", 5))
    check_login_lockout(data.email)
    try:
        user = authenticate_user_for_role(
            db, data.email, data.password, require_admin=True
        )
        # --- MFA gate ---
        if user.mfa_enabled:
            # Return a short-lived pending token — not a full access token
            mfa_token = create_mfa_pending_token(user.email)
            _write_audit_log(db, user.id, "login_mfa_required", target="admin", detail=client_ip)
            return {"mfa_required": True, "mfa_token": mfa_token}
        # No MFA configured — issue full token directly
        _write_audit_log(db, user.id, "login", target="admin", detail=client_ip)
        return _login_response(user, response)
    except HTTPException:
        inc_counter("login_failures_total", {"route": "admin"})
        record_login_failure(data.email)
        _write_audit_log(db, None, "login_failed", target="admin", detail=data.email)
        raise
    except Exception as e:
        logger.exception("Internal error in admin_login", exc_info=e)
        raise HTTPException(status_code=500, detail="Admin login failed.")


@router.post("/auth/login/session", tags=["Auth"])
def session_login(
    data: UserLogin,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Authenticate any active account (admin or portal user) for the shared SPA.

    Role-agnostic counterpart to /auth/admin/login: both administrators and
    non-admin bank product managers sign in here. The MFA gate still applies to
    any account that has enabled it (in practice, admins).
    """
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit("login_ip", client_ip, limit=env_int("LOGIN_RATE_LIMIT_PER_MINUTE", 10))
    check_rate_limit("login_email", data.email.lower(), limit=env_int("LOGIN_EMAIL_RATE_LIMIT_PER_MINUTE", 5))
    check_login_lockout(data.email)
    try:
        user = authenticate_user(db, data.email, data.password)
        # --- MFA gate --- (returns a short-lived pending token, not a session)
        if user.mfa_enabled:
            mfa_token = create_mfa_pending_token(user.email)
            _write_audit_log(db, user.id, "login_mfa_required", target="session", detail=client_ip)
            return {"mfa_required": True, "mfa_token": mfa_token}
        _write_audit_log(db, user.id, "login", target="session", detail=client_ip)
        return _login_response(user, response)
    except HTTPException:
        inc_counter("login_failures_total", {"route": "session"})
        record_login_failure(data.email)
        _write_audit_log(db, None, "login_failed", target="session", detail=data.email)
        raise
    except Exception as e:
        logger.exception("Internal error in session_login", exc_info=e)
        raise HTTPException(status_code=500, detail="Login failed.")


@router.post("/auth/login", response_model=Token, tags=["Auth"], deprecated=True)
def login(
    data: UserLogin,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Legacy login route retained for existing admin API clients."""
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit("login_ip", client_ip, limit=env_int("LOGIN_RATE_LIMIT_PER_MINUTE", 10))
    check_rate_limit("login_email", data.email.lower(), limit=env_int("LOGIN_EMAIL_RATE_LIMIT_PER_MINUTE", 5))
    check_login_lockout(data.email)
    try:
        user = authenticate_user_for_role(
            db, data.email, data.password, require_admin=True
        )
        _write_audit_log(db, user.id, "login", target="legacy", detail=client_ip)
        return _login_response(user, response)
    except HTTPException:
        inc_counter("login_failures_total", {"route": "legacy"})
        record_login_failure(data.email)
        _write_audit_log(db, None, "login_failed", target="legacy", detail=data.email)
        raise
    except Exception as e:
        logger.exception("Internal error in legacy_login", exc_info=e)
        raise HTTPException(status_code=500, detail="Login failed.")


@router.get("/auth/me", tags=["Auth"])
def auth_me(current_user: User = Depends(get_current_user)):
    """Return the current profile for cookie-backed SPA auth (admin or portal user)."""
    return {
        "id": current_user.id,
        "email": current_user.email,
        "full_name": current_user.full_name,
        "is_admin": current_user.is_admin,
    }


@router.post("/auth/logout", tags=["Auth"])
def auth_logout(
    request: Request,
    response: Response,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False)),
    db: Session = Depends(get_db),
):
    """Clear the browser auth cookie."""
    token = credentials.credentials if credentials else request.cookies.get(AUTH_COOKIE_NAME)
    if token:
        try:
            revoke_token(db, token, reason="logout")
            _write_audit_log(db, None, "logout", target="token", detail="revoked")
        except HTTPException:
            pass
    _clear_auth_cookie(response)
    return {"logged_out": True}


def require_metrics_access(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_metrics_bearer),
    db: Session = Depends(get_db),
):
    """Allow metrics scraping by token, or by an authenticated admin fallback."""
    metrics_token = os.getenv("METRICS_BEARER_TOKEN", "").strip()
    if (
        metrics_token
        and credentials
        and token_secrets.compare_digest(credentials.credentials, metrics_token)
    ):
        return True
    return get_current_admin(request, credentials, db)

@router.post("/auth/refresh", response_model=Token, tags=["Auth"])
def auth_refresh(
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Rotate the current valid access token for a new one.
    This supports the 15-minute strict session expiry by allowing active SPAs to refresh.
    """
    # Create a new token for the currently authenticated user
    new_token = create_access_token({"sub": current_user.email})
    _set_auth_cookie(response, new_token)

    _write_audit_log(db, current_user.id, "token_refresh", target="token")

    return Token(
        access_token=new_token,
        user_email=current_user.email,
        user_name=current_user.full_name,
        is_admin=current_user.is_admin,
    )


# ---------------------------------------------------------------------------
# MFA Endpoints
# ---------------------------------------------------------------------------

class MfaVerifyRequest(BaseModel):
    mfa_token: str
    code: str


class MfaSetupResponse(BaseModel):
    totp_uri: str
    secret: str  # Shown ONCE; user must save this


class MfaEnableRequest(BaseModel):
    code: str  # Confirm TOTP code before enabling


@router.post("/auth/mfa/verify", response_model=Token, tags=["Auth - MFA"])
def mfa_verify(
    data: MfaVerifyRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    """
    Complete MFA login. Exchanges an mfa_token + TOTP code for a full access token.
    The mfa_token is the short-lived token returned by /auth/admin/login when MFA is required.
    """
    email = decode_mfa_pending_token(data.mfa_token)
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found.")

    if not verify_totp(user, data.code):
        inc_counter("login_failures_total", {"route": "mfa"})
        record_login_failure(email)
        _write_audit_log(db, user.id, "mfa_failed", target="totp")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired TOTP code.",
        )

    _write_audit_log(db, user.id, "login_mfa_success", target="admin")
    return _login_response(user, response)


@router.post("/auth/mfa/setup", response_model=MfaSetupResponse, tags=["Auth - MFA"])
def mfa_setup(
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """
    Generate a new TOTP secret for the current admin. Returns the otpauth:// URI
    for scanning with Google Authenticator / Authy.

    MFA is NOT enabled until /auth/mfa/enable is called with a valid TOTP code.
    """
    secret = generate_totp_secret()
    current_user.totp_secret = secret
    db.commit()
    _write_audit_log(db, current_user.id, "mfa_setup_initiated", target="totp")
    return MfaSetupResponse(
        totp_uri=get_totp_provisioning_uri(current_user.email, secret),
        secret=secret,
    )


@router.post("/auth/mfa/enable", tags=["Auth - MFA"])
def mfa_enable(
    data: MfaEnableRequest,
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """
    Confirm a TOTP code and activate MFA for the current admin account.
    Must be called after /auth/mfa/setup with a valid TOTP code to prove the
    Authenticator app was configured correctly.
    """
    if not current_user.totp_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA not set up. Call /auth/mfa/setup first.",
        )
    if not verify_totp(current_user, data.code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid TOTP code. Ensure your Authenticator app is correctly configured.",
        )
    current_user.mfa_enabled = True
    db.commit()
    _write_audit_log(db, current_user.id, "mfa_enabled", target="totp")
    return {"mfa_enabled": True, "message": "MFA is now active for your account."}

