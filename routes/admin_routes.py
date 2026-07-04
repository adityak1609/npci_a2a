"""Admin endpoints for document management and dashboards."""

import json
import logging
import os
import shutil
import tempfile
import asyncio
import time
from typing import Optional, List
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, BackgroundTasks, status, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, desc, or_, and_, false

from auth import User, get_current_admin, get_current_user, hash_password, validate_password_strength
from database import (
    AuditLog, ChatMessage, ChatSession, DocumentChunk, MessageFeedback, DocumentSummary,
    A2AChange, A2ABlocker, A2ACounterProposal, A2AMilestone, A2AIngestionJob,
    ReceivedDocument, NpciTicket, HiddenUpdate, get_db, SessionLocal, engine,
)
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



# ---------------------------------------------------------------------------
# Incoming Changes dashboard (Part A change lifecycle)
# ---------------------------------------------------------------------------

_NEW_STATES = {"ASSIGNED", "COMMUNICATED", "RECEIVED"}
_IN_PROGRESS_STATES = {
    "ACCEPTED", "DESIGN_COMPLETED", "CODING_COMPLETED", "TESTED",
    "READY_FOR_CERTIFICATION", "CERTIFYING", "COUNTER_PROPOSAL",
}
_CERTIFIED_STATES = {"CERTIFIED"}


def _change_bucket(state: str) -> str:
    """Map a change lifecycle state to a dashboard bucket."""
    if state in _CERTIFIED_STATES:
        return "certified"
    if state in _IN_PROGRESS_STATES:
        return "in_progress"
    if state in _NEW_STATES:
        return "new"
    return "other"  # e.g. ABORTED


def _change_row(change: A2AChange, db: Session) -> dict:
    doc_count = (
        db.query(func.count(ReceivedDocument.id))
        .filter(ReceivedDocument.change_id == change.change_id)
        .scalar()
    )
    summary_row = (
        db.query(DocumentSummary)
        .filter(DocumentSummary.document_id == ReceivedDocument.id,
                ReceivedDocument.change_id == change.change_id)
        .order_by(DocumentSummary.created_at.desc())
        .first()
    )
    return {
        "change_id": change.change_id,
        "title": change.title or change.change_id,
        "state": change.current_state,
        "bucket": _change_bucket(change.current_state),
        "version": change.version,
        "blocked": change.blocked_at is not None,
        "blocked_reason": change.blocked_reason,
        "doc_count": doc_count or 0,
        "summary": summary_row.summary if summary_row else None,
        "created_at": format_utc_datetime(change.created_at),
        "updated_at": format_utc_datetime(change.updated_at),
    }


@router.get("/admin/changes", tags=["Admin - Changes"])
def list_changes(
    q: Optional[str] = None,
    status_filter: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Incoming Changes board: list changes with dashboard bucket counts.

    Readable by any active user (admins + bank product managers); mutations
    remain admin-only.
    """
    query = db.query(A2AChange).filter(A2AChange.deleted_at.is_(None))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            (A2AChange.title.ilike(like)) | (A2AChange.change_id.ilike(like))
        )
    changes = query.order_by(A2AChange.updated_at.desc().nullslast()).all()

    rows = [_change_row(c, db) for c in changes]
    counts = {"total": len(rows), "new": 0, "in_progress": 0, "certified": 0}
    for r in rows:
        if r["bucket"] in counts:
            counts[r["bucket"]] += 1

    if status_filter in {"new", "in_progress", "certified"}:
        rows = [r for r in rows if r["bucket"] == status_filter]

    return {"counts": counts, "changes": rows}


@router.get("/admin/changes/{change_id}", tags=["Admin - Changes"])
def get_change_detail(
    change_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Detail view for one change: docs, counters, blockers, milestones, tickets.

    Readable by any active user; mutations remain admin-only.
    """
    change = (
        db.query(A2AChange)
        .filter(A2AChange.change_id == change_id, A2AChange.deleted_at.is_(None))
        .first()
    )
    if not change:
        raise HTTPException(status_code=404, detail="Change not found.")

    docs = (
        db.query(ReceivedDocument)
        .filter(ReceivedDocument.change_id == change_id)
        .order_by(ReceivedDocument.created_at.desc())
        .all()
    )
    counters = db.query(A2ACounterProposal).filter(A2ACounterProposal.change_id == change_id).all()
    blockers = db.query(A2ABlocker).filter(A2ABlocker.change_id == change_id).all()
    milestones = db.query(A2AMilestone).filter(A2AMilestone.change_id == change_id).all()
    tickets = db.query(NpciTicket).filter(NpciTicket.change_id == change_id).all()

    return {
        "change": _change_row(change, db),
        "documents": [
            {
                "id": d.id, "filename": d.filename, "doc_type": d.doc_type,
                "status": d.status, "version": d.version,
                "summary": d.summary.summary if d.summary else None,
            }
            for d in docs
        ],
        "counter_proposals": [
            {"counter_proposal_id": c.counter_proposal_id, "status": c.status,
             "version_targeted": c.version_targeted,
             "decided": c.decision_json is not None}
            for c in counters
        ],
        "blockers": [
            {"blocker_id": b.blocker_id, "type": b.type, "severity": b.severity,
             "status": b.status, "resolution": b.resolution}
            for b in blockers
        ],
        "milestones": [
            {"milestone": m.milestone, "state": m.state, "eta": m.eta}
            for m in milestones
        ],
        "tickets": [
            {"ticket_id": t.ticket_id, "query": t.query, "status": t.status, "answer": t.answer}
            for t in tickets
        ],
        "owner": (
            {"id": change.owner.id, "username": change.owner.username or change.owner.email}
            if change.owner_user_id and change.owner
            else None
        ),
    }


@router.post("/admin/changes/{change_id}/assign", tags=["Admin - Changes"])
async def assign_change_owner(
    change_id: str,
    request: Request,
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Assign (or clear) the concerned bank product manager for a change.

    Body: {"user_id": <int> | null}. Sending null unassigns. Once assigned,
    only that user sees this change's NPCI updates on their portal dashboard.
    """
    change = db.query(A2AChange).filter(A2AChange.change_id == change_id).first()
    if not change:
        raise HTTPException(status_code=404, detail="Change not found.")
    body = await request.json()
    user_id = body.get("user_id")
    if user_id is None:
        change.owner_user_id = None
    else:
        owner = db.query(User).filter(User.id == user_id).first()
        if not owner:
            raise HTTPException(status_code=404, detail="User not found.")
        change.owner_user_id = owner.id
    db.commit()
    _write_audit_log(db, current_admin.id, "change_owner_assigned", target=change_id,
                     detail=f"user_id={user_id}")
    return {"change_id": change_id, "owner_user_id": change.owner_user_id}


@router.delete("/admin/changes/{change_id}", tags=["Admin - Changes"])
def delete_change(
    change_id: str,
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Soft-delete a change: hide it from the board, both dashboards, and the
    Assistant change picker. The audit trail (messages, documents) is preserved.
    """
    change = (
        db.query(A2AChange)
        .filter(A2AChange.change_id == change_id, A2AChange.deleted_at.is_(None))
        .first()
    )
    if not change:
        raise HTTPException(status_code=404, detail="Change not found.")
    change.deleted_at = datetime.now(timezone.utc)
    db.commit()
    _write_audit_log(db, current_admin.id, "change_deleted", target=change_id)
    return {"deleted": True, "change_id": change_id}


# ---------------------------------------------------------------------------
# Portal dashboard (bank product manager — owner-scoped NPCI updates)
# ---------------------------------------------------------------------------

@router.get("/portal/dashboard", tags=["Portal"])
def portal_dashboard(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """NPCI updates feed.

    Non-admin bank product managers see only the counter-proposal / blocker /
    milestone / escalation / document activity on changes they own — so one PM
    never sees another's updates — with admin-hidden items removed. Admins see
    the activity on all *assigned* changes (unassigned ones appear under common
    updates), including hidden items flagged, so they can hide/restore what
    users see.
    """
    is_admin = current_user.is_admin
    hidden_set = {(h.kind, h.ref) for h in db.query(HiddenUpdate).all()}

    def _finalize(items: list[dict]) -> list[dict]:
        """Flag hidden items; drop them for non-admins; newest first."""
        for it in items:
            it["hidden"] = (it["kind"], it["ref"]) in hidden_set
        if not is_admin:
            items = [it for it in items if not it["hidden"]]
        items.sort(key=lambda u: (u["at"] is not None, u["at"]), reverse=True)
        return items

    # Feed scope: admins see every assigned change; PMs see only their own.
    if is_admin:
        feed_changes = (
            db.query(A2AChange)
            .filter(A2AChange.owner_user_id.isnot(None), A2AChange.deleted_at.is_(None))
            .order_by(A2AChange.updated_at.desc().nullslast())
            .all()
        )
    else:
        feed_changes = (
            db.query(A2AChange)
            .filter(A2AChange.owner_user_id == current_user.id, A2AChange.deleted_at.is_(None))
            .order_by(A2AChange.updated_at.desc().nullslast())
            .all()
        )
    owned = [c for c in feed_changes if c.owner_user_id == current_user.id]
    change_ids = [c.change_id for c in feed_changes]
    title_of = {c.change_id: (c.title or c.change_id) for c in feed_changes}

    updates: list[dict] = []
    if change_ids:
        for cp in db.query(A2ACounterProposal).filter(A2ACounterProposal.change_id.in_(change_ids)).all():
            responded = cp.status == "decided" or cp.decision_json is not None
            updates.append({
                "kind": "counter_proposal",
                "change_id": cp.change_id,
                "change_title": title_of.get(cp.change_id, cp.change_id),
                "ref": cp.counter_proposal_id,
                "status": cp.status,
                "npci_responded": responded,
                "detail": "NPCI returned a decision" if responded else "Awaiting NPCI decision",
                "at": cp.updated_at,
            })
        for b in db.query(A2ABlocker).filter(A2ABlocker.change_id.in_(change_ids)).all():
            responded = b.resolution is not None
            updates.append({
                "kind": "blocker",
                "change_id": b.change_id,
                "change_title": title_of.get(b.change_id, b.change_id),
                "ref": b.blocker_id,
                "status": b.status,
                "npci_responded": responded,
                "detail": (f"NPCI resolution: {b.resolution}" if responded
                           else f"Open blocker: {b.subject or b.type or ''}".strip()),
                "at": b.updated_at,
            })
        for m in db.query(A2AMilestone).filter(A2AMilestone.change_id.in_(change_ids)).all():
            updates.append({
                "kind": "milestone",
                "change_id": m.change_id,
                "change_title": title_of.get(m.change_id, m.change_id),
                "ref": m.milestone,
                "status": m.state,
                "npci_responded": False,
                "detail": f"{m.milestone} → {m.state}",
                "at": m.updated_at,
            })
        # NOTE: NPCI documents are intentionally NOT added here. They are
        # broadcasts every user should see, so they live in the shared feed
        # below regardless of who owns the change.

    # ── Escalations: scoped to the user who raised them, NOT the change owner.
    # A query one PM raises from the Assistant (and NPCI's answer) is personal —
    # it must never appear on another user's feed. Legacy tickets with no
    # recorded raiser fall back to change-ownership scoping. Admins see all
    # escalations on assigned changes plus any user-raised ones.
    if is_admin:
        ticket_filter = or_(
            NpciTicket.change_id.in_(change_ids) if change_ids else false(),
            NpciTicket.raised_by_user_id.isnot(None),
        )
    else:
        ticket_filter = or_(
            NpciTicket.raised_by_user_id == current_user.id,
            and_(
                NpciTicket.raised_by_user_id.is_(None),
                NpciTicket.change_id.in_(change_ids) if change_ids else false(),
            ),
        )
    tickets = db.query(NpciTicket).filter(ticket_filter).all()

    # Titles for tickets on changes outside the feed scope (e.g. raised on a
    # change owned by someone else).
    missing_ids = {t.change_id for t in tickets if t.change_id and t.change_id not in title_of}
    if missing_ids:
        for c in db.query(A2AChange).filter(A2AChange.change_id.in_(missing_ids)).all():
            title_of[c.change_id] = c.title or c.change_id

    # Admins get oversight of *who* raised each escalation (and thus who NPCI's
    # answer went to). Batch-resolve raiser identities to avoid an N+1 query.
    raiser_of: dict[int, str] = {}
    if is_admin:
        raiser_ids = {t.raised_by_user_id for t in tickets if t.raised_by_user_id is not None}
        if raiser_ids:
            for u in db.query(User).filter(User.id.in_(raiser_ids)).all():
                raiser_of[u.id] = u.full_name or u.email or u.username or f"user #{u.id}"

    for t in tickets:
        responded = bool(t.answer) or (t.status or "").lower() in {"answered", "resolved", "closed", "completed"}
        citations = None
        if t.grounded_in:
            try:
                refs = json.loads(t.grounded_in)
                if isinstance(refs, list) and refs:
                    citations = ", ".join(str(r) for r in refs)
            except (json.JSONDecodeError, TypeError):
                pass
        raised_by = raiser_of.get(t.raised_by_user_id) if is_admin else None
        full_parts = []
        if raised_by:
            full_parts.append(f"Raised by: {raised_by}")
        full_parts.append(f"Question: {t.query}")
        if t.answer:
            full_parts.append(f"NPCI answer: {t.answer}")
            if citations:
                full_parts.append(f"Grounded in: {citations}")
            if t.provisional or t.amends_change:
                full_parts.append(
                    "Note: this answer is provisional — it will be superseded by a formal change update."
                )
        else:
            full_parts.append("Escalated to NPCI — awaiting response.")
        updates.append({
            "kind": "escalation",
            "change_id": t.change_id,
            "change_title": title_of.get(t.change_id, t.change_id or "—"),
            "ref": t.ticket_id,
            "status": t.status,
            "npci_responded": responded,
            "detail": (t.answer[:200] if t.answer else f"Escalated: {t.query[:160]}"),
            "detail_full": "\n\n".join(full_parts),
            "raised_by": raised_by,
            "at": t.created_at,
            "download_id": None,
        })

    updates = _finalize(updates)

    # ── Shared updates: NPCI documents and new-change notices, visible to
    # everyone regardless of change ownership. Documents are broadcasts, so a
    # PM claiming a change never hides its documents from other users.
    shared_changes = (
        db.query(A2AChange)
        .filter(A2AChange.deleted_at.is_(None))
        .order_by(A2AChange.created_at.desc().nullslast())
        .limit(200)
        .all()
    )
    shared_title = {c.change_id: (c.title or c.change_id) for c in shared_changes}
    shared_ids = list(shared_title.keys())
    common_updates: list[dict] = []
    for c in shared_changes:
        common_updates.append({
            "kind": "change_published",
            "change_id": c.change_id,
            "change_title": shared_title[c.change_id],
            "ref": c.change_id,
            "status": c.current_state,
            "npci_responded": True,
            "detail": "New change published by NPCI",
            "at": c.created_at,
            "download_id": None,
        })
    if shared_ids:
        for d in (
            db.query(ReceivedDocument)
            .filter(ReceivedDocument.change_id.in_(shared_ids))
            .order_by(ReceivedDocument.created_at.desc())
            .limit(200)
            .all()
        ):
            common_updates.append({
                "kind": "document",
                "change_id": d.change_id,
                "change_title": shared_title.get(d.change_id, d.change_id),
                "ref": str(d.id),
                "status": d.status,
                "npci_responded": True,
                "detail": f"New document from NPCI: {d.filename}",
                "at": d.created_at,
                "download_id": d.id,
            })
    common_updates = _finalize(common_updates)[:100]

    return {
        "is_admin": is_admin,
        "counts": {
            "changes": len(owned),
            "updates": len(updates),
            "npci_responses": sum(1 for u in updates if u["npci_responded"]),
            "common_updates": len(common_updates),
        },
        "changes": [
            {
                "change_id": c.change_id,
                "title": c.title or c.change_id,
                "state": c.current_state,
                "bucket": _change_bucket(c.current_state),
                "blocked": c.blocked_at is not None,
                "updated_at": format_utc_datetime(c.updated_at),
            }
            for c in owned
        ],
        "updates": [
            {**u, "at": format_utc_datetime(u["at"])}
            for u in updates
        ],
        "common_updates": [
            {**u, "at": format_utc_datetime(u["at"])}
            for u in common_updates
        ],
    }


@router.post("/admin/updates/hide", tags=["Portal"])
async def hide_update(
    request: Request,
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Hide an update item from users' dashboards. Body: {"kind": str, "ref": str}."""
    body = await request.json()
    kind = (body.get("kind") or "").strip()
    ref = str(body.get("ref") or "").strip()
    if not kind or not ref:
        raise HTTPException(status_code=400, detail="kind and ref are required.")
    existing = (
        db.query(HiddenUpdate)
        .filter(HiddenUpdate.kind == kind, HiddenUpdate.ref == ref)
        .first()
    )
    if not existing:
        db.add(HiddenUpdate(kind=kind, ref=ref, hidden_by=current_admin.id))
        db.commit()
        _write_audit_log(db, current_admin.id, "update_hidden", target=f"{kind}:{ref}")
    return {"hidden": True, "kind": kind, "ref": ref}


@router.post("/admin/updates/unhide", tags=["Portal"])
async def unhide_update(
    request: Request,
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Restore a previously hidden update. Body: {"kind": str, "ref": str}."""
    body = await request.json()
    kind = (body.get("kind") or "").strip()
    ref = str(body.get("ref") or "").strip()
    deleted = (
        db.query(HiddenUpdate)
        .filter(HiddenUpdate.kind == kind, HiddenUpdate.ref == ref)
        .delete(synchronize_session=False)
    )
    db.commit()
    if deleted:
        _write_audit_log(db, current_admin.id, "update_unhidden", target=f"{kind}:{ref}")
    return {"hidden": False, "kind": kind, "ref": ref}


# ---------------------------------------------------------------------------
# Knowledge Base (paste + index)
# ---------------------------------------------------------------------------

@router.get("/admin/kb/documents", tags=["Admin - Knowledge Base"])
def kb_list(current_user: User = Depends(get_current_user)):
    """List indexed knowledge-base documents (read-only for non-admins)."""
    from services.kb_ingest import list_kb_documents
    return {"documents": list_kb_documents()}


@router.post("/admin/kb/documents", tags=["Admin - Knowledge Base"])
async def kb_add(
    request: Request,
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Paste a document (title, content, source?) → chunk + embed + index."""
    from services.kb_ingest import ingest_text_document
    body = await request.json()
    title = (body.get("title") or "").strip()
    content = body.get("content") or ""
    source = (body.get("source") or "").strip() or None
    if not title or not content.strip():
        raise HTTPException(status_code=400, detail="title and content are required.")
    try:
        result = await asyncio.to_thread(ingest_text_document, title, content, source)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ComplianceBlockedError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        _log_internal_error("kb_add", exc)
        raise HTTPException(status_code=500, detail="Indexing failed.")
    _write_audit_log(db, current_admin.id, "kb_document_indexed", target=title,
                     detail=f"chunks={result.get('chunks_indexed')}")
    return result


@router.delete("/admin/kb/documents/{title}", tags=["Admin - Knowledge Base"])
def kb_delete(
    title: str,
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Delete a knowledge-base document and its chunks by title."""
    from services.kb_ingest import delete_kb_document
    removed = delete_kb_document(title)
    if removed == 0:
        raise HTTPException(status_code=404, detail="Document not found.")
    _write_audit_log(db, current_admin.id, "kb_document_deleted", target=title,
                     detail=f"chunks_removed={removed}")
    return {"deleted": True, "title": title, "chunks_removed": removed}


# ---------------------------------------------------------------------------
# Settings (runtime config store)
# ---------------------------------------------------------------------------

@router.get("/admin/settings", tags=["Admin - Settings"])
def get_settings(current_admin: User = Depends(get_current_admin)):
    """Return runtime settings grouped by category (secrets masked)."""
    from services.settings_store import get_grouped
    return {"settings": get_grouped(mask_secrets=True)}


@router.put("/admin/settings", tags=["Admin - Settings"])
async def put_settings(
    request: Request,
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Upsert runtime settings. Body: {"<KEY>": "<value>", ...}."""
    from services.settings_store import set_settings
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be an object of key/value pairs.")
    written = set_settings({k: ("" if v is None else str(v)) for k, v in body.items()})
    _write_audit_log(db, current_admin.id, "settings_updated", target="app_settings",
                     detail=f"keys={','.join(written)}")
    return {"updated": written}


@router.post("/admin/settings/test-connection", tags=["Admin - Settings"])
async def settings_test_connection(current_admin: User = Depends(get_current_admin)):
    """Send an echo to NPCI using the current settings; report reachability."""
    from services.npci_client import post_npci_rpc
    from services.npci_outbound import build_outbound_envelope
    envelope = build_outbound_envelope("echo", {"ping": "ping"})
    rpc_payload = {"jsonrpc": "2.0", "id": envelope["message_id"], "method": "echo", "params": envelope}
    try:
        data = await post_npci_rpc(rpc_payload)
        return {"ok": True, "result": data.get("result") if isinstance(data, dict) else data}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


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


@router.get("/admin/users", tags=["Admin - Users"])
def list_users(
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """List portal users with role/status and summary counts.

    Anonymized (deleted) accounts are excluded — their rows are retained for
    audit/FK integrity but should not appear in the management list.
    """
    users = (
        db.query(User)
        .filter(User.email.notlike("%@deleted.invalid"))
        .order_by(User.created_at.asc().nullslast())
        .all()
    )
    total = len(users)
    active = sum(1 for u in users if u.is_active)
    admins = sum(1 for u in users if u.is_admin)
    return {
        "counts": {
            "total": total,
            "active": active,
            "inactive": total - active,
            "admins": admins,
        },
        "users": [
            {
                "id": u.id,
                "username": u.username or u.email,
                "email": u.email,
                "full_name": u.full_name,
                "role": "admin" if u.is_admin else "user",
                "status": "active" if u.is_active else "inactive",
            }
            for u in users
        ],
    }


@router.post("/admin/users", tags=["Admin - Users"])
async def create_user(
    request: Request,
    current_admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Create a portal user. Body: username, password, full_name?, role(user|admin)."""
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    full_name = (body.get("full_name") or "").strip() or None
    role = (body.get("role") or "user").strip().lower()
    if not username:
        raise HTTPException(status_code=400, detail="username is required.")
    validate_password_strength(password)

    email = username if "@" in username else f"{username}@partner.local"
    if db.query(User).filter((User.username == username) | (User.email == email)).first():
        raise HTTPException(status_code=400, detail="A user with this username already exists.")

    user = User(
        email=email,
        username=username,
        full_name=full_name,
        hashed_password=hash_password(password),
        is_admin=(role == "admin"),
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    _write_audit_log(db, current_admin.id, "user_created", target=username, detail=f"role={role}")
    return {"id": user.id, "username": username, "role": role, "status": "active"}


def _set_user_active(user_id: int, active: bool, current_admin: User, db: Session) -> dict:
    if current_admin.id == user_id:
        raise HTTPException(status_code=400, detail="You cannot change your own active status.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    user.is_active = active
    db.commit()
    _write_audit_log(
        db, current_admin.id,
        "user_activated" if active else "user_deactivated",
        target=user.username or user.email,
    )
    return {"id": user.id, "status": "active" if active else "inactive"}


@router.post("/admin/users/{user_id}/activate", tags=["Admin - Users"])
def activate_user(user_id: int, current_admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Reactivate a deactivated user."""
    return _set_user_active(user_id, True, current_admin, db)


@router.post("/admin/users/{user_id}/deactivate", tags=["Admin - Users"])
def deactivate_user(user_id: int, current_admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Deactivate a user (blocks login + existing sessions)."""
    return _set_user_active(user_id, False, current_admin, db)


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
