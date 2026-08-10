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

The deployer accepts only the explicit runtime allowlist in `fabfile.py`. It never imports
database, host, cookie, Django secret, or debug overrides from `.env-prod`. Values containing
CR/LF are rejected. The persistent server `.env` is mode `0600` and is updated atomically.

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
cron locks, creates an encrypted and integrity-checked SQLite backup when the database exists,
runs migrations, collects static files, and runs `check --deploy`. It then installs the checked-in
`operationalinbox-mcp.service`, adds the managed `/mcp` Nginx proxy include, validates Nginx, and
restarts both runtimes. MCP listens only on loopback port `8012`; public traffic remains on
`https://operationalinbox.com/mcp`. Backups are stored under `/var/backups/operationalinbox` for
30 days.
