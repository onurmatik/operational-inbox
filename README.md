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
- Tenant-scoped storage, configurable retention, and encrypted backups.

Customer DNS is inspected but never modified. Organization actions are reversible, and ambiguous
outbound submissions are never retried automatically.

## Documentation

- [Agent installation](INSTALL.md)
- [Technical reference](TECHNICAL.md)
- [Agentic integration and MCP contract](docs/agentic-integration.md)
- [OpenAPI schema](openapi.json)
- [Plugin package](plugins/operational-inbox)
