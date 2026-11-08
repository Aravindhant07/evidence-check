# Runbook

## Deploy

```bash
docker compose up -d                     # api (UI + REST) and one worker, data in the evcheck-data volume
docker compose run --rm api evcheck models download   # once, with network access; then the stack runs offline
docker compose run --rm api evcheck keys add --role admin
docker compose up -d --scale worker=3    # more throughput: workers share the queue safely
```

Health: `GET /healthz` (process up), `GET /readyz` (database reachable, queue depth, history size, models).

Without Docker: `pip install ".[models,mcp]"`, then `evcheck serve --data /srv/evcheck --host 0.0.0.0` and
`evcheck worker --data /srv/evcheck` (one or more). The server refuses to listen beyond localhost without API keys.

## Access

| Role | Can |
|---|---|
| intake | submit cases (for integrations) |
| reviewer | read the queue and cases (masked PII), confirm or dismiss reasons |
| investigator | everything a reviewer can, unmasked PII, close cases |
| admin | everything, plus the audit log |

Keys: `evcheck keys add --role reviewer` (shown once; stored in `<data>/keys.json`, mode 600), or `EVCHECK_KEYS`.
Revoke by deleting the line from keys.json and restarting.

## Integrations

- Inbound: `POST /api/cases` (multipart: `case` JSON + files), or `evcheck watch INBOX`.
- Outbound: `--webhook-url` with `EVCHECK_WEBHOOK_SECRET`. Each delivery has `X-Evcheck-Timestamp` and
  `X-Evcheck-Signature: sha256=HMAC(secret, timestamp + "." + body)`; receivers should reject signatures older than
  5 minutes (`evidence_check.integrations.verify`). Failed deliveries are retried and then recorded in the audit log;
  the verdict is never lost.
- Agents: `evcheck mcp` exposes read-only investigation tools; agents cannot decide or close cases.

## Monitoring and what to do

| Signal | Where | Action |
|---|---|---|
| Drift alert on a check | dashboard | Look at today's cases where it fired. New phone model or scanner? A partner changed their export (stripping metadata)? A real new pattern? Adjust or suppress through a policy proposal, not by editing live config. |
| Queue growing | `/readyz`, dashboard | Add workers. CPU-only throughput is roughly 10–40 s per case with all models; metadata-only checking is under a second. |
| Jobs failing | dashboard ("failed") | Each job retries 3 times; the error is stored on the job. Usually a corrupt file: the case shows an `ingest` finding instead. |
| Low reviewer agreement on a check | dashboard | Tune via calibration on recent closed cases (`evcheck calibrate --data ...`). |
| Audit log not verified | dashboard, `evcheck audit` | Treat as a security incident: someone edited the database. Restore from backup, investigate access. |

## Changing models

Models are pinned to full commits in `models.yaml` and loaded as safetensors without remote code. To change one:
update the pin, run `evcheck models download` and `evcheck models verify`, rerun the benchmark and the golden sets,
and compare with the previous run before deploying. Never point a model at a branch name.

## Backups and retention

Back up `<data>/evcheck.db` (SQLite, WAL mode: use `sqlite3 evcheck.db ".backup backup.db"`), `<data>/history.jsonl`
and `<data>/cases/`. Run `evcheck retention --days N` on a schedule agreed with the client's data protection officer.
