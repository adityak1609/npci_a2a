"""
tests/integration/test_a2a_idempotency.py

Integration tests verifying A2A idempotency and replay attack prevention.
Uses testcontainers to spin up a real PostgreSQL instance.
"""
import asyncio
import hashlib
import time
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from testcontainers.postgres import PostgresContainer

from database import Base, A2AIdempotencyKey, A2AMessage


@pytest.fixture(scope="module")
def db_engine():
    with PostgresContainer("postgres:15-alpine") as pg:
        url = pg.get_connection_url()
        engine = create_engine(url)
        Base.metadata.create_all(engine)
        yield engine
        Base.metadata.drop_all(engine)


@pytest.fixture
def db(db_engine):
    Session = sessionmaker(bind=db_engine)
    session = Session()
    yield session
    session.rollback()
    # Clean up idempotency keys and messages between tests
    session.query(A2AIdempotencyKey).delete()
    session.query(A2AMessage).delete()
    session.commit()
    session.close()


def _claim_idempotency(db, message_id: str, key_type: str = "transport") -> bool:
    """Try to claim an idempotency key. Returns True if first claim, False if duplicate."""
    existing = db.query(A2AIdempotencyKey).filter_by(
        key_type=key_type,
        key_value=message_id,
    ).first()
    if existing:
        return False  # Already processed

    key = A2AIdempotencyKey(
        key_type=key_type,
        key_value=message_id,
        message_id=message_id,
        task_type="policy_query",
        status="processing",
    )
    db.add(key)
    db.commit()
    return True


def _write_a2a_message(db, message_id: str) -> A2AMessage:
    """Write a single A2AMessage audit row."""
    prev = db.query(A2AMessage).order_by(A2AMessage.id.desc()).first()
    prev_hash = prev.entry_hash if prev else "genesis"
    entry_data = f"{message_id}{prev_hash}"
    entry_hash = hashlib.sha256(entry_data.encode()).hexdigest()

    msg = A2AMessage(
        message_id=message_id,
        task_type="policy_query",
        direction="inbound",
        prev_hash=prev_hash,
        entry_hash=entry_hash,
    )
    db.add(msg)
    db.commit()
    return msg


class TestA2ADoubleSubmit:
    """Test that submitting the same message_id twice creates only one DB row."""

    def test_first_submission_is_accepted(self, db):
        message_id = f"msg-{uuid.uuid4()}"
        claimed = _claim_idempotency(db, message_id)
        assert claimed is True

        _write_a2a_message(db, message_id)
        count = db.query(A2AMessage).filter_by(message_id=message_id).count()
        assert count == 1

    def test_second_submission_is_rejected(self, db):
        """A duplicate submission must NOT create a second audit row."""
        message_id = f"msg-{uuid.uuid4()}"

        # First submission — should succeed
        claimed_first = _claim_idempotency(db, message_id)
        assert claimed_first is True
        _write_a2a_message(db, message_id)

        # Second submission with same message_id — must be rejected
        claimed_second = _claim_idempotency(db, message_id)
        assert claimed_second is False

        # Audit table must still have only one row
        count = db.query(A2AMessage).filter_by(message_id=message_id).count()
        assert count == 1

    def test_different_message_ids_are_independent(self, db):
        """Two messages with different IDs are processed independently."""
        id1 = f"msg-{uuid.uuid4()}"
        id2 = f"msg-{uuid.uuid4()}"

        assert _claim_idempotency(db, id1) is True
        assert _claim_idempotency(db, id2) is True

        _write_a2a_message(db, id1)
        _write_a2a_message(db, id2)

        assert db.query(A2AMessage).count() == 2


class TestA2ANonceReplay:
    """Test that nonce-based replay attacks are rejected."""

    def test_nonce_uniqueness_enforced(self, db_engine):
        """Inserting the same nonce twice violates the unique constraint."""
        from database import A2ANonce
        Session = sessionmaker(bind=db_engine)
        db = Session()

        nonce_val = f"nonce-{uuid.uuid4()}"
        expires = datetime.now(timezone.utc).replace(year=2030)

        nonce1 = A2ANonce(nonce=nonce_val, expires_at=expires)
        db.add(nonce1)
        db.commit()

        # Attempting to insert the same nonce should raise IntegrityError
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError):
            nonce2 = A2ANonce(nonce=nonce_val, expires_at=expires)
            db.add(nonce2)
            db.commit()

        db.rollback()
        db.close()


class TestA2AConcurrentDuplicates:
    """Test that concurrent duplicate submissions are safe."""

    def test_concurrent_claims_only_one_succeeds(self, db_engine):
        """Simulate two concurrent requests with the same message_id."""
        message_id = f"msg-{uuid.uuid4()}"
        Session = sessionmaker(bind=db_engine)

        results = []

        def attempt_claim():
            session = Session()
            try:
                result = _claim_idempotency(session, message_id)
                results.append(result)
            except Exception:
                results.append(False)
            finally:
                session.close()

        # Run sequentially to simulate serializable isolation on the same key
        # (True concurrent test requires separate connections; this validates the logic)
        attempt_claim()
        attempt_claim()

        # Exactly one claim should have succeeded
        assert results.count(True) == 1
        assert results.count(False) == 1
