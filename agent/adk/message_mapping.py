"""Translation between Google-A2A messages and the bank's A2A envelope.

The bank↔NPCI contract we define for the Google-A2A transport is deliberately
simple and robust (it avoids ADK's internal ``<a2a_datapart_json>`` DataPart
encoding, which is an implementation detail):

    * **one ``TextPart``** carrying the envelope as JSON (the same shape that
      ``A2AEnvelope`` already validates), and
    * **one ``FilePart(FileWithBytes)`` per ``product_kit`` document** for
      ``change_communication`` — so large binaries travel as real file parts,
      not base64 stuffed into text.

Two directions:

    outbound (client side)  envelope dict  → :func:`envelope_to_a2a_message`
    inbound  (agent side)   genai parts    → :func:`genai_parts_to_envelope`

Inbound runs *inside* an ADK agent, where A2A parts have already been converted
to ``google.genai`` parts (TextPart→``text``; FilePart(bytes)→``inline_data``),
so the inbound mapper reads genai parts. Outbound builds native A2A parts.
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any, Optional

from a2a.types import (
    FilePart,
    FileWithBytes,
    Message,
    Part,
    Role,
    TextPart,
)
from google.genai import types as genai_types

from a2a_models import A2AEnvelope, A2ATaskType

# Documents in a change_communication are carried as FileParts; this mime tags
# them so the receiver treats the bytes as a document rather than inline text.
_DOC_MIME = "application/pdf"


# ---------------------------------------------------------------------------
# Outbound: envelope dict → A2A Message (native A2A parts)
# ---------------------------------------------------------------------------

def envelope_to_a2a_message(
    envelope_wire: dict[str, Any],
    *,
    split_documents: bool = True,
) -> Message:
    """Build a Google-A2A ``Message`` from a wire-format envelope dict.

    For ``change_communication`` (when ``split_documents``), each
    ``product_kit[].content_b64`` is peeled off into its own ``FilePart`` and
    removed from the JSON text part; the receiver re-attaches them by filename.
    All other task types serialize to a single ``TextPart``.
    """
    envelope = json.loads(json.dumps(envelope_wire, default=str))  # deep copy, JSON-safe
    file_parts: list[Part] = []

    if split_documents and envelope.get("task_type") == A2ATaskType.CHANGE_COMMUNICATION.value:
        payload = envelope.get("payload") or {}
        for entry in payload.get("product_kit", []) or []:
            content_b64 = entry.get("content_b64")
            filename = entry.get("filename")
            if not content_b64 or not filename:
                continue
            file_parts.append(
                Part(
                    root=FilePart(
                        file=FileWithBytes(
                            bytes=content_b64,  # A2A FileWithBytes carries base64 text
                            mime_type=_DOC_MIME,
                            name=filename,
                        )
                    )
                )
            )
            # Drop the bytes from the text part; the FilePart carries them now.
            entry.pop("content_b64", None)

    text_part = Part(root=TextPart(text=json.dumps(envelope, separators=(",", ":"), default=str)))
    message_id = str(envelope.get("message_id") or uuid.uuid4().hex)
    return Message(
        role=Role.user,  # the sender is the initiator of this exchange
        parts=[text_part, *file_parts],
        message_id=message_id,
    )


# ---------------------------------------------------------------------------
# Inbound: genai parts (inside the ADK agent) → A2AEnvelope
# ---------------------------------------------------------------------------

def _genai_files_by_name(parts: list[genai_types.Part]) -> dict[str, bytes]:
    """Collect inline binary parts keyed by their display name (filename)."""
    files: dict[str, bytes] = {}
    for part in parts:
        blob = getattr(part, "inline_data", None)
        if blob is not None and blob.data is not None and blob.mime_type != "text/plain":
            name = blob.display_name or f"document_{len(files)}"
            files[name] = blob.data
    return files


def _genai_envelope_json(parts: list[genai_types.Part]) -> Optional[dict[str, Any]]:
    """Return the first text part parsed as a JSON envelope dict, if any."""
    for part in parts:
        text = getattr(part, "text", None)
        if text:
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, dict):
                return data
    return None


def genai_parts_to_envelope(parts: list[genai_types.Part]) -> A2AEnvelope:
    """Reconstruct the ``A2AEnvelope`` from the genai parts of an inbound A2A message.

    For ``change_communication`` the document bytes (delivered as FileParts →
    genai ``inline_data``) are re-injected as ``content_b64`` into the matching
    ``product_kit`` entry, so the existing ingestion handler is reused unchanged.
    """
    envelope_dict = _genai_envelope_json(parts)
    if not envelope_dict:
        raise ValueError("A2A message has no JSON envelope text part.")

    if envelope_dict.get("task_type") == A2ATaskType.CHANGE_COMMUNICATION.value:
        files = _genai_files_by_name(parts)
        payload = envelope_dict.get("payload") or {}
        for entry in payload.get("product_kit", []) or []:
            if entry.get("content_b64"):
                continue
            raw = files.get(entry.get("filename"))
            if raw is not None:
                entry["content_b64"] = base64.b64encode(raw).decode("ascii")

    return A2AEnvelope.model_validate(envelope_dict)


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------

def result_to_genai_content(result: dict[str, Any]) -> genai_types.Content:
    """Wrap a handler result dict as model content the ADK agent yields.

    Serialized to a single text part; the A2A layer converts it back to a
    ``TextPart`` in the task artifact returned to the caller.
    """
    text = json.dumps(result, default=str)
    return genai_types.Content(role="model", parts=[genai_types.Part(text=text)])


def extract_result_dict(events: list[Any]) -> dict[str, Any]:
    """Parse the JSON result from an A2A ``send_message`` response stream.

    Accepts the tuples ``(Task, update)`` and bare ``Message`` objects yielded
    by ``a2a.client.Client.send_message`` and returns the first JSON object
    found in any text part (the handler result), or ``{}`` if none is present.
    """
    for event in reversed(events):
        message = event if isinstance(event, Message) else None
        task = None
        if isinstance(event, tuple) and event:
            task = event[0]
        candidates: list[Any] = []
        if message is not None:
            candidates.extend(message.parts)
        if task is not None:
            for artifact in getattr(task, "artifacts", None) or []:
                candidates.extend(getattr(artifact, "parts", None) or [])
            status_msg = getattr(getattr(task, "status", None), "message", None)
            if status_msg is not None:
                candidates.extend(getattr(status_msg, "parts", None) or [])
        for part in candidates:
            root = getattr(part, "root", part)
            text = getattr(root, "text", None)
            if text:
                try:
                    data = json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(data, dict):
                    return data
    return {}
