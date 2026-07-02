# Deployment Secret Mounts

Do not commit real secret files here.

For local smoke tests of the production compose stack, create files with these
names and real deployment values:

- `secret_key`
- `database_url`
- `redis_url`
- `google_api_key`
- `npci_api_key`
- `a2a_hmac_secret`
- `smtp_pass`

In live production, prefer your orchestrator or cloud secret manager to mount
the same paths under `/run/secrets`.
