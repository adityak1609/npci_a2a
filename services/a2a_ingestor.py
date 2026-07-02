"""A2A inbound document ingestion service."""

import asyncio
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import fitz
from google import genai
from sqlalchemy import text

from compliance import require_external_ai_approval
from database import (
    A2AIngestionJob,
    AuditLog,
    DocumentChunk,
    DocumentSummary,
    ReceivedDocument,
    SessionLocal,
)
from file_validation import (
    FileValidationError,
    decode_base64_limited,
    validate_ingest_bytes,
)
from ingest import chunk_text, generate_embeddings
from services.a2a_vision import extract_image_text
from services.email_notify import send_ingest_email


RECEIVED_DIR = Path("data") / "a2a_received"


def _now():
    return datetime.now(timezone.utc)


def _safe_filename(filename: str) -> str:
    base = Path(filename).name.strip() or "document"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base)[:180]


def _json_dumps(value: dict) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def _extract_pdf_text(content: bytes) -> str:
    """Extract text from received PDF bytes."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        doc = fitz.open(tmp_path)
        try:
            return "\n\n".join(page.get_text("text") for page in doc).strip()
        finally:
            doc.close()
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _summarize_text(client: genai.Client, text: str) -> str:
    """Summarize extracted document text for the admin Inbox."""
    require_external_ai_approval("A2A document summarization")
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=(
            "Summarize this compliance document for a Mindgate admin. "
            "Keep it concise, factual, and useful for later retrieval.\n\n"
            f"{text[:4000]}"
        ),
    )
    return (response.text or "").strip() or "Summary unavailable."


def _env_list(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


def _approved_recipients(requested: list[str]) -> list[str]:
    env_notify = _env_list("A2A_NOTIFY_LIST")
    explicit_allowlist = {item.lower() for item in _env_list("A2A_NOTIFY_ALLOWLIST")}
    allowed_domains = {item.lower().lstrip("@") for item in _env_list("A2A_NOTIFY_ALLOWED_DOMAINS")}
    approved = list(env_notify)

    for raw_email in requested:
        email = str(raw_email).strip()
        domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
        if email.lower() in explicit_allowlist or domain in allowed_domains:
            approved.append(email)

    deduped = []
    seen = set()
    for email in approved:
        key = email.lower()
        if key not in seen:
            deduped.append(email)
            seen.add(key)
    return deduped


def _set_job_status(job_id: int, status: str, error: str | None = None) -> None:
    db = SessionLocal()
    try:
        job = db.query(A2AIngestionJob).filter(A2AIngestionJob.id == job_id).first()
        if not job:
            return
        job.status = status
        job.error = error
        job.updated_at = _now()
        if status == "processing" and not job.started_at:
            job.started_at = _now()
        if status in {"ready", "failed"}:
            job.completed_at = _now()
        if job.document:
            job.document.status = status
            job.document.updated_at = _now()
        if status == "processing" and job.document:
            db.add(AuditLog(
                admin_id=None,
                action="a2a_ingest_started",
                target=job.document.filename,
                detail=f"job_id={job_id}; document_id={job.document_id}",
            ))
        db.commit()
    finally:
        db.close()


def _store_processed_document(
    document_id: int,
    filename: str,
    source: str,
    file_type: str,
    metadata: dict,
    chunks: list[str],
    embeddings: list[list[float]],
    summary: str,
) -> tuple[int, int]:
    source_file = f"a2a_{document_id}_{filename}"
    db = SessionLocal()
    try:
        db.query(DocumentChunk).filter(
            DocumentChunk.document_id == document_id
        ).delete(synchronize_session=False)
        db.query(DocumentSummary).filter(
            DocumentSummary.document_id == document_id
        ).delete(synchronize_session=False)

        metadata_json = _json_dumps(metadata)
        for idx, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            db.add(DocumentChunk(
                document_id=document_id,
                source_file=source_file,
                chunk_index=idx,
                text=chunk,
                embedding=embedding,
                source=source,
                content_type=file_type,
                source_doc=filename,
                metadata_json=metadata_json,
            ))

        doc = DocumentSummary(
            document_id=document_id,
            filename=filename,
            source=source,
            summary=summary,
        )
        db.add(doc)
        db.add(AuditLog(
            admin_id=None,
            action="a2a_ingest_completed",
            target=filename,
            detail=f"document_id={document_id}; chunks={len(chunks)}",
        ))
        db.commit()
        db.refresh(doc)
        return doc.id, len(chunks)
    finally:
        db.close()


async def process_ingestion_job(job_id: int) -> None:
    """Process one queued A2A ingestion job in the background."""
    _set_job_status(job_id, "processing")
    db = SessionLocal()
    try:
        job = db.query(A2AIngestionJob).filter(A2AIngestionJob.id == job_id).first()
        if not job or not job.document:
            return
        document = job.document
        document_id = document.id
        filename = document.filename
        source = document.source
        file_type = document.file_type
        stored_path = document.stored_path
        metadata = json.loads(document.metadata_json or "{}")
    finally:
        db.close()

    try:
        content = await asyncio.to_thread(Path(stored_path).read_bytes)
        _set_job_status(job_id, "extracting")
        if file_type == "pdf":
            text = await asyncio.to_thread(_extract_pdf_text, content)
        elif file_type == "image":
            text = await asyncio.to_thread(extract_image_text, content)
        else:
            text = content.decode("utf-8")

        if not text.strip():
            raise ValueError("No text could be extracted from the document.")

        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise EnvironmentError("GOOGLE_API_KEY not set.")
        require_external_ai_approval("A2A document embeddings and summaries")
        client = genai.Client(api_key=api_key)

        chunks = chunk_text(text)
        _set_job_status(job_id, "embedding")
        embeddings = await asyncio.to_thread(generate_embeddings, chunks, client)
        summary = await asyncio.to_thread(_summarize_text, client, text)

        doc_id, chunk_count = await asyncio.to_thread(
            _store_processed_document,
            document_id,
            filename,
            source,
            file_type,
            metadata,
            chunks,
            embeddings,
            summary,
        )
        _set_job_status(job_id, "ready")

        recipients = _approved_recipients(metadata.get("notify_list") or [])
        if recipients:
            await asyncio.to_thread(send_ingest_email, filename, metadata, summary, recipients)

        print(f"[a2a] Ingestion job {job_id} ready: doc={doc_id}, chunks={chunk_count}")
    except Exception as exc:
        db = SessionLocal()
        try:
            job = db.query(A2AIngestionJob).filter(A2AIngestionJob.id == job_id).first()
            filename = job.document.filename if job and job.document else str(job_id)
            db.add(AuditLog(
                admin_id=None,
                action="a2a_ingest_failed",
                target=filename,
                detail=str(exc),
            ))
            db.commit()
        finally:
            db.close()
        _set_job_status(job_id, "failed", str(exc))


# ---------------------------------------------------------------------------
# Tracked task registry — replaces fire-and-forget asyncio.create_task
# ---------------------------------------------------------------------------
# In inline development mode this is an in-process task tracker. If the API
# dies mid-ingestion, the task is lost (orphan recovery on startup mitigates
# this).  More critically, ingestion work (OCR, embedding — slow) shares the
# same process and event loop as live chat traffic.  Under a burst of NPCI
# document pushes, chat latency degrades.
#
# Migration path: move ingestion to a real worker queue (arq or Celery + Redis)
# once inbound NPCI volume is predictable.  The ReceivedDocument + A2AIngestionJob
# tables already model a durable job queue — the worker just needs to be a
# separate process reading from those tables instead of asyncio.create_task.
# ---------------------------------------------------------------------------
_active_tasks: dict[int, asyncio.Task] = {}

# Maximum accepted A2A document size (50 MB decoded).
MAX_INGEST_SIZE_BYTES = int(os.getenv("A2A_MAX_INGEST_BYTES", str(50 * 1024 * 1024)))


def worker_mode_enabled() -> bool:
    return os.getenv("A2A_INGESTION_MODE", "inline").strip().lower() == "worker"


def _task_done_callback(job_id: int, task: asyncio.Task) -> None:
    """Callback attached to every ingestion task for cleanup + error logging."""
    _active_tasks.pop(job_id, None)
    if task.cancelled():
        print(f"[a2a] Ingestion task for job {job_id} was cancelled.")
        return
    exc = task.exception()
    if exc:
        # The exception was already handled inside process_ingestion_job
        # (which sets job status to "failed"), but log it here as a safety net
        # so it never vanishes silently.
        print(f"[a2a] Ingestion task for job {job_id} raised: {exc!r}")


def _schedule_job(job_id: int) -> None:
    """Schedule an ingestion job as a tracked asyncio task."""
    if worker_mode_enabled():
        return
    task = asyncio.create_task(process_ingestion_job(job_id))
    _active_tasks[job_id] = task
    task.add_done_callback(lambda t: _task_done_callback(job_id, t))


def claim_next_queued_job() -> int | None:
    """Atomically claim one queued ingestion job for a worker process."""
    db = SessionLocal()
    try:
        result = db.execute(text("""
            UPDATE a2a_ingestion_jobs
            SET status = 'processing',
                started_at = COALESCE(started_at, NOW()),
                updated_at = NOW()
            WHERE id = (
                SELECT id
                FROM a2a_ingestion_jobs
                WHERE status = 'queued'
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id
        """)).scalar()
        db.commit()
        return int(result) if result is not None else None
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def recover_orphaned_jobs() -> int:
    """Re-queue jobs stuck in transient states from a previous unclean shutdown.

    Called once during FastAPI lifespan startup. Returns the number of
    recovered jobs.
    """
    orphan_statuses = {"queued", "processing", "extracting", "embedding"}
    db = SessionLocal()
    recovered = 0
    try:
        orphans = (
            db.query(A2AIngestionJob)
            .filter(A2AIngestionJob.status.in_(orphan_statuses))
            .all()
        )
        for job in orphans:
            job.status = "queued"
            job.error = None
            job.started_at = None
            job.completed_at = None
            job.updated_at = _now()
            if job.document:
                job.document.status = "queued"
                job.document.updated_at = _now()
            db.add(AuditLog(
                admin_id=None,
                action="a2a_ingest_recovered",
                target=job.document.filename if job.document else str(job.id),
                detail=f"job_id={job.id}; previous_status={job.status}",
            ))
            recovered += 1
        if recovered:
            db.commit()
    except Exception as exc:
        db.rollback()
        print(f"[startup] Failed to recover orphaned jobs: {exc}")
    finally:
        db.close()

    # Now schedule the recovered jobs in dev inline mode. In worker mode the
    # separate worker process claims queued rows durably from Postgres.
    if recovered and not worker_mode_enabled():
        db = SessionLocal()
        try:
            jobs_to_run = (
                db.query(A2AIngestionJob)
                .filter(A2AIngestionJob.status == "queued")
                .all()
            )
            for job in jobs_to_run:
                if job.id not in _active_tasks:
                    _schedule_job(job.id)
        finally:
            db.close()

    return recovered


async def handle_ingest(params: dict) -> dict:
    """Queue agent.ingest_document and return job metadata quickly."""
    file_type = (params.get("file_type") or "").lower()
    content_b64 = params.get("content_b64")
    filename = _safe_filename(params.get("filename") or "")
    notify_list = params.get("notify_list") or []
    metadata = params.get("metadata") or {}

    if file_type not in {"pdf", "image", "text"} or not content_b64 or not filename:
        raise ValueError("file_type, content_b64, and filename are required.")
    if notify_list and not isinstance(notify_list, list):
        raise ValueError("notify_list must be an array when provided.")
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object when provided.")

    try:
        content = decode_base64_limited(content_b64, max_bytes=MAX_INGEST_SIZE_BYTES)
        validate_ingest_bytes(
            file_type,
            filename,
            content,
            max_bytes=MAX_INGEST_SIZE_BYTES,
        )
    except FileValidationError as exc:
        raise ValueError(str(exc)) from exc

    content_hash = hashlib.sha256(content).hexdigest()
    metadata = dict(metadata)
    metadata["notify_list"] = notify_list
    metadata.setdefault("source", "npci_agent")
    metadata.setdefault("timestamp", _now().isoformat())

    db = SessionLocal()
    try:
        existing = (
            db.query(ReceivedDocument)
            .filter(ReceivedDocument.content_hash == content_hash)
            .first()
        )
        if existing:
            latest_job = (
                db.query(A2AIngestionJob)
                .filter(A2AIngestionJob.document_id == existing.id)
                .order_by(A2AIngestionJob.created_at.desc())
                .first()
            )
            db.add(AuditLog(
                admin_id=None,
                action="a2a_ingest_duplicate",
                target=existing.filename,
                detail=f"content_hash={content_hash}",
            ))
            db.commit()
            return {
                "queued": False,
                "duplicate": True,
                "document_id": existing.id,
                "job_id": latest_job.id if latest_job else None,
                "status": existing.status,
                "content_hash": content_hash,
            }

        RECEIVED_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = _now().strftime("%Y%m%d%H%M%S")
        stored_name = f"{timestamp}_{content_hash[:12]}_{filename}"
        stored_path = RECEIVED_DIR / stored_name
        stored_path.write_bytes(content)

        received = ReceivedDocument(
            filename=filename,
            stored_path=str(stored_path),
            content_hash=content_hash,
            source="npci_agent",
            file_type=file_type,
            status="queued",
            metadata_json=_json_dumps(metadata),
        )
        db.add(received)
        db.flush()

        job = A2AIngestionJob(document_id=received.id, status="queued")
        db.add(job)
        db.add(AuditLog(
            admin_id=None,
            action="a2a_ingest_queued",
            target=filename,
            detail=f"document_id={received.id}; content_hash={content_hash}",
        ))
        db.commit()
        db.refresh(job)
        document_id = received.id
        job_id = job.id
    finally:
        db.close()

    _schedule_job(job_id)
    return {
        "queued": True,
        "duplicate": False,
        "document_id": document_id,
        "job_id": job_id,
        "status": "queued",
        "content_hash": content_hash,
    }


# ---------------------------------------------------------------------------
# change_communication handler (spec §6 Part A)
# ---------------------------------------------------------------------------

def _prepare_change_documents(cc, change_id: str, version: int) -> list[dict]:
    """Validate every product_kit entry before any DB or filesystem writes."""
    from a2a_models import DocTypeEnum

    prepared = []
    for entry in cc.product_kit:
        try:
            DocTypeEnum(entry.doc_type)
        except ValueError:
            raise ValueError(
                f"Invalid doc_type '{entry.doc_type}' in product_kit. "
                f"Valid types: {', '.join(e.value for e in DocTypeEnum)}"
            )

        filename = _safe_filename(entry.filename)
        if str(entry.doc_type) in {"explainer_video", "promo_video"}:
            raise ValueError(
                f"Document '{filename}' has doc_type '{entry.doc_type}', but video "
                "ingestion is not implemented. Message was not processed."
            )

        file_type = "pdf"
        try:
            content = decode_base64_limited(entry.content_b64, max_bytes=MAX_INGEST_SIZE_BYTES)
            validate_ingest_bytes(
                file_type,
                filename,
                content,
                max_bytes=MAX_INGEST_SIZE_BYTES,
            )
        except FileValidationError as exc:
            raise ValueError(f"Document '{filename}' rejected: {exc}") from exc

        metadata = {
            "source": "npci_official",
            "change_id": change_id,
            "doc_type": str(entry.doc_type),
            "version": version,
            "description": entry.description,
            "notify_list": cc.notify_list or [],
            "timestamp": _now().isoformat(),
        }
        if entry.metadata:
            metadata.update(entry.metadata)

        prepared.append({
            "filename": filename,
            "content": content,
            "content_hash": hashlib.sha256(content).hexdigest(),
            "source": "npci_official",
            "file_type": file_type,
            "change_id": change_id,
            "doc_type": str(entry.doc_type),
            "version": version,
            "metadata": metadata,
        })
    return prepared


def _store_prepared_change_documents(prepared: list[dict]) -> tuple[list[dict], list[int], list[str]]:
    """Persist validated change documents and return response rows, jobs, and paths."""
    RECEIVED_DIR.mkdir(parents=True, exist_ok=True)
    written_paths: list[Path] = []
    job_ids: list[int] = []
    stored_paths: list[str] = []
    results: list[dict] = []
    db = SessionLocal()
    try:
        for item in prepared:
            existing = db.query(ReceivedDocument).filter(
                ReceivedDocument.content_hash == item["content_hash"]
            ).first()
            if existing:
                db.add(AuditLog(
                    admin_id=None,
                    action="a2a_ingest_duplicate",
                    target=existing.filename,
                    detail=f"change_id={item['change_id']}; content_hash={item['content_hash']}",
                ))
                results.append({
                    "filename": item["filename"],
                    "queued": False,
                    "duplicate": True,
                    "document_id": existing.id,
                    "content_hash": item["content_hash"],
                })
                stored_paths.append(existing.stored_path)
                continue

            timestamp = _now().strftime("%Y%m%d%H%M%S")
            stored_name = f"{timestamp}_{item['content_hash'][:12]}_{item['filename']}"
            stored_path = RECEIVED_DIR / stored_name
            stored_path.write_bytes(item["content"])
            written_paths.append(stored_path)
            stored_paths.append(str(stored_path))

            received = ReceivedDocument(
                filename=item["filename"],
                stored_path=str(stored_path),
                content_hash=item["content_hash"],
                source=item["source"],
                file_type=item["file_type"],
                status="queued",
                change_id=item["change_id"],
                doc_type=item["doc_type"],
                version=item["version"],
                metadata_json=_json_dumps(item["metadata"]),
            )
            db.add(received)
            db.flush()

            job = A2AIngestionJob(document_id=received.id, status="queued")
            db.add(job)
            db.flush()
            db.add(AuditLog(
                admin_id=None,
                action="a2a_ingest_queued",
                target=item["filename"],
                detail=(
                    f"change_id={item['change_id']}; doc_type={item['doc_type']}; "
                    f"document_id={received.id}"
                ),
            ))

            job_ids.append(job.id)
            results.append({
                "filename": item["filename"],
                "queued": True,
                "duplicate": False,
                "document_id": received.id,
                "job_id": job.id,
                "content_hash": item["content_hash"],
            })

        db.commit()
        return results, job_ids, stored_paths
    except Exception:
        db.rollback()
        for path in written_paths:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    finally:
        db.close()


def _ensure_documents_table() -> None:
    db = SessionLocal()
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS documents (
                change_id TEXT PRIMARY KEY,
                path TEXT,
                status TEXT NOT NULL,
                received_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                error TEXT
            )
        """))
        db.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_documents_status_received "
            "ON documents (status, received_at)"
        ))
        db.commit()
    finally:
        db.close()


def _get_received_change(change_id: str) -> dict | None:
    _ensure_documents_table()
    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT change_id, path, status, received_at, error FROM documents WHERE change_id = :change_id"),
            {"change_id": change_id},
        ).mappings().first()
        return dict(row) if row else None
    finally:
        db.close()


def _insert_received_change(change_id: str, stored_paths: list[str]) -> None:
    _ensure_documents_table()
    db = SessionLocal()
    try:
        db.execute(
            text("""
                INSERT INTO documents (change_id, path, status, received_at, updated_at)
                VALUES (:change_id, :path, 'received', NOW(), NOW())
            """),
            {"change_id": change_id, "path": _json_dumps({"paths": stored_paths})},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _set_received_change_status(change_id: str, status: str, error: str | None = None) -> None:
    _ensure_documents_table()
    db = SessionLocal()
    try:
        db.execute(
            text("""
                UPDATE documents
                SET status = :status,
                    error = :error,
                    updated_at = NOW()
                WHERE change_id = :change_id
            """),
            {"change_id": change_id, "status": status, "error": error},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _received_change_job_rows(change_id: str) -> list[dict]:
    db = SessionLocal()
    try:
        rows = db.execute(
            text("""
                SELECT j.id, j.status, d.version
                FROM a2a_ingestion_jobs j
                JOIN received_documents d ON d.id = j.document_id
                WHERE d.change_id = :change_id
                ORDER BY j.created_at, j.id
            """),
            {"change_id": change_id},
        ).mappings().all()
        return [dict(row) for row in rows]
    finally:
        db.close()


def _received_change_version(change_id: str) -> int:
    db = SessionLocal()
    try:
        version = db.execute(
            text("""
                SELECT version
                FROM received_documents
                WHERE change_id = :change_id AND version IS NOT NULL
                ORDER BY created_at
                LIMIT 1
            """),
            {"change_id": change_id},
        ).scalar()
        return int(version) if version is not None else 1
    finally:
        db.close()


def _claim_next_received_change_sync() -> str | None:
    _ensure_documents_table()
    db = SessionLocal()
    try:
        result = db.execute(text("""
            UPDATE documents
            SET status = 'processing',
                updated_at = NOW()
            WHERE change_id = (
                SELECT change_id
                FROM documents
                WHERE status = 'received'
                ORDER BY received_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING change_id
        """)).scalar()
        db.commit()
        return str(result) if result is not None else None
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


_active_change_tasks: dict[str, asyncio.Task] = {}


def _task_done_change_callback(change_id: str, task: asyncio.Task) -> None:
    _active_change_tasks.pop(change_id, None)
    if task.cancelled():
        print(f"[a2a] Change processing task for {change_id} was cancelled.")
        return
    exc = task.exception()
    if exc:
        print(f"[a2a] Change processing task for {change_id} raised: {exc!r}")


def _schedule_received_change(change_id: str) -> None:
    if worker_mode_enabled():
        return
    if change_id in _active_change_tasks:
        return
    task = asyncio.create_task(process_received_change(change_id))
    _active_change_tasks[change_id] = task
    task.add_done_callback(lambda t: _task_done_change_callback(change_id, t))


async def _enqueue_received_change(change_id: str) -> dict:
    if not worker_mode_enabled():
        _schedule_received_change(change_id)
        return {"mode": "inline", "queued": True}
    try:
        from services.task_queue import enqueue_received_change_processing
        await enqueue_received_change_processing(change_id)
        return {"mode": "arq", "queued": True}
    except Exception as exc:
        print(f"[a2a] Failed to enqueue received change {change_id}: {exc}")
        return {"mode": "arq", "queued": False, "error": str(exc)}


async def process_received_change(change_id: str) -> dict:
    """Run the existing per-document ingestion pipeline for a durable change row."""
    _set_received_change_status(change_id, "processing")
    try:
        rows = await asyncio.to_thread(_received_change_job_rows, change_id)
        if not rows:
            version = await asyncio.to_thread(_received_change_version, change_id)
            _set_received_change_status(change_id, "ingested")
            change_ack = await _auto_fire_change_acknowledgement(change_id, version)
            return {"status": "ingested", "change_id": change_id, "jobs": [], "change_acknowledgement": change_ack}

        processed: list[int] = []
        for row in rows:
            if row["status"] == "ready":
                processed.append(int(row["id"]))
                continue
            await process_ingestion_job(int(row["id"]))
            latest = await asyncio.to_thread(_received_change_job_rows, change_id)
            latest_row = next((item for item in latest if int(item["id"]) == int(row["id"])), None)
            if latest_row and latest_row["status"] == "failed":
                _set_received_change_status(change_id, "failed", f"job_id={row['id']}")
                return {"status": "failed", "change_id": change_id, "failed_job_id": int(row["id"])}
            processed.append(int(row["id"]))

        version = int(rows[0]["version"] or 1)
        _set_received_change_status(change_id, "ingested")
        change_ack = await _auto_fire_change_acknowledgement(change_id, version)
        return {
            "status": "ingested",
            "change_id": change_id,
            "jobs": processed,
            "change_acknowledgement": change_ack,
        }
    except Exception as exc:
        _set_received_change_status(change_id, "failed", str(exc))
        raise


async def resend_change_acknowledgements(change_id: str, version: int) -> dict:
    """Re-send outbound acknowledgements for an already received duplicate."""
    existing_change = await asyncio.to_thread(_get_received_change, change_id)
    if not existing_change:
        return {"status": "not_found", "change_id": change_id, "version": version}
    status = str(existing_change["status"])
    proposal_ack = await _auto_fire_proposal_acknowledged(change_id, version, force=True)
    result = {
        "status": status,
        "change_id": change_id,
        "version": version,
        "proposal_acknowledged": proposal_ack,
    }
    if status == "ingested":
        result["change_acknowledgement"] = await _auto_fire_change_acknowledgement(
            change_id, version, force=True
        )
    return result


def claim_next_received_change() -> str | None:
    """Atomically claim one durable received change for a worker process."""
    return _claim_next_received_change_sync()


async def recover_received_changes() -> int:
    """Re-enqueue durable received/processing change rows after an unclean shutdown."""
    _ensure_documents_table()
    db = SessionLocal()
    try:
        rows = db.execute(
            text("""
                SELECT change_id
                FROM documents
                WHERE status IN ('received', 'processing')
                ORDER BY received_at
            """)
        ).mappings().all()
        if rows:
            db.execute(
                text("""
                    UPDATE documents
                    SET status = 'received',
                        updated_at = NOW()
                    WHERE status = 'processing'
                """)
            )
            db.commit()
    finally:
        db.close()

    for row in rows:
        await _enqueue_received_change(str(row["change_id"]))
    return len(rows)


async def handle_change_communication_ingest(envelope, db_session) -> dict:
    """Process an inbound change_communication (NPCI → Bank).

    Per spec §6 Part A:
    1. Unpack product_kit[] entries doc-by-doc
    2. Tag each with source="npci_official", change_id from envelope
    3. Queue ingestion for each document
    4. Auto-fire proposal_acknowledged (Bank → NPCI, idempotent on
       (change_id, version_received))

    Args:
        envelope: Validated A2AEnvelope with task_type="change_communication"
        db_session: SQLAlchemy session from the route

    Returns:
        dict with queued document IDs and job IDs
    """
    from a2a_models import ChangeCommunicationPayload

    payload = envelope.payload
    try:
        cc = ChangeCommunicationPayload.model_validate(payload)
    except Exception as exc:
        raise ValueError(f"Invalid change_communication payload: {exc}")

    change_id = cc.change_id
    version = cc.version
    existing_change = await asyncio.to_thread(_get_received_change, change_id)
    if existing_change:
        status = str(existing_change["status"])
        proposal_ack = await _auto_fire_proposal_acknowledged(change_id, version, force=True)
        response = {
            "status": status,
            "change_id": change_id,
            "version": version,
            "duplicate": True,
            "proposal_acknowledged": proposal_ack,
        }
        if status == "ingested":
            response["change_acknowledgement"] = await _auto_fire_change_acknowledgement(
                change_id, version, force=True
            )
        return response

    prepared = _prepare_change_documents(cc, change_id, version)
    results, job_ids, stored_paths = _store_prepared_change_documents(prepared)
    await asyncio.to_thread(_insert_received_change, change_id, stored_paths)

    ack_result = await _auto_fire_proposal_acknowledged(change_id, version)
    if job_ids:
        queue_result = await _enqueue_received_change(change_id)
        change_ack = None
    else:
        await asyncio.to_thread(_set_received_change_status, change_id, "ingested")
        queue_result = {"queued": False, "reason": "no_new_documents"}
        change_ack = await _auto_fire_change_acknowledgement(change_id, version)

    response = {
        "status": "received",
        "change_id": change_id,
        "version": version,
        "documents": results,
        "proposal_acknowledged": ack_result,
        "processing": queue_result,
    }
    if change_ack:
        response["change_acknowledgement"] = change_ack
    return response

async def _auto_fire_proposal_acknowledged(change_id: str, version: int, force: bool = False) -> dict:
    """Auto-fire proposal_acknowledged (Bank → NPCI) per spec.

    Idempotent on (change_id, version_received).
    """
    import uuid

    from a2a_models import A2AEnvelope, ProposalAcknowledgedPayload

    # Check if already acknowledged
    db = SessionLocal()
    try:
        from database import A2AMessage
        existing = db.query(A2AMessage).filter(
            A2AMessage.task_type == "proposal_acknowledged",
            A2AMessage.direction == "outbound",
            A2AMessage.change_id == change_id,
            A2AMessage.error_code.is_(None),
        ).first()

        if not force and existing and existing.payload_ref and f"version_received={version}" in (existing.payload_ref or ""):
            return {"status": "already_acknowledged", "change_id": change_id, "version": version}
    finally:
        db.close()

    # Construct and send the acknowledgment
    ack_payload = ProposalAcknowledgedPayload(
        change_id=change_id,
        version_received=version,
    )

    message_id = f"ack_{change_id}_v{version}_{uuid.uuid4().hex[:8]}"
    envelope = {
        "task_type": "proposal_acknowledged",
        "change_id": change_id,
        "from": "mindgate_a2a_agent",
        "agent_id": "mindgate_a2a_agent",
        "agent_run_id": str(uuid.uuid4()),
        "correlation_id": str(uuid.uuid4()),
        "timestamp": _now().isoformat(),
        "payload": ack_payload.model_dump(),
        "protocol_version": "1.0",
        "message_id": message_id,
    }
    rpc_payload = {
        "jsonrpc": "2.0",
        "id": message_id,
        "method": "proposal_acknowledged",
        "params": envelope,
    }

    send_status = "sent"
    send_result = None
    try:
        from services.npci_client import post_npci_rpc
        send_result = await post_npci_rpc(rpc_payload)
    except Exception as exc:
        send_status = "send_failed"
        send_result = {"error": str(exc)}

    # Log as outbound
    db = SessionLocal()
    try:
        from a2a_audit import log_a2a_message
        log_a2a_message(
            db,
            message_id=message_id,
            task_type="proposal_acknowledged",
            direction="outbound",
            change_id=change_id,
            payload_ref=f"proposal_acknowledged|{change_id}|{version}|version_received={version}",
            result_payload=json.dumps({
                "payload": ack_payload.model_dump(),
                "send_status": send_status,
                "send_result": send_result,
            }, default=str),
            error_code=None if send_status == "sent" else "outbound_send_failed",
            http_status=200 if send_status == "sent" else 502,
        )
    finally:
        db.close()

    return {
        "status": "acknowledged" if send_status == "sent" else "acknowledgment_not_sent",
        "change_id": change_id,
        "version": version,
        "send_status": send_status,
        "send_result": send_result,
    }


async def _auto_fire_change_acknowledgement(change_id: str, version: int, force: bool = False) -> dict:
    """Auto-fire change_acknowledgement (Bank -> NPCI) after durable ingestion."""
    import uuid

    if not force:
        db = SessionLocal()
        try:
            from database import A2AMessage
            existing = db.query(A2AMessage).filter(
                A2AMessage.task_type == "change_acknowledgement",
                A2AMessage.direction == "outbound",
                A2AMessage.change_id == change_id,
                A2AMessage.error_code.is_(None),
            ).first()
            if existing and existing.payload_ref and f"version_ingested={version}" in (existing.payload_ref or ""):
                return {"status": "already_acknowledged", "change_id": change_id, "version": version}
        finally:
            db.close()

    ack_payload = {
        "change_id": change_id,
        "version_ingested": version,
        "status": "ingested",
    }
    message_id = f"change_ack_{change_id}_v{version}_{uuid.uuid4().hex[:8]}"
    envelope = {
        "task_type": "change_acknowledgement",
        "change_id": change_id,
        "from": "mindgate_a2a_agent",
        "agent_id": "mindgate_a2a_agent",
        "agent_run_id": str(uuid.uuid4()),
        "correlation_id": str(uuid.uuid4()),
        "timestamp": _now().isoformat(),
        "payload": ack_payload,
        "protocol_version": "1.0",
        "message_id": message_id,
    }
    rpc_payload = {
        "jsonrpc": "2.0",
        "id": message_id,
        "method": "change_acknowledgement",
        "params": envelope,
    }

    send_status = "sent"
    send_result = None
    try:
        from services.npci_client import post_npci_rpc
        send_result = await post_npci_rpc(rpc_payload)
    except Exception as exc:
        send_status = "send_failed"
        send_result = {"error": str(exc)}

    db = SessionLocal()
    try:
        from a2a_audit import log_a2a_message
        log_a2a_message(
            db,
            message_id=message_id,
            task_type="change_acknowledgement",
            direction="outbound",
            change_id=change_id,
            payload_ref=f"change_acknowledgement|{change_id}|{version}|version_ingested={version}",
            result_payload=json.dumps({
                "payload": ack_payload,
                "send_status": send_status,
                "send_result": send_result,
            }, default=str),
            error_code=None if send_status == "sent" else "outbound_send_failed",
            http_status=200 if send_status == "sent" else 502,
        )
    finally:
        db.close()

    return {
        "status": "acknowledged" if send_status == "sent" else "acknowledgment_not_sent",
        "change_id": change_id,
        "version": version,
        "send_status": send_status,
        "send_result": send_result,
    }


async def retry_ingestion_job(job_id: int) -> dict:
    """Create a new queued job for the document behind a failed ingestion job."""
    db = SessionLocal()
    try:
        failed_job = db.query(A2AIngestionJob).filter(A2AIngestionJob.id == job_id).first()
        if not failed_job:
            raise ValueError("ingestion job not found.")
        if failed_job.status != "failed":
            raise ValueError("only failed ingestion jobs can be retried.")

        failed_job.document.status = "queued"
        failed_job.document.updated_at = _now()
        retry_job = A2AIngestionJob(document_id=failed_job.document_id, status="queued")
        db.add(retry_job)
        db.add(AuditLog(
            admin_id=None,
            action="a2a_ingest_queued",
            target=failed_job.document.filename,
            detail=f"retry_of={job_id}; document_id={failed_job.document_id}",
        ))
        db.commit()
        db.refresh(retry_job)
        retry_job_id = retry_job.id
        document_id = retry_job.document_id
    finally:
        db.close()

    _schedule_job(retry_job_id)
    return {"queued": True, "document_id": document_id, "job_id": retry_job_id, "status": "queued"}
