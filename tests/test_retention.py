"""
tests/test_retention.py — Unit tests for data retention and GDPR anonymization.
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, call


class TestRetentionPurge:
    def _make_mock_db(self, query_results=None):
        """Helper to create a mock SQLAlchemy session."""
        db = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.delete.return_value = 5  # Simulate 5 rows deleted
        db.query.return_value = mock_query
        return db, mock_query

    def test_retention_deletes_old_chat_messages(self):
        from services.retention import run_retention
        db, mock_query = self._make_mock_db()
        result = run_retention(db)
        assert "chat_messages_deleted" in result
        db.commit.assert_called()

    def test_retention_deletes_expired_nonces(self):
        from services.retention import run_retention
        db, _ = self._make_mock_db()
        result = run_retention(db)
        assert "a2a_nonces_purged" in result

    def test_retention_purges_revoked_sessions(self):
        from services.retention import run_retention
        db, _ = self._make_mock_db()
        result = run_retention(db)
        assert "revoked_sessions_purged" in result

    def test_retention_commits_once(self):
        from services.retention import run_retention
        db, _ = self._make_mock_db()
        run_retention(db)
        db.commit.assert_called_once()


class TestGdprAnonymization:
    def test_anonymize_nonexistent_user_returns_error(self):
        from services.retention import anonymize_user_data
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        result = anonymize_user_data(db, user_id=9999)
        assert result == {"error": "user_not_found"}

    def test_anonymize_user_masks_credentials(self):
        from services.retention import anonymize_user_data

        # Set up a realistic user with sessions/messages
        mock_msg = MagicMock()
        mock_session = MagicMock()
        mock_session.messages = [mock_msg]

        mock_user = MagicMock()
        mock_user.id = 42
        mock_user.email = "user@example.com"
        mock_user.sessions = [mock_session]

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = mock_user
        db.query.return_value.filter.return_value.delete.return_value = 1

        result = anonymize_user_data(db, user_id=42)

        # Email must be anonymized
        assert "deleted_42@deleted.invalid" == mock_user.email
        # Password must be wiped
        assert "[deleted]" == mock_user.hashed_password
        # Message content must be anonymized
        assert "[deleted" in mock_msg.content
        # Commit must be called
        db.commit.assert_called_once()

    def test_anonymize_writes_audit_log(self):
        from services.retention import anonymize_user_data

        mock_user = MagicMock()
        mock_user.sessions = []

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = mock_user
        db.query.return_value.filter.return_value.delete.return_value = 0

        anonymize_user_data(db, user_id=1)

        # Verify AuditLog was added
        db.add.assert_called()
        added_obj = db.add.call_args[0][0]
        assert hasattr(added_obj, "action")
        assert added_obj.action == "user_data_deleted"
