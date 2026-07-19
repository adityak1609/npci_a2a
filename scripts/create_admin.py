"""Bootstrap the first administrator account in a fresh database.

All other users are provisioned by an admin through POST /admin/users, which
requires an existing admin session — this script breaks that chicken-and-egg
once, from a trusted operator shell:

    python scripts/create_admin.py --email ops@bank.example --full-name "Ops Admin"

The password is read from the ADMIN_BOOTSTRAP_PASSWORD environment variable,
or prompted for interactively (never accepted as a CLI argument, which would
leak into shell history and process listings).  Idempotent: re-running for an
existing admin is a no-op; use --promote to elevate an existing non-admin.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from core.secrets_loader import load_secret_files  # noqa: E402

load_secret_files()

from database import AuditLog, SessionLocal, User  # noqa: E402
from security.auth import hash_password, validate_password_strength  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or promote the first admin account.")
    parser.add_argument("--email", required=True, help="Admin login email")
    parser.add_argument("--full-name", default=None, help="Display name")
    parser.add_argument(
        "--promote",
        action="store_true",
        help="If the account already exists as a non-admin, promote it instead of failing.",
    )
    args = parser.parse_args()
    email = args.email.strip().lower()

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == email).first()
        if existing:
            if existing.is_admin:
                print(f"{email} is already an administrator; nothing to do.")
                return 0
            if not args.promote:
                print(f"{email} exists but is not an admin. Re-run with --promote to elevate.")
                return 1
            existing.is_admin = True
            db.add(AuditLog(admin_id=existing.id, action="admin_bootstrap_promote", target=email,
                            detail="Promoted to admin via scripts/create_admin.py"))
            db.commit()
            print(f"Promoted {email} to administrator.")
            return 0

        password = os.getenv("ADMIN_BOOTSTRAP_PASSWORD") or getpass.getpass(
            f"Password for new admin {email}: "
        )
        validate_password_strength(password)
        user = User(
            email=email,
            hashed_password=hash_password(password),
            full_name=args.full_name,
            is_admin=True,
            is_active=True,
        )
        db.add(user)
        db.flush()
        db.add(AuditLog(admin_id=user.id, action="admin_bootstrap_create", target=email,
                        detail="First admin created via scripts/create_admin.py"))
        db.commit()
        print(f"Created administrator {email}. Enable MFA from the portal on first login.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
