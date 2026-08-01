# Operational Inbox deployment

This directory deploys the tracked `origin/main` release to the StageOps `hetzner-stage`
host. It deliberately does not build Tailwind or run Node in production; the compiled CSS is
tracked and WhiteNoise serves collected static files.

## Prerequisites

1. Create the private GitHub repository `onurmatik/operational-inbox` and push this project to
   its `main` branch.
   Load a GitHub-authorized SSH key into the local agent before deploying, or install a read-only
   deploy key for that repository on the host. Fabric forwards the local agent and clones over
   SSH; no GitHub token is written to disk.
2. Deploy the CDK stack in `us-east-1`, create an access key for the emitted
   `operational-inbox-hetzner` IAM user out of band, and place the non-empty runtime values in
   the ignored root `.env-prod` file.
   Production is fixed to `DJANGO_EMAIL_BACKEND=ses`; verification and notification email must
   never use the console backend on the server.
3. Generate `BACKUP_ENCRYPTION_KEY` with
   `python3 -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`.
4. Quote values containing spaces so the StageOps cron environment remains POSIX-shell
   sourceable, for example `DEFAULT_FROM_EMAIL='Operational Inbox <notifications@operationalinbox.com>'`.

The deployer accepts only the explicit runtime allowlist in `fabfile.py`. It never imports
database, host, cookie, Django secret, or debug overrides from `.env-prod`. Values containing
CR/LF are rejected. The persistent server `.env` is mode `0600` and is updated atomically.

## Deploy

```console
python3 -m pip install -r .deploy/requirements.txt
cd .deploy
python3 -m fabric deploy
```

Deployment stops the cold Gunicorn service, acquires all cron locks, creates an encrypted and
integrity-checked SQLite backup when the database exists, runs migrations, collects static
files, runs `check --deploy`, and restarts the socket. Backups are stored under
`/var/backups/operationalinbox` for 30 days.
