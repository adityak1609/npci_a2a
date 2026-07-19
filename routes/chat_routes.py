"""System and Chat endpoints.

Provides:
    GET  /health            — Backward-compatible health check
    GET  /health/live       — Kubernetes liveness probe
    GET  /health/ready      — Kubernetes readiness probe (DB + Redis + index)
    GET  /metrics           — Prometheus metrics scrape endpoint
    POST /ingest            — Admin PDF ingestion trigger
    POST /chat              — Non-streaming chat (JSON response)
    POST /chat/stream       — Streaming chat via Server-Sent Events (SSE)
    GET  /chat/sessions     — List all sessions for the authenticated user
    DELETE /chat/sessions/{session_id} — Delete a session
    GET  /chat/history/{session_id}    — Retrieve message history for a session
"""

import asyncio
import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from agent.dispatcher import _get_genai_client
from core.config import env_int, gemini_models, is_retryable_llm_error
from core.metrics import inc_counter, render_prometheus, set_gauge
from core.telemetry import _log_internal_error
from core.utils import (
    _allowed_visibility_tiers,
    _build_gemini_contents,
    _generate_with_retry,
    _get_or_create_session,
    _load_chat_history,
    _save_messages,
    _verified_answer_chunks,
    format_utc_datetime,
)
from database import (
    A2AIngestionJob,
    ChatMessage,
    ChatSession,
    SessionLocal,
    engine,
    get_db,
)
from rag.decompose import decomposed_retrieve_with_scores
from rag.ingest import run_ingestion
from rag.prompt import build_prompt
from rag.retriever import index_exists, invalidate_cache
from routes.auth_routes import require_metrics_access
from security.auth import User, get_current_admin
from security.compliance import ComplianceBlockedError, require_external_ai_approval
from security.guardrails import (
    VerifiedStreamBuffer,
    _check_regex,
    check_input,
    check_output,
    classifier_circuit_state,
    sanitize_question,
)
from security.rate_limit import check_chat_rate_limit

router = APIRouter()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    session_id: str
    question: str
    context: Optional[dict] = None


class ChatResponse(BaseModel):
    answer: str
    chunks_used: int
    message_id: int


# ---------------------------------------------------------------------------
# Health / system routes
# ---------------------------------------------------------------------------

@router.get("/health", tags=["System"])
async def health_check():
    """Backward-compatible health check (kept for existing probes)."""
    pool = engine.pool
    return {
        "status": "ok",
        "index_ready": index_exists(),
        "db_pool": {
            "checked_in": pool.checkedin(),
            "checked_out": pool.checkedout(),
            "overflow": pool.overflow(),
            "size": pool.size(),
        },
    }


@router.get("/health/live", tags=["System"])
async def health_live():
    """Kubernetes liveness probe — confirms the process is running.

    Always returns 200 unless the process has crashed.  Intentionally
    lightweight: no external calls so it cannot trigger a pod restart
    due to a transient downstream outage.
    """
    return {"status": "alive"}


@router.get("/health/ready", tags=["System"])
async def health_ready():
    """Kubernetes readiness probe — confirms the app can serve traffic.

    Checks:
    - PostgreSQL reachability (SELECT 1)
    - Redis reachability (PING), if REDIS_URL is configured
    - Vector index loaded

    Returns 200 when all checks pass, 503 when any check fails.
    """
    from sqlalchemy import text

    checks: dict[str, object] = {}
    all_ok = True

    # Database check
    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            checks["db"] = {"ok": True}
        finally:
            db.close()
    except Exception as _db_exc:
        checks["db"] = {"ok": False, "error": "unreachable"}
        all_ok = False

    # Redis check (only when configured)
    redis_url = os.getenv("REDIS_URL", "").strip()
    if redis_url:
        try:
            import redis as _redis
            _r = _redis.Redis.from_url(
                redis_url,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
            _r.ping()
            checks["redis"] = {"ok": True}
        except Exception:
            checks["redis"] = {"ok": False, "error": "unreachable"}
            all_ok = False
    else:
        checks["redis"] = {"ok": True, "note": "not configured"}

    # Vector index check
    index_ready = index_exists()
    checks["index"] = {"ok": index_ready}
    if not index_ready:
        all_ok = False

    status_code = 200 if all_ok else 503
    return JSONResponse(
        content={"status": "ready" if all_ok else "degraded", "checks": checks},
        status_code=status_code,
    )


@router.get("/metrics", tags=["System"])
async def metrics_endpoint(_access=Depends(require_metrics_access)):
    """Prometheus-compatible application metrics."""
    pool = engine.pool
    set_gauge("db_pool_checked_in_connections", pool.checkedin(), {"pool": "default"})
    set_gauge("db_pool_checked_out_connections", pool.checkedout(), {"pool": "default"})
    set_gauge("db_pool_overflow_connections", pool.overflow(), {"pool": "default"})
    set_gauge("db_pool_size_connections", pool.size(), {"pool": "default"})
    set_gauge("guardrail_classifier_circuit_open", classifier_circuit_state())
    db = SessionLocal()
    try:
        for status_value in ("queued", "processing", "failed"):
            count = db.query(A2AIngestionJob).filter(A2AIngestionJob.status == status_value).count()
            set_gauge(
                "a2a_ingestion_jobs",
                count,
                {"status": status_value},
            )
    finally:
        db.close()
    return PlainTextResponse(render_prometheus(), media_type="text/plain; version=0.0.4")


@router.post("/ingest", tags=["System"])
async def ingest_endpoint(current_user: User = Depends(get_current_admin)):
    """
    Trigger PDF ingestion (or re-ingestion if the PDF was updated).
    Requires authentication. Runs ingestion in a thread to avoid blocking.
    """
    try:
        result = await asyncio.to_thread(run_ingestion)
        invalidate_cache()
        return result
    except ComplianceBlockedError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _log_internal_error("system_ingest", e)
        raise HTTPException(status_code=500, detail="Ingestion failed.")


# ---------------------------------------------------------------------------
# Chat routes
# ---------------------------------------------------------------------------

@router.post("/chat", response_model=ChatResponse, tags=["Chat"])
async def chat_endpoint(
    req: ChatRequest,
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """
    Accept a user question, retrieve relevant policy chunks,
    inject chat history, and generate an answer using Gemini.

    Security: Input is checked by guardrails before retrieval,
    the question is sanitized, and output is filtered post-generation.

    Performance: The guardrail check (Tier 1 regex + Tier 2 LLM) and
    the retrieval (embedding + pgvector search) are independent, so we
    run them in parallel via asyncio.gather to cut ~1 API round-trip
    off the total latency.
    """
    # 0. Tier 1 regex — instant, no API cost
    check_chat_rate_limit(str(current_user.id))
    regex_guard = _check_regex(req.question)
    if regex_guard.blocked:
        message = regex_guard.message or ""
        session = _get_or_create_session(db, req.session_id, current_user)
        assistant = _save_messages(db, session.id, req.question, message)
        return ChatResponse(
            answer=message,
            chunks_used=0,
            message_id=assistant.id,
        )

    # 1. Sanitize the question (strip embedded meta-instructions)
    clean_question = sanitize_question(req.question)

    # 2. Ensure session exists + load history (fast DB queries)
    session = _get_or_create_session(db, req.session_id, current_user)
    history = _load_chat_history(db, session.id)

    # 3. Run LLM guardrail + retrieval IN PARALLEL — they are independent
    #    This saves ~1-3 seconds by overlapping the classifier and embedding calls.
    #    Retrieval decomposes multi-part questions into standalone sub-queries
    #    retrieved in parallel and RRF-fused (rag/decompose.py).
    guard_task = asyncio.to_thread(check_input, req.question)
    visibility_tiers = _allowed_visibility_tiers(current_user)
    retrieve_task = asyncio.to_thread(
        decomposed_retrieve_with_scores, clean_question, 5, visibility_tiers
    )

    try:
        guard, scored_chunks = await asyncio.gather(guard_task, retrieve_task)
    except FileNotFoundError:
        raise HTTPException(
            status_code=400,
            detail="Policy index not found. Please trigger /ingest first.",
        )
    chunks = [row["text"] for row in scored_chunks]

    # 4. If guardrail blocked, discard retrieval results
    if guard.blocked:
        message = guard.message or ""
        assistant = _save_messages(db, session.id, req.question, message)
        return ChatResponse(
            answer=message,
            chunks_used=0,
            message_id=assistant.id,
        )

    # 5. Build grounded system prompt + contents with history
    system_prompt = build_prompt(chunks)
    contents = _build_gemini_contents(system_prompt, history, clean_question)

    # 6. Call Gemini (blocks on Gemini generation API)
    try:
        answer = await _generate_with_retry(contents)
    except ComplianceBlockedError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        _log_internal_error("chat_generate", e)
        raise HTTPException(
            status_code=503 if "503" in str(e) or "UNAVAILABLE" in str(e) else 500,
            detail="Model service is temporarily unavailable.",
        )

    # 7. Output guard — catch leaked system content
    answer = check_output(answer, chunks)

    # 8. Save messages to DB (original question for accurate history)
    assistant = _save_messages(db, session.id, req.question, answer)

    return ChatResponse(
        answer=answer,
        chunks_used=len(chunks),
        message_id=assistant.id,
    )


@router.post("/chat/stream", tags=["Chat"])
async def chat_stream_endpoint(
    req: ChatRequest,
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """
    Streaming version of /chat — returns Server-Sent Events (SSE).
    Includes chat history (last 5 turns) in context.
    Security: Input guardrails run before streaming begins.
    Performance: Guardrail LLM check + retrieval run in parallel.
    Requires authentication.

    SSE event types emitted to the client:
        {"type": "heartbeat"}                          — Keep-alive while Gemini generates
        {"token": "<text>"}                            — Verified answer chunk
        {"type": "low_confidence", "confidence": …}   — No confident answer; suggest escalation
        {"error": "<message>"}                         — Unrecoverable generation error
        {"done": true, "chunks_used": N, "message_id": N} — Stream complete
    """
    # 0. Tier 1 regex — instant, no API cost
    check_chat_rate_limit(str(current_user.id))
    regex_guard = _check_regex(req.question)
    if regex_guard.blocked:
        message = regex_guard.message or ""
        session = _get_or_create_session(db, req.session_id, current_user)
        assistant = _save_messages(db, session.id, req.question, message)

        async def blocked_stream():
            payload = json.dumps({"token": message})
            yield f"data: {payload}\n\n"
            done_payload = json.dumps({
                "done": True,
                "chunks_used": 0,
                "message_id": assistant.id,
            })
            yield f"data: {done_payload}\n\n"
        return StreamingResponse(blocked_stream(), media_type="text/event-stream")

    # 1. Sanitize question & set up session/history (fast DB queries)
    clean_question = sanitize_question(req.question)
    session = _get_or_create_session(db, req.session_id, current_user)
    history = _load_chat_history(db, session.id)

    # 2. Run LLM guardrail + retrieval IN PARALLEL. Retrieval decomposes
    #    multi-part questions into standalone sub-queries retrieved in
    #    parallel and RRF-fused (rag/decompose.py).
    guard_task = asyncio.to_thread(check_input, req.question)
    visibility_tiers = _allowed_visibility_tiers(current_user)
    retrieve_task = asyncio.to_thread(
        decomposed_retrieve_with_scores, clean_question, 5, visibility_tiers
    )

    try:
        guard, scored_chunks = await asyncio.gather(guard_task, retrieve_task)
    except FileNotFoundError:
        async def error_stream():
            payload = json.dumps({"error": "Policy index not found. Please trigger /ingest first."})
            yield f"data: {payload}\n\n"
        return StreamingResponse(error_stream(), media_type="text/event-stream")

    # 3. If guardrail blocked, discard retrieval results
    if guard.blocked:
        message = guard.message or ""
        assistant = _save_messages(db, session.id, req.question, message)

        async def blocked_stream_llm():
            payload = json.dumps({"token": message})
            yield f"data: {payload}\n\n"
            done_payload = json.dumps({
                "done": True,
                "chunks_used": 0,
                "message_id": assistant.id,
            })
            yield f"data: {done_payload}\n\n"
        return StreamingResponse(blocked_stream_llm(), media_type="text/event-stream")

    chunks = [row["text"] for row in scored_chunks]
    # Hybrid retrieval orders rows by fused relevance, not raw distance —
    # confidence gating should still reflect the closest dense match found.
    best_distance = min((row["distance"] for row in scored_chunks), default=float("inf"))
    scale = float(os.getenv("A2A_EMBEDDING_SCALE", "1.5"))
    threshold = float(os.getenv("A2A_CONFIDENCE_THRESHOLD", "0.45"))
    # Clamp to [0.0, 1.0] — distance > scale gives 0; distance ≈ 0 gives 1.
    # TODO(calibration): Validate threshold against labeled query/answer pairs
    # from logged agent.query_rag calls.  Pull the last N calls, hand-label
    # whether escalation was correct, and tune A2A_CONFIDENCE_THRESHOLD against
    # the resulting precision/recall curve.  The current 0.45 is a heuristic.
    confidence = max(0.0, min(1.0, 1.0 - (best_distance / scale)))

    if confidence < threshold:
        async def low_confidence_stream():
            """Stream a low-confidence SSE event without calling Gemini."""
            payload = json.dumps({
                "type": "low_confidence",
                "confidence": round(confidence, 3),
                "message": "I could not find a confident answer in the knowledge base.",
                "suggest_escalation": True,
            })
            yield f"data: {payload}\n\n"

        return StreamingResponse(low_confidence_stream(), media_type="text/event-stream")

    # 4. Build prompt + contents
    system_prompt = build_prompt(chunks)
    contents = _build_gemini_contents(system_prompt, history, clean_question)
    try:
        require_external_ai_approval("chat streaming answer generation")
    except ComplianceBlockedError as e:
        raise HTTPException(status_code=503, detail=str(e))

    # Heartbeat interval: emit a keep-alive SSE event every N seconds while
    # Gemini is generating so the browser/proxy doesn't close an idle connection.
    _HEARTBEAT_INTERVAL_SECONDS = env_int("STREAM_HEARTBEAT_INTERVAL_SECONDS", 2)

    async def event_generator():
        """Buffer Gemini's stream, verify it, then emit safe SSE chunks.

        The blocking Gemini stream iterator runs in a thread so it never
        stalls the event loop. Raw model tokens are accumulated server-side
        and checked as they arrive, but are never exposed to the client until
        the complete answer passes the output guard.

        While waiting for Gemini to finish generating, periodic heartbeat SSE
        events are emitted so the client knows the connection is still alive.
        """
        queue: asyncio.Queue = asyncio.Queue()
        verified_buffer = VerifiedStreamBuffer(chunks=chunks)
        # Use asyncio.get_running_loop() — asyncio.get_event_loop() is
        # deprecated in Python 3.10+ inside a running async context.
        loop = asyncio.get_running_loop()

        def _stream_to_queue():
            """Run in a thread: push each token onto the queue, then a sentinel.

            Tries each configured model in order. Falling back is only safe
            *before* any token has been emitted, because emitted tokens are
            already accumulated by the consumer; once streaming has begun a
            failure is surfaced rather than restarted on a different model.
            """
            models = gemini_models()
            emitted = False
            last_error: Exception | None = None
            try:
                for model_index, model in enumerate(models):
                    try:
                        stream = _get_genai_client().models.generate_content_stream(
                            model=model,
                            contents=contents,
                        )
                        for chunk in stream:
                            if chunk.text:
                                emitted = True
                                loop.call_soon_threadsafe(
                                    queue.put_nowait, ("token", chunk.text)
                                )
                        return  # Completed successfully on this model.
                    except Exception as exc:
                        last_error = exc
                        can_fallback = (
                            not emitted
                            and is_retryable_llm_error(str(exc))
                            and model_index < len(models) - 1
                        )
                        if can_fallback:
                            logger.warning(
                                "Gemini stream model exhausted; falling back",
                                extra={"model": model, "next_model": models[model_index + 1]},
                            )
                            continue
                        raise
                if last_error is not None:
                    raise last_error
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

        # Start the blocking stream in a background thread.
        # Use the already-captured loop reference, not get_event_loop().
        thread_task = loop.run_in_executor(None, _stream_to_queue)

        try:
            while True:
                try:
                    # Wait up to _HEARTBEAT_INTERVAL_SECONDS before emitting a
                    # keep-alive heartbeat.  This prevents proxy / browser
                    # timeouts during slow Gemini responses.
                    kind, value = await asyncio.wait_for(
                        queue.get(),
                        timeout=_HEARTBEAT_INTERVAL_SECONDS,
                    )
                except TimeoutError:
                    # No token yet — emit heartbeat and keep waiting
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
                    continue

                if kind == "token":
                    # add() intentionally has no return value: raw model text
                    # remains server-side until finalize() verifies everything.
                    verified_buffer.add(value)
                elif kind == "error":
                    error_payload = json.dumps({"error": value})
                    yield f"data: {error_payload}\n\n"
                    return
                elif kind == "done":
                    full_answer = verified_buffer.finalize()

                    # Only verified content crosses the server boundary.
                    for safe_chunk in _verified_answer_chunks(full_answer):
                        payload = json.dumps({"token": safe_chunk})
                        yield f"data: {payload}\n\n"

                    # ── Save to DB first, THEN signal completion ──────────────
                    message_id = None
                    save_db = SessionLocal()
                    try:
                        assistant = _save_messages(
                            save_db,
                            session.id,
                            req.question,
                            full_answer,
                        )
                        message_id = assistant.id
                    except Exception as e:
                        inc_counter("chat_stream_save_failures_total")
                        logger.exception("Failed to save streamed chat messages", exc_info=e)
                    finally:
                        save_db.close()
                    done_payload = json.dumps({
                        "done": True,
                        "chunks_used": len(chunks),
                        "message_id": message_id,
                    })
                    yield f"data: {done_payload}\n\n"
                    break
        finally:
            await thread_task  # ensure the thread is cleaned up

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ---------------------------------------------------------------------------
# Session management routes
# ---------------------------------------------------------------------------

@router.get("/chat/sessions", tags=["Chat"])
def list_sessions(
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """
    Return all chat sessions for the authenticated user, newest first.
    Each item includes a preview (first user message, truncated) and message count.
    """
    from sqlalchemy import func

    first_user_message = (
        db.query(
            ChatMessage.session_id.label("session_id"),
            func.min(ChatMessage.id).label("message_id"),
        )
        .filter(ChatMessage.role == "user")
        .group_by(ChatMessage.session_id)
        .subquery()
    )
    message_counts = (
        db.query(
            ChatMessage.session_id.label("session_id"),
            func.count(ChatMessage.id).label("message_count"),
        )
        .group_by(ChatMessage.session_id)
        .subquery()
    )

    sessions = (
        db.query(
            ChatSession,
            ChatMessage.content.label("first_message"),
            func.coalesce(message_counts.c.message_count, 0).label("message_count"),
        )
        .outerjoin(
            first_user_message,
            first_user_message.c.session_id == ChatSession.id,
        )
        .outerjoin(ChatMessage, ChatMessage.id == first_user_message.c.message_id)
        .outerjoin(message_counts, message_counts.c.session_id == ChatSession.id)
        .filter(ChatSession.user_id == current_user.id)
        .order_by(ChatSession.created_at.desc())
        .all()
    )

    result = []
    for session, first_message, message_count in sessions:
        preview = (
            first_message[:72] + "…"
            if first_message and len(first_message) > 72
            else (first_message or "Empty session")
        )
        result.append({
            "session_id": session.id,
            "preview": preview,
            "message_count": message_count,
            "created_at": format_utc_datetime(session.created_at),
        })

    return {"sessions": result}


@router.delete("/chat/sessions/{session_id}", tags=["Chat"])
def delete_session(
    session_id: str,
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """
    Delete a chat session (and all its messages) for the authenticated user.
    Users can only delete their own sessions.
    """
    session = db.query(ChatSession).filter(
        ChatSession.id == session_id,
        ChatSession.user_id == current_user.id,
    ).first()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    db.delete(session)
    db.commit()
    return {"deleted": True, "session_id": session_id}


@router.get("/chat/history/{session_id}", tags=["Chat"])
def get_chat_history(
    session_id: str,
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """
    Retrieve message history for a specific session.
    Users can only access their own sessions.
    """
    session = db.query(ChatSession).filter(
        ChatSession.id == session_id,
        ChatSession.user_id == current_user.id,
    ).first()

    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at)
        .all()
    )
    return {
        "session_id": session_id,
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "created_at": format_utc_datetime(m.created_at),
            }
            for m in messages
        ],
    }
