import asyncio
import os
from typing import Optional
from sqlalchemy.orm import Session
from auth import User
from database import ChatSession, ChatMessage
from guardrails import sanitize_question
from config import GEMINI_MODEL, VERIFIED_STREAM_CHUNK_SIZE, CHAT_MEMORY_SIZE, MAX_RETRIES
import time
from telemetry import _log_internal_error
from metrics import time_request
from metrics import observe_histogram
from compliance import require_external_ai_approval
import logging
logger = logging.getLogger(__name__)
_RETRYABLE = ["429", "503", "500", "ReadTimeout"]
from agent.dispatcher import _get_genai_client
from datetime import timezone

def _generate_content_once(contents: list) -> str:
    response = _get_genai_client().models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
    )
    return response.text or ""




async def _generate_with_retry(contents: list) -> str:
    """Call Gemini generate_content with fast retry on transient errors."""
    require_external_ai_approval("chat answer generation")
    for attempt in range(MAX_RETRIES + 1):
        try:
            started = time_request()
            result = await asyncio.to_thread(_generate_content_once, contents)
            observe_histogram(
                "gemini_api_latency_seconds",
                time_request() - started,
                labels={"call_type": "chat"},
            )
            return result
        except Exception as e:
            err = str(e)
            if any(code in err for code in _RETRYABLE) and attempt < MAX_RETRIES:
                wait = min(attempt + 1, 2)  # 1s, 2s — fast retries
                logger.warning(
                    "Gemini transient error; retrying",
                    extra={"attempt": attempt + 1, "max_retries": MAX_RETRIES, "wait": wait},
                )
                await asyncio.sleep(wait)
            else:
                raise
    raise RuntimeError("Gemini retry loop exhausted without a response.")




def _verified_answer_chunks(answer: str, size: int = VERIFIED_STREAM_CHUNK_SIZE):
    """Yield a fully verified answer in small SSE-friendly text chunks."""
    for start in range(0, len(answer), size):
        yield answer[start : start + size]




def _get_or_create_session(db, session_id: str, user: User) -> ChatSession:
    """Return existing session or create a new one for this user."""
    session = db.query(ChatSession).filter(
        ChatSession.id == session_id,
        ChatSession.user_id == user.id,
    ).first()

    if not session:
        session = ChatSession(id=session_id, user_id=user.id)
        db.add(session)
        db.commit()
        db.refresh(session)

    return session




def _load_chat_history(db, session_id: str, limit: int = CHAT_MEMORY_SIZE * 2) -> list[dict]:
    """
    Load the last `limit` messages from a session.

    Returns a list of dicts like: [{"role": "user", "content": "..."}, ...]
    CHAT_MEMORY_SIZE * 2 because each turn = 1 user + 1 assistant message.

    Note: User messages in history are sanitized to prevent memory poisoning —
    a past malicious message should not influence future LLM responses.
    """
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(limit)
        .all()
    )
    # Reverse so they're in chronological order
    # Sanitize user messages to prevent memory-poisoning through persisted history
    history = []
    for m in reversed(messages):
        content = sanitize_question(m.content) if m.role == "user" else m.content
        history.append({"role": m.role, "content": content})
    return history




def _allowed_visibility_tiers(user: User) -> list[str] | None:
    """Return KB visibility tiers this user may retrieve; admins see all tiers."""
    if user.is_admin:
        return None
    tier = (user.visibility_tier or "public").lower()
    order = ["public", "staff", "hr", "admin"]
    if tier not in order:
        tier = "public"
    return order[: order.index(tier) + 1]




def _save_messages(db, session_id: str, user_msg: str, assistant_msg: str) -> ChatMessage:
    """Persist a chat turn and return the saved assistant message."""
    db.add(ChatMessage(session_id=session_id, role="user", content=user_msg))
    assistant = ChatMessage(
        session_id=session_id,
        role="assistant",
        content=assistant_msg,
    )
    db.add(assistant)
    db.commit()
    db.refresh(assistant)
    return assistant




def _build_gemini_contents(system_prompt: str, history: list[dict], question: str) -> list:
    """
    Build the `contents` list for the Gemini API call, including chat history.

    Format:
        1. System prompt (as first user turn)
        2. Bot acknowledgment
        3. Past conversation history (alternating user/assistant)
        4. Current user question
    """
    contents = [
        {"role": "user", "parts": [{"text": system_prompt}]},
        {
            "role": "model",
            "parts": [{"text": "Understood. I will answer strictly based on the provided policy context."}],
        },
    ]

    # Inject chat history
    for msg in history:
        role = "model" if msg["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})

    # Current question
    contents.append({"role": "user", "parts": [{"text": question}]})
    return contents


def format_utc_datetime(dt) -> str:
    """Ensure a datetime object is returned as a timezone-aware UTC ISO string ending in Z."""
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


