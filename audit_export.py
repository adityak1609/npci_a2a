"""Export and verify append-only audit logs.

Production usage:
  1. Run `python audit_export.py export --out audit_exports`.
  2. Push the resulting directory to immutable storage such as S3 Object Lock,
     Azure immutable blob storage, or another WORM target.
  3. Alert when export or verification fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from database import A2AMessage, AuditLog, SessionLocal, _audit_chain_payload

load_dotenv()


def _dt(value):
    return value.isoformat() if value else None


def _audit_log_row(row: AuditLog) -> dict:
    return {
        "table": "audit_logs",
        "id": row.id,
        "admin_id": row.admin_id,
        "action": row.action,
        "target": row.target,
        "detail": row.detail,
        "prev_hash": row.prev_hash,
        "entry_hash": row.entry_hash,
        "created_at": _dt(row.created_at),
    }


def _a2a_row(row: A2AMessage) -> dict:
    return {
        "table": "a2a_messages",
        "id": row.id,
        "message_id": row.message_id,
        "task_type": row.task_type,
        "direction": row.direction,
        "change_id": row.change_id,
        "cflow_id": row.cflow_id,
        "agent_id": row.agent_id,
        "agent_run_id": row.agent_run_id,
        "correlation_id": row.correlation_id,
        "caller_ip": row.caller_ip,
        "jwt_sub": row.jwt_sub,
        "latency_ms": row.latency_ms,
        "error_code": row.error_code,
        "payload_ref": row.payload_ref,
        "result_payload": row.result_payload,
        "http_status": row.http_status,
        "prev_hash": row.prev_hash,
        "entry_hash": row.entry_hash,
        "created_at": _dt(row.created_at),
    }


def _write_jsonl(path: Path, rows: list[dict]) -> str:
    h = hashlib.sha256()
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            line = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
            handle.write(line + "\n")
            h.update(line.encode("utf-8"))
            h.update(b"\n")
    return h.hexdigest()


def export_audit_logs(out_dir: str) -> dict:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    db = SessionLocal()
    try:
        audit_rows = [_audit_log_row(row) for row in db.query(AuditLog).order_by(AuditLog.id).all()]
        a2a_rows = [_a2a_row(row) for row in db.query(A2AMessage).order_by(A2AMessage.id).all()]
    finally:
        db.close()

    audit_file = out_path / f"audit_logs_{stamp}.jsonl"
    a2a_file = out_path / f"a2a_messages_{stamp}.jsonl"
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": {
            audit_file.name: {
                "rows": len(audit_rows),
                "sha256": _write_jsonl(audit_file, audit_rows),
            },
            a2a_file.name: {
                "rows": len(a2a_rows),
                "sha256": _write_jsonl(a2a_file, a2a_rows),
            },
        },
    }
    manifest_file = out_path / f"manifest_{stamp}.json"
    manifest_file.write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return {"manifest": str(manifest_file), **manifest}


_AUDIT_FIELDS = ["admin_id", "action", "target", "detail", "created_at"]
_A2A_FIELDS = [
    "message_id",
    "protocol_version",
    "task_type",
    "direction",
    "change_id",
    "cflow_id",
    "agent_id",
    "agent_run_id",
    "correlation_id",
    "caller_ip",
    "jwt_sub",
    "latency_ms",
    "error_code",
    "payload_ref",
    "result_payload",
    "http_status",
    "created_at",
]


def _verify_chain(rows, label: str, fields: list[str]) -> list[str]:
    errors: list[str] = []
    previous = "GENESIS"
    for row in rows:
        if row.prev_hash != previous:
            errors.append(
                f"{label} id={row.id}: prev_hash={row.prev_hash} expected={previous}"
            )
        expected_hash = hashlib.sha256(
            f"{row.prev_hash}.{_audit_chain_payload(row, fields)}".encode()
        ).hexdigest()
        if row.entry_hash != expected_hash:
            errors.append(
                f"{label} id={row.id}: entry_hash={row.entry_hash} expected={expected_hash}"
            )
        if not row.entry_hash:
            errors.append(f"{label} id={row.id}: missing entry_hash")
        previous = row.entry_hash or ""
    return errors


def verify_audit_chains() -> dict:
    db = SessionLocal()
    try:
        audit_rows = db.query(AuditLog).order_by(AuditLog.id).all()
        a2a_rows = db.query(A2AMessage).order_by(A2AMessage.id).all()
        errors = [
            *_verify_chain(audit_rows, "audit_logs", _AUDIT_FIELDS),
            *_verify_chain(a2a_rows, "a2a_messages", _A2A_FIELDS),
        ]
        return {
            "ok": not errors,
            "errors": errors,
            "audit_logs": len(audit_rows),
            "a2a_messages": len(a2a_rows),
        }
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit export and verification utilities.")
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("--out", required=True)
    sub.add_parser("verify")
    args = parser.parse_args()

    if args.command == "export":
        print(json.dumps(export_audit_logs(args.out), indent=2, sort_keys=True))
    elif args.command == "verify":
        result = verify_audit_chains()
        print(json.dumps(result, indent=2, sort_keys=True))
        if not result["ok"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
