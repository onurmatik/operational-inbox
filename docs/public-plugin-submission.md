# Public plugin submission

This is the source-of-truth checklist for submitting Operational Inbox to the public OpenAI
Plugin Directory. It intentionally excludes credentials, reviewer passwords, and domain
verification tokens.

Status as of August 10, 2026: the review scope includes safe domain inspection and onboarding,
cross-domain inbox triage, reversible organization, agent-authored exact-revision sending, Outbox
monitoring and controls, and explicit resend. Domain routing transitions, disablement, API-token management, attachments,
reports, and permanent deletion are not exposed through MCP.

## Implemented in the repository

- Production MCP endpoint: `https://operationalinbox.com/mcp`.
- OAuth 2.1 discovery, public-client registration, PKCE authorization, rotating refresh tokens,
  exact MCP resource binding, and protected tool calls.
- Public install instructions, MCP documentation, privacy policy, terms, and support pages.
- Public fallback-integration compatibility metadata at
  `https://operationalinbox.com/agent-manifest.json`.
- Public Agent Plugins 1.0 manifests and four bundled skills under
  `plugins/operational-inbox`.
- Production manifest URLs:
  `https://operationalinbox.com/plugins/operational-inbox/plugin.json` and
  `https://operationalinbox.com/plugins/operational-inbox/mcp.json`.
- OpenAI/Codex manifest with a 512 x 512 logo, legal URLs, remote MCP server, and starter prompts.
- MCP Registry metadata in `server.json` for optional registry publication.
- Domain challenge endpoint backed by `OPENAI_APPS_CHALLENGE_TOKEN`.
- Tool discovery without credentials and MCP `mcp/www_authenticate` challenges on protected calls.
- Email-content prompt-injection boundary, reversible organization, delegated exact-revision
  sending, bounded account controls, and delivery-event visibility.
- Domain setup boundary that returns generation-fenced instructions while leaving DNS writes to a
  separately authorized provider tool or the owner.

## Before portal submission

- [ ] Deploy this release and run database migrations.
- [ ] Verify the production MCP endpoint, OAuth metadata, DCR/PKCE flow, protected resource
  metadata, and all public/legal URLs.
- [ ] Complete the tool annotation review and generate `chatgpt-app-submission.json` with exactly
  six positive and three negative reviewer tests.
- [ ] Create a dedicated, already-verified reviewer account with sample inbox data and sufficient
  plan access. It must not require MFA, SMS, email confirmation, a private network, or payment.
- [ ] Record a public HTTPS demo video covering read, reversible write, agent-authored send,
  Outbox monitoring/pause, and explicit resend behavior on supported OpenAI surfaces.
- [ ] Confirm the OpenAI organization owner has **Apps Management: Write**, the selected project
  uses **Global** data residency, and developer/business verification is complete.
- [ ] Choose the initial listing countries and regions.

## Production verification

Run these checks after deployment:

```console
curl -i https://operationalinbox.com/.well-known/oauth-protected-resource/mcp
curl -i https://operationalinbox.com/.well-known/oauth-authorization-server
curl -i https://operationalinbox.com/agent-manifest.json
curl -i https://operationalinbox.com/mcp-docs/
curl -i https://operationalinbox.com/privacy/
curl -i https://operationalinbox.com/terms/
curl -i https://operationalinbox.com/support/
curl -iL https://operationalinbox.com/INSTALL.md
```

The MCP resource must be byte-exact as `https://operationalinbox.com/mcp`. OAuth access tokens
must be short-lived, bound to that resource, issued only through public clients and authorization
code + PKCE, and returned only after explicit user consent.

## Portal submission

1. Open [OpenAI Plugins](https://platform.openai.com/plugins) and choose
   **Create plugin -> With MCP -> Standard**.
2. Enter `https://operationalinbox.com/mcp` and run **Scan Tools**.
3. Review tool names, descriptions, input/output schemas, OAuth security schemes, server
   instructions, and every `readOnlyHint`, `openWorldHint`, and `destructiveHint`.
4. Copy the portal-generated domain token into the production
   `OPENAI_APPS_CHALLENGE_TOKEN` environment variable and deploy. Verify that
   `https://operationalinbox.com/.well-known/openai-apps-challenge` returns HTTP 200 with only the
   exact token, then select **Verify Domain**.
5. Upload `setup-domain`, `triage-inboxes`, `reply-to-conversations`, and
   `monitor-outbound-delivery` from the packaged plugin
   and use the listing metadata from `plugins/operational-inbox/.codex-plugin/plugin.json`.
6. Add these starter prompts:
   - Connect my domain without disrupting its current mail route.
   - Triage new mail across all my domains.
   - Draft and send a reply for the conversation I select.
   - Show outbound replies that need attention.
7. Import the six positive and three negative tests from `chatgpt-app-submission.json` and run all
   nine with the reviewer account.
8. Add reviewer login details, demo video, countries and regions, and release notes.
9. Complete only attestations that are true for the scanned production release, submit for
   review, and record the date and portal status below.

## Initial release notes

> Initial public submission of Operational Inbox. Includes OAuth-authenticated domain inspection
> and setup planning, cross-domain inbox triage, reversible organization, agent-authored
> exact-revision sending, Outbox controls, delivery monitoring, and explicit resend, plus the Setup
> Domain, Triage Inboxes, Reply to Conversations, and Monitor Outbound Delivery skills. Operational
> Inbox never writes customer DNS; email content is treated as untrusted data, trash does not
> permanently delete mail, and external sends are bounded by delegated scope, exact-content checks,
> account pause, and send limits.

## Submission record

- Submitted: not yet
- Portal status: not submitted
- Approved: not yet
- Published: not yet; approval and publication are separate portal actions

## Changes after approval

Treat new MCP tools, material schema or behavior changes, new data collection, new side effects,
or new outbound capabilities as a new reviewed version. Update the tool scan, listing, privacy
disclosures, skills, reviewer tests, demo recording, and release notes before submitting that
version.
