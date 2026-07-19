"""
security/auth.py — JWT authentication helpers for Mindgate Policy Bot.

Provides:
    - Password hashing and verification (bcrypt directly)
    - JWT token creation and decoding (python-jose)
    - FastAPI dependency `get_current_user` for protected routes
    - Pydantic schemas for register / login requests
"""

import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import pyotp
from dotenv import load_dotenv
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from core.secrets_loader import load_secret_files
from database import RevokedSession, User, get_db

load_dotenv()
load_secret_files()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
AUTH_PRIVATE_KEY = os.getenv("AUTH_PRIVATE_KEY")
AUTH_PUBLIC_KEY = os.getenv("AUTH_PUBLIC_KEY")
SECRET_KEY = os.getenv("SECRET_KEY")

if AUTH_PRIVATE_KEY and AUTH_PUBLIC_KEY:
    ALGORITHM = "RS256"
elif SECRET_KEY:
    ALGORITHM = "HS256"
else:
    raise OSError(
        "No JWT keys configured. Set AUTH_PRIVATE_KEY and AUTH_PUBLIC_KEY for RS256, "
        "or SECRET_KEY for legacy HS256."
    )
# 15-minute default: highly restrictive session expiry.
# above 60 minutes without a refresh-token flow is highly discouraged.
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "15"))
AUTH_COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "mg_access_token")

# ---------------------------------------------------------------------------
# Password policy
# ---------------------------------------------------------------------------
# bcrypt only hashes the first 72 bytes of a password and silently ignores the
# rest.  Rather than let long passphrases lose entropy, we pre-hash every
# password with SHA-256 and base64-encode the digest (44 ASCII bytes, safely
# under the 72-byte ceiling) before feeding it to bcrypt.  This makes the full
# password contribute to the hash and removes the truncation footgun.  The
# scheme is versioned via a stored prefix so legacy raw-bcrypt hashes still
# verify.
MIN_PASSWORD_LENGTH = int(os.getenv("MIN_PASSWORD_LENGTH", "12"))
MAX_PASSWORD_LENGTH = int(os.getenv("MAX_PASSWORD_LENGTH", "128"))
_PREHASH_PREFIX = "sha256$"


def validate_password_strength(password: str) -> None:
    """Enforce the password policy. Raises HTTPException 400 on violation.

    Policy: length within [MIN_PASSWORD_LENGTH, MAX_PASSWORD_LENGTH] and at
    least three of the four character classes (lower, upper, digit, symbol).
    """
    if password is None or len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters long.",
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Password must be at most {MAX_PASSWORD_LENGTH} characters long.",
        )
    classes = sum(
        bool(match)
        for match in (
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        )
    )
    if classes < 3:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Password must include at least three of: lowercase, uppercase, "
                "digit, and symbol."
            ),
        )


# ---------------------------------------------------------------------------
# Password hashing  (direct bcrypt — compatible with bcrypt 4.x / 5.x)
# ---------------------------------------------------------------------------
def _bcrypt_payload(plain: str) -> bytes:
    """Return the 72-byte-safe bcrypt input for a plain-text password.

    Pre-hashing with SHA-256 means the entire password contributes to the
    resulting bcrypt hash, defeating bcrypt's silent 72-byte truncation.
    """
    import base64

    digest = hashlib.sha256(plain.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(plain: str) -> str:
    """Return a versioned bcrypt hash of the (SHA-256 pre-hashed) password."""
    hashed = bcrypt.hashpw(_bcrypt_payload(plain), bcrypt.gensalt()).decode("utf-8")
    return f"{_PREHASH_PREFIX}{hashed}"


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if plain matches the hashed password.

    Transparently supports both the new ``sha256$``-prefixed pre-hashed scheme
    and legacy raw-bcrypt hashes so existing accounts keep working.
    """
    try:
        if hashed.startswith(_PREHASH_PREFIX):
            stored = hashed[len(_PREHASH_PREFIX):].encode("utf-8")
            return bcrypt.checkpw(_bcrypt_payload(plain), stored)
        # Legacy hashes were computed directly over the raw password bytes.
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """
    Create a signed JWT access token.

    Args:
        data: Payload dict (should include ``sub`` = user email).
        expires_delta: Custom TTL; defaults to ACCESS_TOKEN_EXPIRE_MINUTES.

    Returns:
        Encoded JWT string.
    """
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode["exp"] = expire
    to_encode.setdefault("iat", datetime.now(timezone.utc))
    to_encode.setdefault("jti", uuid.uuid4().hex)
    signing_key = AUTH_PRIVATE_KEY if ALGORITHM == "RS256" else SECRET_KEY
    return jwt.encode(to_encode, signing_key, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """
    Decode and verify a JWT token.

    Raises:
        HTTPException 401 if the token is invalid or expired.
    """
    try:
        verify_key = AUTH_PUBLIC_KEY if ALGORITHM == "RS256" else SECRET_KEY
        payload = jwt.decode(token, verify_key, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# FastAPI dependency — current user
# ---------------------------------------------------------------------------
_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    """
    FastAPI dependency that extracts and validates the JWT from the
    Authorization header, then returns the corresponding User object.

    Raises:
        HTTPException 401 if the token is missing, invalid, or the user
        no longer exists in the database.
    """
    token = credentials.credentials if credentials else request.cookies.get(AUTH_COOKIE_NAME)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token or auth cookie required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_token(token)
    email: Optional[str] = payload.get("sub")
    jti: Optional[str] = payload.get("jti")
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token payload missing user identity.",
        )
    if jti and db.query(RevokedSession).filter(RevokedSession.jti == jti).first():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has been revoked. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account not found. Please register again.",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This account has been deactivated.",
        )
    return user


def revoke_token(db: Session, token: str, *, reason: str = "logout") -> None:
    """Persist a JWT jti so the token can no longer be used."""
    payload = decode_token(token)
    jti: Optional[str] = payload.get("jti")
    if not jti:
        return

    expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    email: Optional[str] = payload.get("sub")
    user = db.query(User).filter(User.email == email).first() if email else None
    if db.query(RevokedSession).filter(RevokedSession.jti == jti).first():
        return
    db.add(
        RevokedSession(
            jti=jti,
            user_id=user.id if user else None,
            reason=reason,
            expires_at=expires_at,
        )
    )
    db.commit()


def get_current_admin(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    """
    FastAPI dependency that validates the JWT and enforces the admin role.

    Raises:
        HTTPException 401 if unauthenticated.
        HTTPException 403 if the authenticated user is not an admin.
    """
    user = get_current_user(request, credentials, db)
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )
    return user


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------
class UserCreate(BaseModel):
    """Registration request body."""
    email: EmailStr
    password: str
    full_name: Optional[str] = None


class UserLogin(BaseModel):
    """Login request body."""
    email: str
    password: str


class Token(BaseModel):
    """Response body for a successful login or registration."""
    access_token: str
    token_type: str = "bearer"
    user_email: str
    user_name: Optional[str] = None
    is_admin: bool = False


class UserOut(BaseModel):
    """Public user info returned after auth."""
    id: int
    email: str
    full_name: Optional[str] = None

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Auth service functions
# ---------------------------------------------------------------------------
def register_user(db: Session, data: UserCreate) -> User:
    """
    Create a new user account.

    Raises:
        HTTPException 400 if the email is already registered.
    """
    validate_password_strength(data.password)
    if db.query(User).filter(User.email == data.email).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An account with this email already exists.",
        )
    user = User(
        email=data.email,
        hashed_password=hash_password(data.password),
        full_name=data.full_name,
        # Admin accounts must be provisioned by a trusted operator, never
        # requested through the public registration payload.
        is_admin=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(db: Session, email: str, password: str) -> User:
    """
    Verify credentials and return the User object.

    Raises:
        HTTPException 401 if credentials are invalid.
    """
    # Accept either an email or a portal username in the identifier field.
    user = (
        db.query(User)
        .filter((User.email == email) | (User.username == email))
        .first()
    )
    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been deactivated.",
        )
    return user


def authenticate_user_for_role(
    db: Session,
    email: str,
    password: str,
    *,
    require_admin: bool,
) -> User:
    """Authenticate credentials and require the selected login role."""
    user = authenticate_user(db, email, password)
    if require_admin and not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account does not have administrator access.",
        )
    if not require_admin and user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator accounts must use Admin Login.",
        )
    return user


# ---------------------------------------------------------------------------
# TOTP / MFA helpers
# ---------------------------------------------------------------------------
MFA_TOKEN_EXPIRE_MINUTES = int(os.getenv("MFA_TOKEN_EXPIRE_MINUTES", "5"))
MFA_PENDING_CLAIM = "mfa_pending"  # special JWT claim marking incomplete auth


def generate_totp_secret() -> str:
    """Generate a new random Base32 TOTP secret."""
    return pyotp.random_base32()


def get_totp_provisioning_uri(user_email: str, secret: str) -> str:
    """Return the otpauth:// URI for QR code generation in an Authenticator app."""
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(
        name=user_email,
        issuer_name="Mindgate Policy Bot",
    )


def verify_totp(user: User, code: str) -> bool:
    """Return True if the 6-digit TOTP code is valid for this user's secret."""
    if not user.totp_secret:
        return False
    totp = pyotp.TOTP(user.totp_secret)
    # valid_window=1 accepts the previous and next 30-second window for clock skew
    return totp.verify(code, valid_window=1)


def create_mfa_pending_token(user_email: str) -> str:
    """Create a short-lived JWT that signals MFA verification is still required.

    This token can only be exchanged at /auth/mfa/verify — it is NOT a valid
    access token for any other endpoint.
    """
    payload = {
        "sub": user_email,
        "claim": MFA_PENDING_CLAIM,
        "jti": uuid.uuid4().hex,
    }
    return create_access_token(
        payload,
        expires_delta=timedelta(minutes=MFA_TOKEN_EXPIRE_MINUTES),
    )


def decode_mfa_pending_token(token: str) -> str:
    """Decode and validate an MFA-pending token. Returns the user email.

    Raises HTTPException 401 if the token is invalid, expired, or not an MFA-pending token.
    """
    payload = decode_token(token)
    if payload.get("claim") != MFA_PENDING_CLAIM:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid MFA token. Use the token returned by the login endpoint.",
        )
    email = payload.get("sub")
    if not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid MFA token payload.")
    return email
