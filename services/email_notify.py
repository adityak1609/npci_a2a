"""Email notification helper for A2A document ingestion."""

import os
import smtplib
from email.message import EmailMessage


def send_ingest_email(filename: str, metadata: dict, summary: str, recipients: list[str]) -> None:
    """agent.ingest_document; params: filename, metadata, summary, recipients; returns None."""
    if not recipients:
        return

    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASS")
    if not host or not user or not password:
        return

    source = (metadata or {}).get("source", "npci_agent")
    timestamp = (metadata or {}).get("timestamp", "")

    message = EmailMessage()
    message["From"] = user
    message["To"] = ", ".join(recipients)
    message["Subject"] = f"[Mindgate A2A] New document received: {filename}"
    message.set_content(
        f"Document  : {filename}\n"
        f"Source    : {source}\n"
        f"Received  : {timestamp}\n\n"
        "Summary\n"
        "-------\n"
        f"{summary}\n\n"
        "This document is now queryable in the Mindgate RAG system.\n"
    )

    with smtplib.SMTP(host, port, timeout=15) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(message)
