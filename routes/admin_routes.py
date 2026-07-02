"""Admin endpoints for document management and dashboards."""

import logging
import os
import shutil
import tempfile
import asyncio
import time
from typing import Optional, List
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, BackgroundTasks, status, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, desc

from auth import User, get_current_admin
from database import AuditLog, ChatMessage, ChatSession, DocumentChunk, MessageFeedback, DocumentSummary, get_db, SessionLocal, engine
from metrics import time_request, inc_counter, observe_histogram
from config import env_int, env_bool
from services.retention import anonymize_user_data
from agent.dispatcher import _generate_content_once, _log_confidence_sample
from file_validation import FileValidationError, max_admin_upload_bytes, validate_pdf_bytes
from compliance import ComplianceBlockedError
from telemetry import _log_internal_error
from utils import format_utc_datetime
from ingest import run_ingestion
from retriever import invalidate_cache

router = APIRouter()
logger = logging.getLogger(__name__)

DATA_DIR = str(Path(__file__).resolve().parent.parent / "data")

def _write_audit_log(
    db: Session,
    admin_id: int,
    action: str,
    target: str = None,
    detail: str = None,
) -> None:
    try:
        entry = AuditLog(admin_id=admin_id, action=action, target=target, detail=detail)
        db.add(entry)
        db.commit()
    except Exception as e:
        logger.exception("Failed to write audit log", exc_info=e)

def _upsert_admin_upload_summary(filename: str) -> None:
    """Admin upload summary; params: filename; returns None."""
    db = SessionLocal()
    try:
        chunks = (
            db.query(DocumentChunk.text)
            .filter(DocumentChunk.source_file == filename)
            .order_by(DocumentChunk.chunk_index)
            .limit(8)
            .all()
        )
        text = "\n\n".join(row[0] for row in chunks)[:4000]
        if not text:
            return

        started = time_request()
        summary = _generate_content_once([{
            "role": "user",
            "parts": [{"text": (
                "Summarize this uploaded policy document for an admin Inbox. "
                "Keep it concise and factual.\n\n"
                f"{text}"
            )}],
        }]).strip()
        observe_histogram(
            "gemini_api_latency_seconds",
            time_request() - started,
            labels={"call_type": "summary"},
        )

        existing = db.query(DocumentSummary).filter(DocumentSummary.filename == filename).first()
        if existing:
            existing.source = "admin_upload"
            existing.summary = summary or "Summary unavailable."
        else:
            db.add(DocumentSummary(
                filename=filename,
                source="admin_upload",
                summary=summary or "Summary unavailable.",
            ))
        db.commit()
    finally:
        db.close()



@router.get("/admin/pdfs", tags=["Admin"])
def list_pdfs(current_admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    """
    List all ingested PDF files with their chunk counts and last-modified time.
    Admin only.
    """
    from sqlalchemy import func
    rows = (
        db.query(
            DocumentChunk.source_file,
            func.count(DocumentChunk.id).label("chunk_count"),
        )
        .group_by(DocumentChunk.source_file)
        .all()
    )

    result = []
    for source_file, chunk_count in rows:
        file_path = os.path.join(DATA_DIR, source_file)
        size_bytes = os.path.getsize(file_path) if os.path.isfile(file_path) else None
        mtime = None
        if os.path.isfile(file_path):
            import datetime
            mtime = datetime.datetime.fromtimestamp(
                os.path.getmtime(file_path), tz=datetime.timezone.utc
            ).isoformat()
        result.append({
            "filename": source_file,
            "chunk_count": chunk_count,
            "size_bytes": size_bytes,
            "last_modified": mtime,
        })

    return {"pdfs": result}


@router.post("/admin/upload", tags=["Admin"])
async def upload_pdf(
    file: UploadFile = File(...),
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """
    Upload a new PDF file, save it to the data/ directory, and trigger ingestion.
    Admin only.
    """
    safe_filename = os.path.basename(file.filename or "").strip()
    if not safe_filename:
        raise HTTPException(status_code=400, detail="Filename is required.")

    os.makedirs(DATA_DIR, exist_ok=True)
    dest_path = os.path.join(DATA_DIR, safe_filename)
    backup_path = None
    tmp_path = None

    try:
        content = await file.read(max_admin_upload_bytes() + 1)
        validate_pdf_bytes(content, filename=safe_filename, max_bytes=max_admin_upload_bytes())

        with tempfile.NamedTemporaryFile(
            delete=False,
            dir=DATA_DIR,
            prefix=f".{safe_filename}.",
            suffix=".upload",
        ) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        # This endpoint performs ingestion itself.
        if os.path.exists(dest_path):
            backup_path = os.path.join(
                DATA_DIR,
                f".{safe_filename}.{int(time.time() * 1000)}.bak",
            )
            os.replace(dest_path, backup_path)
        os.replace(tmp_path, dest_path)
        tmp_path = None
    except FileValidationError as e:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
        if backup_path and os.path.exists(backup_path) and not os.path.exists(dest_path):
            os.replace(backup_path, dest_path)
        _log_internal_error("admin_upload_save", e)
        raise HTTPException(status_code=500, detail="Failed to save file.")

    try:
        result = await asyncio.to_thread(run_ingestion, dest_path)
        invalidate_cache()
    except ComplianceBlockedError as e:
        if os.path.isfile(dest_path):
            os.remove(dest_path)
        if backup_path and os.path.exists(backup_path):
            os.replace(backup_path, dest_path)
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        if os.path.isfile(dest_path):
            os.remove(dest_path)
        if backup_path and os.path.exists(backup_path):
            os.replace(backup_path, dest_path)
        _log_internal_error("admin_upload_ingest", e)
        raise HTTPException(status_code=500, detail="Ingestion failed.")

    if backup_path and os.path.exists(backup_path):
        os.remove(backup_path)

    summary_warning = None
    try:
        await asyncio.to_thread(_upsert_admin_upload_summary, safe_filename)
    except Exception as e:
        summary_warning = f"Summary unavailable: {e}"
        logger.exception("Admin upload summary failed for %s", safe_filename, exc_info=e)

    detail = f"Chunks: {result.get('chunks', result.get('chunks_created', '?'))}"
    if summary_warning:
        detail = f"{detail}; {summary_warning}"
        result["summary_warning"] = summary_warning
    _write_audit_log(db, current_admin.id, "upload_pdf", safe_filename, detail)
    return result


@router.delete("/admin/pdfs/{filename}", tags=["Admin"])
def delete_pdf(
    filename: str,
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """
    Delete a PDF: removes all its chunks from the database and the file from disk.
    Admin only.
    """
    # Sanitize filename to prevent path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")

    # Delete chunks from DB
    deleted_chunks = (
        db.query(DocumentChunk)
        .filter(DocumentChunk.source_file == filename)
        .delete()
    )
    db.query(DocumentSummary).filter(DocumentSummary.filename == filename).delete()
    db.commit()
    invalidate_cache()

    # Delete file from disk (best-effort)
    file_path = os.path.join(DATA_DIR, filename)
    file_existed = os.path.isfile(file_path)
    if file_existed:
        try:
            os.remove(file_path)
        except Exception as e:
            _log_internal_error("admin_delete_pdf_file", e)
            raise HTTPException(status_code=500, detail="DB cleaned up but file delete failed.")

    if deleted_chunks == 0 and not file_existed:
        raise HTTPException(status_code=404, detail=f"PDF '{filename}' not found.")

    _write_audit_log(db, current_admin.id, "delete_pdf", filename,
                     f"Chunks removed: {deleted_chunks}")
    return {"deleted": True, "filename": filename, "chunks_removed": deleted_chunks}


@router.get("/admin/dashboard", tags=["Admin"])
def admin_dashboard(
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """
    Aggregate analytics across all users: total queries, queries per day (last 14 days),
    top users by query volume, and the most recent 20 queries.
    Admin only.
    """
    import datetime
    from sqlalchemy import func, cast, Date

    now = datetime.datetime.now(datetime.timezone.utc)
    two_weeks_ago = now - datetime.timedelta(days=13)

    # ── Total counts ──────────────────────────────────────────────────────
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    message_counts = (
        db.query(
            func.count(ChatMessage.id).label("total_messages"),
            func.count(ChatMessage.id)
            .filter(ChatMessage.role == "user")
            .label("total_user_queries"),
            func.count(ChatMessage.id)
            .filter(
                ChatMessage.role == "user",
                ChatMessage.created_at >= today_start,
            )
            .label("queries_today"),
        )
        .one()
    )
    total_sessions = db.query(ChatSession).count()
    total_users = db.query(User).filter(User.is_admin == False).count()
    feedback_counts = (
        db.query(
            func.count(MessageFeedback.id).label("total"),
            func.count(MessageFeedback.id)
            .filter(MessageFeedback.rating == "positive")
            .label("positive"),
            func.count(MessageFeedback.id)
            .filter(MessageFeedback.rating == "negative")
            .label("negative"),
        )
        .one()
    )
    positive_rate = (
        round((feedback_counts.positive / feedback_counts.total) * 100, 1)
        if feedback_counts.total
        else 0
    )

    # ── Queries per day (last 14 days) ────────────────────────────────────
    daily_rows = (
        db.query(
            cast(ChatMessage.created_at, Date).label("day"),
            func.count(ChatMessage.id).label("count"),
        )
        .filter(
            ChatMessage.role == "user",
            ChatMessage.created_at >= two_weeks_ago,
        )
        .group_by(cast(ChatMessage.created_at, Date))
        .order_by(cast(ChatMessage.created_at, Date))
        .all()
    )
    # Fill in missing days with 0
    daily_map = {str(r.day): r.count for r in daily_rows}
    daily_chart = []
    for i in range(13, -1, -1):
        day = (now - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        daily_chart.append({"date": day, "count": daily_map.get(day, 0)})

    # ── Top users by query count ───────────────────────────────────────────
    top_users_rows = (
        db.query(
            User.email,
            User.full_name,
            func.count(ChatMessage.id).label("query_count"),
        )
        .join(ChatSession, ChatSession.user_id == User.id)
        .join(ChatMessage, ChatMessage.session_id == ChatSession.id)
        .filter(ChatMessage.role == "user", User.is_admin == False)
        .group_by(User.id, User.email, User.full_name)
        .order_by(func.count(ChatMessage.id).desc())
        .limit(10)
        .all()
    )
    top_users = [
        {"email": r.email, "name": r.full_name or r.email, "query_count": r.query_count}
        for r in top_users_rows
    ]

    # ── Recent queries (last 20) ───────────────────────────────────────────
    recent_rows = (
        db.query(ChatMessage, User.email, User.full_name)
        .join(ChatSession, ChatMessage.session_id == ChatSession.id)
        .join(User, ChatSession.user_id == User.id)
        .filter(ChatMessage.role == "user")
        .order_by(ChatMessage.created_at.desc())
        .limit(20)
        .all()
    )
    recent_queries = [
        {
            "user_email": email,
            "user_name": full_name or email,
            "query": msg.content[:200],
            "created_at": format_utc_datetime(msg.created_at),
            "session_id": msg.session_id,
        }
        for msg, email, full_name in recent_rows
    ]

    recent_feedback_rows = (
        db.query(
            MessageFeedback,
            ChatMessage,
            User.email,
            User.full_name,
        )
        .join(ChatMessage, MessageFeedback.message_id == ChatMessage.id)
        .join(User, MessageFeedback.user_id == User.id)
        .order_by(MessageFeedback.updated_at.desc())
        .limit(20)
        .all()
    )
    recent_feedback = [
        {
            "id": feedback.id,
            "message_id": message.id,
            "rating": feedback.rating,
            "reason": feedback.reason,
            "comment": feedback.comment,
            "answer": message.content[:300],
            "user_email": email,
            "user_name": full_name or email,
            "created_at": format_utc_datetime(feedback.created_at),
            "updated_at": format_utc_datetime(feedback.updated_at or feedback.created_at),
        }
        for feedback, message, email, full_name in recent_feedback_rows
    ]

    return {
        "summary": {
            "total_users": total_users,
            "total_sessions": total_sessions,
            "total_messages": message_counts.total_messages,
            "total_user_queries": message_counts.total_user_queries,
            "queries_today": message_counts.queries_today,
            "total_feedback": feedback_counts.total,
            "positive_feedback": feedback_counts.positive,
            "negative_feedback": feedback_counts.negative,
            "positive_rate": positive_rate,
        },
        "daily_chart": daily_chart,
        "top_users": top_users,
        "recent_queries": recent_queries,
        "recent_feedback": recent_feedback,
    }



@router.get("/admin/audit-log", tags=["Admin"])
def get_audit_log(
    limit: int = 100,
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Return recent audit log entries. Admin only."""
    entries = (
        db.query(AuditLog)
        .order_by(AuditLog.created_at.desc())
        .limit(min(limit, 500))
        .all()
    )
    return {
        "entries": [
            {
                "id": e.id,
                "action": e.action,
                "target": e.target,
                "detail": e.detail,
                "admin_email": e.admin.email if e.admin else "system",
                "created_at": format_utc_datetime(e.created_at),
            }
            for e in entries
        ]
    }


@router.delete("/admin/users/{user_id}", tags=["Admin - Users"])
def delete_user_data(
    user_id: int,
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """
    GDPR-compliant user data deletion.
    Anonymizes all chat messages and deletes sessions for the given user.
    The user record itself is anonymized (not hard-deleted) to preserve FK integrity.
    Audit logs are NOT deleted — they are required for regulatory compliance.
    """
    if current_admin.id == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Admins cannot delete their own account via this endpoint.",
        )
    result = anonymize_user_data(db, user_id)
    if "error" in result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    _write_audit_log(
        db,
        current_admin.id,
        action="user_data_deleted",
        target=f"user_id:{user_id}",
        detail=f"Deleted by admin {current_admin.email}. Result: {result}",
    )
    return {"status": "deleted", **result}
