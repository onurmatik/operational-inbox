# Operational Inbox

**Agent-first operational email for every project address.**

Operational Inbox brings `support@`, `contact@`, `privacy@`, and other project addresses across
multiple domains into one conversation history. It sends replies to received email from a verified domain. It is built for people to delegate email operations to
their own agents without turning the inbox into a CRM or task-management system.

Agents are the primary interface for setup, triage, replies, and delivery monitoring. The web
application remains the owner’s control surface for oversight, consent, billing, and account
settings. Conversations combines Inbox, Sent, and Archive across domains. Domain setup offers receiving only or receiving and replying.

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
- Mark spam false positives as Not spam and Undo, with original verdicts and audit history.
- Draft immutable reply revisions and send only the exact authorized revision.
- Monitor outbound delivery, including failures, bounces, and complaints.

The standalone skill routes these jobs through focused setup, triage, reply, and delivery
workflows. Agents connect to the native MCP server with explicit `read`, `write`,
`manage_domains`, and `send` scopes.

## Core capabilities

- Multi-domain inbox with unlimited routed addresses per connected domain.
- Optional daily email digest across all account domains, with configurable lookback and schedule.
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

- **Free Core:** one active domain, 30 user-requested replies per UTC calendar month,
  and fixed retention.
- **Pro Scale:** up to 20 active domains, 5,000 user-requested replies per UTC calendar
  month, custom retention.

Reply attempts use this allowance, which resets at 00:00 UTC on the first day of each
month. Reply drafting and Outbox safety remain available when it is exhausted.

Operational Inbox is reply-only. Each outbound reply requires an existing inbound message.

Open a conversation to read its thread, filter the inbox, and write a **Reply**. **Save draft**
persists the exact text so you or your own connected agent can continue later. **Send reply**
queues the editor's exact content. **Write with AI** opens optional instructions and contextual
reply choices; **Review** suggests improvements to an existing draft. Suggestions appear in a
preview and change the editor only when you choose **Apply**. They never save or send themselves.

If an account returns to Free Core while more than one domain is active, it receives a 30-day
capacity grace period. The owner chooses the domain that will keep the Free slot; the others remain
readable during the grace period and are disabled when it ends.

## Documentation

- [Agent installation](INSTALL.md)
- [Technical reference](TECHNICAL.md)
- [Agentic integration and MCP contract](docs/agentic-integration.md)
- [OpenAPI schema](openapi.json)
- [Plugin package](plugins/operational-inbox)

## Digest

Enable **Settings → Account → Digest** to receive one email at the account address
covering incoming messages across all accessible domains. The default is a 24-hour lookback
at 09:00 UTC; lookback can be 1–168 hours, with a daily send time and IANA timezone.
The digest includes sender, subject, a short preview, and a conversation link. Quarantined
content is not included. Empty periods do not produce email. Digests are off by default.

The existing `run_scheduler` command queues and delivers digests through durable jobs and the
configured notification email backend. Run it regularly (for example, every minute) and set
`PUBLIC_BASE_URL` to the application URL for email links. Each scheduled window is fixed for
retries; after scheduler downtime, only the latest window is queued. Lookbacks shorter than
24 hours leave gaps between daily windows; longer lookbacks can repeat messages. Windows use
elapsed hours, while send times follow the selected timezone, including daylight-saving changes.


## Import and export emails

Open a domain's **Advanced settings → Import / export emails** to upload an MBOX file
of up to 250 MB or export its received and sent mail. Transfers continue in the background
after upload. Existing messages are not overwritten; duplicates and conflicts appear in the result.
Original email dates are preserved, while imported content's retention starts at import.
Imported attachments remain **Not scanned** and cannot be downloaded or exported.

Exports include Archive and Trash and omit quarantined or expired content. When original MIME
is unavailable, retained content is used and omissions are reported. Large exports are split
into MBOX files of up to 250 MB; downloads remain available for 24 hours. Owners can also
export disabled domains from the Domains list. This feature is web-only.

## Reply assistance

There is no built-in conversation autopilot, policy engine or automatic follow-up. Your own agent
can manage conversations through MCP and the supplied skills, using its authorized external
sources. The website AI sees only the current thread, draft and your instructions.

The service supplies `OPENAI_API_KEY`; configure `REPLY_ASSIST_*` settings in `.env.example`.
Assistance is requested only by an explicit UI action. Manual drafting and sending continue when
AI is unavailable or its daily budget is exhausted. Server/plugin 0.5.0 uses MCP contract 3.0.0
and standalone skill 1.4.0. Update older clients: new-email tools and send-only onboarding have been removed.
