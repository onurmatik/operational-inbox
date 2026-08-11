# Operational Inbox

**Agent-first operational email for every project address.**

Operational Inbox brings `support@`, `contact@`, `privacy@`, and other project addresses across
multiple domains into one secure inbox. It is built for people to delegate email operations to
their own agents without turning the inbox into a CRM or task-management system.

Agents are the primary interface for setup, triage, replies, and delivery monitoring. The web
application remains the owner’s control surface for oversight, consent, billing, and account
settings.

## Connect your agent

Paste this prompt into a compatible agent:

```text
Help me manage my operational inboxes using the Operational Inbox skill.
Read https://operationalinbox.com/INSTALL.md and follow every step.
```

The guide installs the `$operational-inbox` skill, configures the agent’s native MCP connection,
and completes OAuth authorization without asking you to paste secrets into chat. See the
[installation guide](INSTALL.md) for the full setup flow.

Standalone-skill and MCP compatibility versions are published at
[`/agent-manifest.json`](https://operationalinbox.com/agent-manifest.json). Version checks run only
during setup, explicit updates, or connection diagnostics; ordinary inbox work never self-updates
the copied skill.

## What agents can do

- Inspect and set up domains with exact, provider-neutral DNS instructions.
- Review and search mail across domains and routed addresses.
- Organize conversations with tags, Star, Archive, Trash, and Restore.
- Draft immutable reply revisions and send only the exact authorized revision.
- Monitor outbound delivery, including failures, bounces, and complaints.

The standalone skill routes these jobs through focused setup, triage, reply, and delivery
workflows. Agents connect to the native MCP server with explicit `read`, `write`,
`manage_domains`, and `send` scopes.

## Core capabilities

- Multi-domain inbox with unlimited routed addresses per connected domain.
- Direct-MX and provider catch-all forwarding setup paths.
- Searchable conversation history, attachment quarantine, and audit trail.
- Safe outbound sending with immutable revisions and conservative retry behavior.
- Web application, Django Ninja API, standalone agent skill, and Streamable HTTP MCP server.
- Tenant-scoped storage, plan-appropriate retention, and encrypted backups.

Customer DNS is inspected but never modified. Organization actions are reversible, and ambiguous
outbound submissions are never retried automatically.

## Free Core and Pro Scale

Both plans include the complete agent workflow: inbound receiving, the feed for every authorized
active domain, search, tags and folders, immutable drafts, Outbox pause/resume and delivery events,
and API + MCP access. MCP tool calls are not commercially metered, and incoming mail has no hard
plan-volume quota. Technical and abuse-prevention safety limits still apply.

- **Free Core:** one active domain, 30 user-requested one-to-one replies per UTC calendar month,
  and fixed retention.
- **Pro Scale:** up to 20 active domains, 5,000 user-requested one-to-one replies per UTC calendar
  month, custom retention, and server-side AI classification.

Reply allowances reset at 00:00 UTC on the first day of each month. Drafting and Outbox safety
remain available when the reply allowance is exhausted.

If an account returns to Free Core while more than one domain is active, it receives a 30-day
capacity grace period. The owner chooses the domain that will keep the Free slot; the others remain
readable during the grace period and are disabled when it ends.

## Documentation

- [Agent installation](INSTALL.md)
- [Technical reference](TECHNICAL.md)
- [Agentic integration and MCP contract](docs/agentic-integration.md)
- [OpenAPI schema](openapi.json)
- [Plugin package](plugins/operational-inbox)
