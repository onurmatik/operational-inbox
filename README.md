# Operational Inbox

Operational Inbox is a tenant-safe control layer for operational email: the addresses a
business must publish and must not ignore, such as `support@`, `privacy@`, `security@`,
`legal@`, `billing@`, and `abuse@`. It receives mail through Amazon SES, preserves the raw
message, presents a complete searchable inbox, highlights work that needs attention, creates
scheduled reviews, and prepares replies that require exact human approval.

This repository contains the production-ready MVP application, AWS email data-plane
infrastructure, and the tracked deployment contract. The application and all product copy are
English.

> **Brand and DNS scope:** `operationalinbox.com` is the only product domain in scope.
> `operational.email` and `operationalemail.com` are deliberately not used. The existing root
> MX records for `operationalinbox.com` must be preserved; only the root A record is moved to the
> application host. SES receiving is configured on `inbound.operationalinbox.com`.

## What is implemented

- Domain-first self-service onboarding with a single email field, throttled 10-minute
  django-sesame magic links for both sign-in and sign-up, and configurable abuse limits.
  The pending domain travels in short-lived, user-bound signed state so the email can be opened
  in another browser. Staff and superuser accounts remain on Django admin's password login.
- One owner per domain, with one user able to own multiple domains. Team roles and memberships
  are intentionally absent from the MVP.
- Direct-MX and provider-forwarding domain onboarding, exact
  DNS instructions, DNS drift checks, test delivery, separate inbound/outbound readiness, and
  safe domain disablement.
- A complete inbox with search and filters, conversation timelines, security verdicts,
  quarantined attachments, reports, notifications, settings, API-token management, and an
  append-only audit view.
- Tenant-scoped SES/S3/SNS/SQS ingestion with durable idempotency, bounded MIME parsing, HTML
  sanitization, domain-local message threading, multi-domain delivery, and delivery-event processing.
- Structured OpenAI triage, reply drafting, and report generation with a deterministic report
  fallback when OpenAI is unavailable.
- Immutable reply revisions, exact-revision approval, explicit resend, and conservative SES
  timeout handling that never retries an ambiguous submission automatically.
- Django Ninja `/api/v1`, CSRF-protected session access, and once-displayed hashed bearer tokens
  with `read`, `write`, and `approve_send` scopes.
- SQLite WAL deployment, encrypted integrity-checked backups, configurable retention, private S3
  storage, WhiteNoise static delivery, AWS CDK infrastructure, StageOps configuration, and a
  locked `origin/main` deployment flow.

## Architecture

The Django application runs on the StageOps `hetzner-stage` server. AWS `us-east-1` is used only
for the email data plane.

```text
Customer domain or provider catch-all
                 |
                 v
        Amazon SES Email Receiving
                 |
                 | S3 receipt action (TLS required; virus/spam scan enabled)
                 v
      private encrypted S3: ingress/
                 |
                 | S3-action notification
                 v
        inbound SNS topic ----+
                              |
        delivery SNS topic ---+--> standard SQS queue --> DLQ
                                              |
                                              | cron, flock, <=55 s long poll
                                              v
                              manage.py ingest_aws_events
                                              |
                         tenant S3 copies + short DB commit
                                              |
                                              v
                          SQLite/WAL --> Django UI + /api/v1

Exact approved revision --> durable job --> SES outbound --> delivery SNS
Scheduled jobs ----------> triage, reports, reminders, DNS and retention
```

The queue uses 20-second long polling and a 5-minute visibility timeout. A StageOps cron starts
`ingest_aws_events --max-runtime 55` every minute under `flock`; there is no resident Celery or
Node process. In healthy operation, a message should appear in the application within 90
seconds.

SES stores the original MIME object under `ingress/` and publishes its S3-action notification to
SNS. The SQS subscription intentionally keeps the SNS envelope so the worker can validate the
topic ARN and use the SNS `MessageId` as its first idempotency boundary. It routes tenants from
SES `receipt.recipients`, never from the untrusted MIME `To` header.

At-least-once delivery is handled at two levels:

1. SNS `MessageId` is globally unique in the ingress ledger.
2. `(domain, SES messageId)` is unique for normalized messages.

If one SES message targets multiple domains, each domain gets its own `Message`, raw MIME copy,
and attachment copies under a domain S3 prefix. The SQS message is deleted only
after every routed copy and database operation succeeds. Transient failures remain visible for
retry; malformed or permanently invalid input leaves an inspectable quarantine event.

## Tenant and data model

All tenant records use UUID primary keys and carry a `domain_id`. Tenant resolution begins with
the authenticated domain owner or domain-bound bearer token
before an object identifier is accepted. Cross-tenant web and API lookups return `404`, and S3
keys and AWS implementation details are never exposed in API errors.

The model covers:

- domains, domain-specific report schedules and retention policies;
- DNS instructions/results, inbound routes, and delivery tests;
- conversations, messages, envelope/header recipients, RFC references, and attachments;
- classifications, agent runs, and idempotent leased jobs;
- reply drafts, immutable revisions, exact approvals, and outbound messages;
- reports/items, notifications, ingress/delivery events, API tokens, and append-only audits.

Conversation states are `OPEN`, `WAITING_EXTERNAL`, `RESOLVED`, and `QUARANTINED`. Threading is
domain-local: RFC `References` is considered first, then `In-Reply-To` with participant overlap.
Subject similarity can create a merge suggestion but never silently merges conversations. A new
inbound message reopens a resolved/waiting conversation and invalidates any existing draft
approval.

The default abuse controls are 5 active domains per user, 5 domain
provisioning attempts per hour, 5 magic-link requests per hour, 3 legacy verification-link resend
attempts per hour, and a 72-hour unverified domain claim. All limits are configurable through
environment variables.

## Domain onboarding without breaking mail

Domain states progress through
`PROVISIONING -> PENDING_DNS -> PENDING_TEST -> READY`. Provisioning failures use `ERROR`,
configuration drift uses `DEGRADED`, and removal uses `DISABLED`. This state machine is receiving-
only. Sending has its own explicit lifecycle:
`DISABLED -> PROVISIONING -> PENDING_DNS -> READY`, with capability-local `ERROR` and `DEGRADED`
states. A sending failure never disables an otherwise ready inbound route.

Operational Inbox checks current MX records before presenting setup guidance. If MX records
already exist, it recommends the forwarding path and never tells the owner to replace those
records blindly.

### Direct MX

Use this when the selected customer domain or subdomain can be dedicated to SES receiving. The
owner publishes the application ownership TXT, SES `_amazonses` verification TXT, and MX records
shown by the application. DKIM is not provisioned during receiving setup. After ownership is
verified, the domain is added to the explicit SES receipt-rule recipient allowlist.

### Provider catch-all forwarding

Use this when an existing provider must keep handling the domain's MX. The owner keeps all
current MX records and configures the provider catch-all to forward unmatched mail to a unique,
high-entropy address at `inbound.operationalinbox.com`. The forwarding alias is tenant-specific;
it is not a shared catch-all address. Receiving-only setup publishes just the application ownership
TXT and does not create or inspect a customer-domain SES identity.

### Optional outbound sending

Sending is enabled only after the receiving test succeeds and the owner explicitly requests it.
That action provisions SES identity verification and DKIM records without changing the receiving
state. Direct-MX domains reuse their receiving identity; provider-forward domains touch a
customer-domain SES identity for the first time at this step.

For both modes, the delivery test targets `test-<token>@<customer-domain>`. Direct mode therefore
tests the customer's SES MX path, while forwarding mode tests the existing provider and its
catch-all rule before the message reaches the high-entropy service route.

The CDK stack creates and activates an empty named SES receipt-rule set. Django owns the single
named rule and reconciles its recipient list to exactly:

- `inbound.operationalinbox.com`; and
- active, ownership-verified domains that selected direct MX.

The rule never uses an empty recipient condition because that would accept every verified SES
identity in a shared AWS account. Pre-existing SES identities are not adopted automatically, and
the rule enforces SES's 500-recipient condition limit.

DNS remains customer-managed. `check_domain_drift` only reads DNS and relevant SES state; it never
writes customer records. Provider-forward domains with sending disabled are not queried in SES.

## Message and agent safety

- Raw MIME parsing uses Python's standard email library with total-message, decoded-body,
  part-count, and nesting limits.
- HTML is sanitized with an allowlist. Scripts, forms, active content, and external image loads
  are removed; attachments are never opened automatically.
- A virus verdict other than `PASS` quarantines the message and its attachments. Spam or failed
  authentication verdicts remain visible as suspicious mail instead of being deleted.
- Only a clean, unexpired attachment can receive an authorized S3 URL. The URL expires after five
  minutes. Quarantined/unscanned objects stay locked and retention-expired objects return `410`.
- Logs and API errors carry request IDs but do not include message bodies, secrets, tenant S3
  keys, or raw AWS details.
- Raw email and every metadata value are marked as untrusted data in model prompts. The model has
  no tools, cannot browse or open links, and receives only attachment filename/type/size/scan
  metadata—not attachment bytes.

OpenAI integration uses the Responses API with Pydantic Structured Outputs and `store=false`.
Defaults are configurable:

- triage: `gpt-5.6-luna`, low reasoning;
- drafts: `gpt-5.6-terra`, medium reasoning;
- reports: `gpt-5.6-terra`, medium reasoning.

When `OPENAI_API_KEY` is empty or an API/schema/refusal error occurs, ingestion, search, security
verdicts, and the rest of the inbox continue to work. Messages remain visibly unclassified, and
scheduled reports use deterministic content. Draft generation reports itself unavailable rather
than taking external action.

The default review frequency is hourly. Daily mode generates a report at 09:00 in the
domain's time zone, including DST-safe schedule keys. Aging reminders default to 24 hours.
Important and suspicious classifications create deduplicated in-app and SES email notifications.

## Human-approved outbound mail

Editing a reply creates a new immutable revision and invalidates any older approval. Only the
domain owner—or a token with `approve_send` acting for that owner—can approve the exact
current revision and content hash. The queued sender revalidates the approval, draft freshness,
domain owner and domain ownership/outbound readiness immediately before submission.

Outbound status is tracked as:

```text
QUEUED -> SUBMITTING -> ACCEPTED -> DELIVERED
                     \-> FAILED | UNKNOWN | BOUNCED | COMPLAINED
```

A timeout or connection failure after submission begins is `UNKNOWN`: SES may have accepted the
message, so automatic retry is forbidden. The owner must inspect the status and explicitly
resend. Delivery events correlate by SES message ID and the outbound tag, tolerate out-of-order
events, and do not regress a terminal result incorrectly.

Automatic acknowledgements and every form of autonomous external reply are disabled in this MVP.

## Local development

### Requirements

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- Node.js/npm only for local Tailwind and CDK work
- SQLite 3

### Setup

```console
git clone git@github.com:onurmatik/operational-inbox.git
cd operational-inbox
cp .env.example .env
uv sync --all-groups --frozen
npm ci
npm run css:build
uv run python manage.py migrate
uv run python manage.py runserver
```

For local HTTP development, edit `.env` to use a unique secret and these overrides:

```dotenv
DJANGO_SECRET_KEY=replace-with-a-local-random-secret
DJANGO_DEBUG=true
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=http://localhost:8000
DJANGO_SECURE_COOKIES=false
DJANGO_SECURE_SSL_REDIRECT=false
PUBLIC_BASE_URL=http://localhost:8000
DJANGO_EMAIL_BACKEND=console
OPENAI_API_KEY=
```

With the console email backend, passwordless sign-in links and legacy verification-resend links
are printed in the server terminal. Leave `OPENAI_API_KEY` blank to exercise graceful
AI-unavailable behavior; add a key only when testing live Responses API calls. The ignored `.env`
and `.env-prod` files must never be committed.

The Tailwind input is [`assets/css/app.css`](assets/css/app.css); the minified output
[`inbox/static/css/app.css`](inbox/static/css/app.css) is tracked. Production installs no Node
runtime and serves collected static files through WhiteNoise.

Useful commands:

```console
# Process due triage, report, notification, domain and outbound jobs
uv run python manage.py run_scheduler

# Poll SQS for at most 55 seconds
uv run python manage.py ingest_aws_events --max-runtime 55

# Read DNS/SES readiness and detect drift
uv run python manage.py check_domain_drift

# Reconcile the explicit-recipient SES rule
uv run python manage.py reconcile_ses_receipt_rule

# Apply S3/database retention
uv run python manage.py purge_retention

# Create an online encrypted SQLite backup
uv run python manage.py backup_sqlite
```

`backup_sqlite` requires `BACKUP_ENCRYPTION_KEY`, a URL-safe base64 encoding of exactly 32 random
bytes:

```console
python3 -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

## Configuration

[`.env.example`](.env.example) is the authoritative runtime contract. Important groups are:

- Django host, CSRF, HTTPS/cookie, database, email backend, and public URL settings;
- domain/signup/verification-resend limits and `INBOUND_SERVICE_DOMAIN`;
- dedicated AWS `us-east-1` credentials plus bucket, queue, topic, configuration-set, and
  receipt-rule names;
- OpenAI API key and model names;
- backup encryption key and destination.

Production AWS credentials belong only in ignored `.env-prod` and the mode-`0600` server `.env`.
Use a dedicated access key created out of band for the stack's least-privilege IAM user. CDK does
not create or output an access key, and no secret is stored in CloudFormation or Git.

The runtime policy pins S3 and SQS access to this stack's ARNs and exposes only the SES identity,
sending, and named receipt-rule operations the application needs. Customer replies always use the
Operational Inbox configuration set; system verification/notification mail intentionally does not,
so unrelated delivery events do not enter the application queue. Receipt-rule APIs do not support
resource-level IAM scoping, so the code pins both the rule-set and rule names. The application also
refuses to adopt pre-existing identities and builds its recipient allowlist only from verified
managed domains. Because customer sending identities are created dynamically, the SES send
statement necessarily uses this account's `identity/*` resource pattern. For the strongest
credential-compromise boundary, deploy the email data plane in a dedicated AWS account; in a shared
account, use an out-of-band policy reconciler that replaces the wildcard with the current managed
identity ARNs.

SQLite is the only configured application database. It uses WAL, a 20-second busy timeout,
`IMMEDIATE` short transactions, one Gunicorn worker, and two threads.

## API

The Django Ninja API is mounted at `/api/v1`; interactive OpenAPI documentation is available at
`/api/v1/docs` when the application is running. It exposes domains/checks/tests and domain-scoped
conversations/messages, classification overrides, drafts/revisions/exact
approval, outbound status/resend, reports, notifications, audits, API tokens, and attachment
download authorization.

Browser requests use the authenticated Django session and CSRF protection. External clients use
a bearer token:

```http
Authorization: Bearer oi_<one-time-secret>
```

Only the hash and a short lookup prefix are stored. The raw token is displayed once. Tokens are
domain-bound, independently revocable, invalid when the owner or domain is inactive,
and limited to `read`, `write`, and/or `approve_send`.

Conversation lists use opaque signed cursors. API failures have one stable envelope:

```json
{
  "code": "validation_error",
  "message": "Correct the highlighted fields and try again.",
  "fields": {"hostname": ["Enter a valid domain name."]},
  "request_id": "c56f..."
}
```

## Retention and recovery

Each domain receives these default retention periods:

| Data | Default |
| --- | ---: |
| Raw MIME and attachment objects | 90 days |
| Normalized message, classification, draft, and report content | 365 days |
| Audit, delivery, ingress, and terminal-job metadata | 730 days |
| Encrypted SQLite backups | 30 days |

The S3 bucket enforces the raw/attachment and backup lifecycles. The retention command removes S3
objects and redacts normalized database content, recipients, classifications, draft/outbound
bodies, report/notification content, and expired metadata. Ingress bucket/key locations are
redacted when raw-message retention expires; old ingress events and terminal jobs expire with the
domain's metadata policy. Domain-less records use the longest configured policy or
the model default so one tenant cannot shorten another tenant's retention. Signup-attempt records
expire after the configured rate-limit window, and used or expired verification tokens are
deleted. Active/retry jobs are never removed by age. Audit events are append-only during normal
operation; the retention path is the explicit expiration mechanism.

Backups use SQLite's online backup API, verify both source and copy with `PRAGMA integrity_check`,
then encrypt with AES-256-GCM before writing a mode-`0600` file. When the production bucket is
configured, the command uploads the encrypted artifact under `backups/sqlite/` and verifies its
size and SHA-256 metadata; S3 and the host both retain backups for 30 days. Deployment obtains
every cron lock and creates this backup before migrations.

## AWS email data plane (CDK)

[`infra/stack.py`](infra/stack.py) provisions in `us-east-1`:

- a retained, private, TLS-only, S3-managed-encryption bucket with lifecycle rules;
- separate inbound and outbound-delivery SNS topics with TLS and scoped SES publish policies;
- a standard SQS queue (20-second long poll, 5-minute visibility, 14-day retention) and a 14-day
  DLQ after five receives;
- a sending configuration set and SNS delivery-event destination;
- an empty active SES receipt-rule set for Django to reconcile safely;
- `operationalinbox.com` and `inbound.operationalinbox.com` SES identities with DKIM outputs;
- a least-privilege `operational-inbox-hetzner` IAM user without an access key;
- CloudWatch alarms for DLQ messages, queue backlog, and oldest-message age.

Deploy it only after selecting the AWS account:

```console
export CDK_DEFAULT_ACCOUNT=123456789012
export CDK_DEFAULT_REGION=us-east-1
uv sync --group infra --frozen
npm ci
npx cdk bootstrap aws://$CDK_DEFAULT_ACCOUNT/us-east-1
npx cdk deploy OperationalInboxEmail
```

Review the outputs, publish the emitted DKIM/MX records, create the IAM user's access key out of
band, and copy the non-secret resource identifiers plus the secret credentials into ignored
`.env-prod`. After the app has verified a direct domain, run the receipt-rule reconciler (the
scheduler also enqueues reconciliation when ownership or disablement changes).

## StageOps deployment

The StageOps registry contains an `operationalinbox` app on `hetzner-stage`:

- cold tier with 10-minute idle stop;
- one Gunicorn worker and two threads;
- 800 MB memory limit and 60% CPU quota;
- Node and Celery disabled;
- locked ingestion (every minute), scheduler (every five minutes), DNS drift (hourly), retention
  (daily), and backup (daily) cron commands.

The tracked [`.deploy/fabfile.py`](.deploy/fabfile.py) contract deploys only `origin/main` to
`/srv/apps/operationalinbox`. It mints a short-lived GitHub App installation token locally,
clones/fetches the configured private repository over HTTPS without persisting credentials on the
host, checks out `main`, creates or reuses the Python 3.12 virtual environment, installs the frozen
production [`requirements.txt`](requirements.txt), and safely merges only an explicit environment
allowlist.
It then stops the cold service, acquires every cron/deploy lock, backs up and verifies SQLite,
runs migrations, collects static files, executes `check --deploy`, and restarts the socket.

Prepare the ignored local GitHub App credentials and production runtime configuration:

```console
cp .deploy/.credentials.env.example .deploy/.credentials.env
chmod 600 .deploy/.credentials.env
cp .env.example .env-prod
# Fill the GitHub App values and only production runtime values; keep DJANGO_EMAIL_BACKEND=ses.

python3 -m pip install -r .deploy/requirements.txt
cd .deploy
python3 -m fabric deploy
```

The default target is the private `onurmatik/operational-inbox` repository, host `46.225.14.95`,
SSH key `~/.ssh/hetzner-stage`, application user `ubuntu`, and domain
`operationalinbox.com`. See [`.deploy/README.md`](.deploy/README.md) for the concise operator
contract.

## DNS and live acceptance

Do not change the existing root MX records for `operationalinbox.com`.

1. Point only the root A record for `operationalinbox.com` to `46.225.14.95`.
2. Publish the CDK-provided DKIM records for the system identity.
3. Publish the SES MX value `10 inbound-smtp.us-east-1.amazonaws.com` for
   `inbound.operationalinbox.com`, plus its CDK-provided DKIM records.
4. Let customer-domain owners choose direct MX or provider catch-all forwarding from the
   application; never overwrite customer DNS automatically.
5. Verify HTTPS and both `/health/live` and `/health/ready`.
6. Confirm the minute cron consumes SQS, leaves the DLQ empty, and meets the 90-second inbound
   acceptance target.
7. Exercise the complete path: enter domain -> request magic link -> domain setup
   -> choose the safe domain setup mode -> DNS checklist -> catch-all test -> inbox -> triage ->
   report -> draft -> optionally enable sending and publish DKIM -> exact approval -> SES reply ->
   delivery/audit event.

Before the first live deploy, create the empty private GitHub repository
`onurmatik/operational-inbox`, commit and push this project to `main`, deploy the CDK stack, and
provide the server's ignored production environment. These are external prerequisites; the
repository never fabricates credentials or changes DNS.

## Quality checks

The test suite covers tenant isolation, domain-first onboarding, magic-link authentication,
signup and provisioning limits, IDNA and claim expiry,
MX safety, explicit receipt-rule reconciliation and the 500-recipient limit, duplicate and
out-of-order AWS events, multi-domain routing, malformed MIME, crash idempotency, sanitization,
quarantine, prompt injection, attachment authorization, retention, threading, DST scheduling,
OpenAI failure, stale drafts, exact approval, header injection, ambiguous SES submission,
bounce/complaint handling, and explicit resend.

Run the local quality gate:

```console
uv run ruff format --check operational_inbox inbox infra tests .deploy
uv run ruff check operational_inbox inbox infra tests .deploy
uv run mypy operational_inbox inbox infra
uv run pytest
uv run python manage.py makemigrations --check --dry-run
npm run css:build
npx cdk synth --quiet
DJANGO_DEBUG=false DJANGO_SECRET_KEY=local-deploy-check-secret-key-with-at-least-fifty-characters-000000 \
  DJANGO_ALLOWED_HOSTS=operationalinbox.com \
  DJANGO_SECURE_COOKIES=true DJANGO_SECURE_SSL_REDIRECT=true \
  uv run python manage.py check --deploy
```

These checks are intentionally local; this repository does not install a GitHub Actions workflow.

## MVP boundaries

Not included: teams or roles, pricing/billing, custom cron expressions, legal hold, marketing or
bulk email, personal IMAP/POP replacement, automatic acknowledgement, autonomous external
replies, or automatic model analysis of attachment contents. Customer DNS is verified and shown
as exact instructions but is never modified by the application.
