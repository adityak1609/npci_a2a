"""Feedback API routes for chat messages."""

import html
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth import User, get_current_user
from database import ChatMessage, ChatSession, MessageFeedback, get_db
from pydantic import BaseModel
from typing import Optional

router = APIRouter()

FEEDBACK_REASONS = {
    "incorrect",
    "incomplete",
    "not_relevant",
    "unclear",
    "outdated",
    "other",
}

# Assuming FeedbackRequest is defined in main.py, I will copy it here or we can import from a schema file.
# Since we are deconstructing main, let's look at where FeedbackRequest is. It's likely in main.py. Let's define it here.
class FeedbackRequest(BaseModel):
    rating: str
    reason: Optional[str] = None
    comment: Optional[str] = None


@router.post("/feedback/{message_id}", tags=["Feedback"])
def submit_feedback(
    message_id: int,
    data: FeedbackRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create or update feedback for one of the user's assistant messages."""
    rating = data.rating.strip().lower()
    if rating not in {"positive", "negative"}:
        raise HTTPException(
            status_code=400,
            detail="Rating must be 'positive' or 'negative'.",
        )

    reason = data.reason.strip().lower() if data.reason else None
    if reason and reason not in FEEDBACK_REASONS:
        raise HTTPException(status_code=400, detail="Invalid feedback reason.")
    if rating == "positive":
        reason = None

    comment = data.comment.strip() if data.comment else None
    if rating == "positive":
        comment = None
    if comment and len(comment) > 1000:
        raise HTTPException(
            status_code=400,
            detail="Feedback comment must be 1000 characters or fewer.",
        )
    # Sanitize to prevent stored XSS when comments are rendered in the admin UI
    if comment:
        comment = html.escape(comment)

    message = (
        db.query(ChatMessage)
        .join(ChatSession, ChatMessage.session_id == ChatSession.id)
        .filter(
            ChatMessage.id == message_id,
            ChatMessage.role == "assistant",
            ChatSession.user_id == current_user.id,
        )
        .first()
    )
    if not message:
        raise HTTPException(status_code=404, detail="Assistant message not found.")

    feedback = (
        db.query(MessageFeedback)
        .filter(
            MessageFeedback.message_id == message_id,
            MessageFeedback.user_id == current_user.id,
        )
        .first()
    )
    if feedback:
        feedback.rating = rating
        feedback.reason = reason
        feedback.comment = comment
    else:
        feedback = MessageFeedback(
            message_id=message_id,
            user_id=current_user.id,
            rating=rating,
            reason=reason,
            comment=comment,
        )
        db.add(feedback)

    db.commit()
    db.refresh(feedback)
    return {
        "saved": True,
        "message_id": message_id,
        "rating": feedback.rating,
        "reason": feedback.reason,
        "comment": feedback.comment,
        "updated_at": (feedback.updated_at or feedback.created_at).isoformat(),
    }
