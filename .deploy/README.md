# Operational Inbox deployment

This directory deploys the tracked `origin/main` release to the StageOps `hetzner-stage`
host. It deliberately does not build Tailwind or run Node in production; the compiled CSS is
tracked and WhiteNoise serves collected static files.

## Prerequisites

1. Install the deployment GitHub App with read access to the private
   `onurmatik/operational-inbox` repository. Copy `.credentials.env.example` to the ignored
   `.credentials.env`, fill its App ID, installation ID, and local private-key path, then set mode
   `0600`. Fabric mints a short-lived installation token locally and uses it only as an ephemeral
   HTTPS header for remote Git operations. No GitHub credential is installed on the host or saved
   in the repository's remote URL.
2. Deploy the CDK stack in `us-east-1`, create an access key for the emitted
   `operational-inbox-hetzner` IAM user out of band, and place the non-empty runtime values in
   the ignored root `.env-prod` file.
   Production is fixed to `DJANGO_EMAIL_BACKEND=ses`; verification and notification email must
   never use the console backend on the server.
3. Generate `BACKUP_ENCRYPTION_KEY` with
   `python3 -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`.
4. Quote values containing spaces so the StageOps cron environment remains POSIX-shell
   sourceable, for example `DEFAULT_FROM_EMAIL='Operational Inbox <notifications@operationalinbox.com>'`.
5. OAuth defaults to the production `PUBLIC_BASE_URL`; override `MCP_RESOURCE_URL` or
   `OAUTH_ISSUER` only when the externally visible canonical URLs differ. During OpenAI domain
   verification, place the portal token in `OPENAI_APPS_CHALLENGE_TOKEN` and redeploy.

The deployer accepts only the explicit runtime allowlist in `fabfile.py`. Normal deploys never
import database, host, cookie, Django secret, or debug overrides from `.env-prod`. Database
changes use the separate guarded PostgreSQL tasks below. Values containing CR/LF are rejected.
The persistent server `.env` is mode `0600` and is updated atomically.

## Deploy

```console
cp .deploy/.credentials.env.example .deploy/.credentials.env
chmod 600 .deploy/.credentials.env
# Fill the three GitHub App values in .deploy/.credentials.env.

python3 -m pip install -r .deploy/requirements.txt
cd .deploy
python3 -m fabric deploy
```

Deployment stops the cold Gunicorn service and the dedicated MCP Uvicorn service, acquires all
cron locks, creates an encrypted and integrity-checked backup of the active database, runs
migrations, collects static files, and runs `check --deploy`. It then installs the checked-in
`operationalinbox-mcp.service`, waits up to 30 seconds for its MCP initialize response, and
restarts both runtimes. MCP listens only on loopback port `8012`; StageOps owns the Nginx
`/mcp` proxy route that exposes `https://operationalinbox.com/mcp`. Backups are stored under
`/var/backups/operationalinbox` for 30 days.

## PostgreSQL 17 cutover

Keep `DJANGO_DATABASE_URL` out of `.env-prod` for the first PostgreSQL-capable `fab deploy`. After
that release is live on SQLite, add the PostgreSQL URL to the ignored mode-`0600` file and run:

```console
cd .deploy
python3 -m fabric prepare-postgresql
python3 -m fabric cutover-postgresql
```

Preparation creates the least-privileged `operationalinbox_app` role and isolated
`operationalinbox` database, migrates the empty schema, and runs real PostgreSQL concurrency,
sequence, privilege, and write checks without changing the runtime `.env`. Cutover stages only the
database URL, holds every app/cron lock, stops web and MCP writers, backs up and validates SQLite,
loads PostgreSQL, compares deterministic model manifests, resets and validates every Django
sequence, takes encrypted `pg_dump` and pgBackRest backups, and opens services only after all
checks pass. A pre-open failure restores SQLite automatically; after services open, recovery is
PostgreSQL-only roll-forward.

`backup_database` dispatches to SQLite's online backup API or PostgreSQL custom-format `pg_dump`.
The legacy `backup_sqlite` command remains as a backend-aware alias for the existing StageOps cron.
