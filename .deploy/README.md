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

The local checkout must be clean, published as `origin/main`, and pass the read-only
`makemigrations --check --dry-run` check using its `.venv`. Fabric pins that exact SHA, uploads
the checked-in release helper, and runs it in one SSH operation. The deploy lock is held from
Git fetch through source/config/dependency changes, migration, activation, and live checks.

Schema-changing releases pause only `/etc/cron.d/stageops-operationalinbox`, drain every app
cron lock, and stop the web socket, Gunicorn, and MCP before replacing checkout files. A
persistent systemd condition prevents these units from starting while the containment marker
exists, including after reboot. The compatible previous code creates an encrypted, verified
database backup before checkout and migration. The release checks for model drift and pending
migrations, reads every managed model's columns through the ORM, and runs `check --deploy`
before opening services. Cron resumes only after three consecutive public web health checks,
the MCP Bearer challenge, exact revision verification, and individual runtime checks pass.
Verification does not send email or invoke scheduled workloads.

Proven migrationless releases retain a short in-place path: no database backup or full
shutdown unless shared environment, dependencies, or units change. They record the previous
revision and restore it on failure, verify its schema and live health, then resume cron. During
this path existing processes may briefly execute different revisions with the same schema.
Dependencies, collected static files, and unit files are updated only when changed or missing.
Export production dependencies from `uv.lock` before publishing:

```console
uv export --frozen --no-dev --no-hashes --no-emit-project --output-file requirements.txt
```

MCP listens only on loopback port `8012`; StageOps owns the Nginx `/mcp` proxy route.
Backups are stored under `/var/backups/operationalinbox` for 30 days.

## Release failure and recovery

Release state, protected configuration snapshots, and a private diagnostic log are kept under
`/var/lib/operationalinbox-deploy` (mode `0700`). Before migration starts, failure recovery can
restore the previous checkout, environment, dependency requirements, unit, and static files;
services reopen only after the previous schema and live health pass. Once migration starts,
recovery is roll-forward: web/MCP stay stopped, the systemd containment marker persists, and
the cron file remains outside `/etc/cron.d` after the SSH process and locks exit. No `finally`
block restarts an unverified schema. Never print configuration snapshots or raw diagnostic logs.

Inspect the recorded phase, applied migrations, and service state. Repair and publish the
candidate using the existing workflow, then run:

```console
cd .deploy
python3 -m fabric deploy --recover
```

Recovery reacquires the deploy lock, verifies the previous owner is absent, keeps workloads
contained, and runs forward migrations and the same checks. It clears the failure marker and
restores cron only after successful verification. Do not reverse migration `0015` or start the
old revision after it has retired tables; original records are archived as audit events and the
encrypted pre-migration backup remains available for deliberate data recovery.

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
