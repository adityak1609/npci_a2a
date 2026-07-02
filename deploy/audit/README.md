# Immutable Audit Export

The application keeps hash-chained `audit_logs` and `a2a_messages` rows in
Postgres. Production still needs an external immutable destination.

Recommended destinations:

- S3 bucket with Object Lock in compliance mode
- Azure Blob immutable storage with time-based retention
- GCS bucket retention policy plus legal hold controls
- WORM storage controlled by the compliance team

Operator flow:

1. Run `python audit_export.py verify`.
2. Run `python audit_export.py export --out audit_exports`.
3. Upload the exported JSONL files and manifest to the immutable destination.
4. Store the manifest hash in object metadata or a separate compliance register.
5. Alert on any verification or upload failure.

Keep the destination write-only for the application/operator identity. Retention,
legal hold, and deletion privileges should belong to a separate break-glass role.
