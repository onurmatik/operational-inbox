# Task: simplify Operational Inbox around mail and agents

Use the existing Superdesign project and the local application's visual system.
Only this brief, selected interface source files, and synthetic audit imagery are relevant.

## Product

1. Give small projects support@, contact@, and other addresses on their domains.
2. Send individual customer emails from a verified project domain.

The user's own external agent performs setup, triage, writing, and delivery monitoring through MCP.
The UI is a readable conversation history with oversight and a manual sending fallback.
Do not add an embedded AI chat, campaign builder, CRM, contacts model, tasks, or automated sequences.

## Proposed information architecture

- Two main destinations: Conversations and Domains.
- Conversations combines received and sent email across all owned domains.
- Domain is an optional visible filter, rather than a hidden global active-domain session.
- Received, Sent, Starred, Archive, and Trash are views of the same conversations.
- Search is the default control; advanced tag/security/delivery filters sit behind Filters.
- Keep New email visible. Use one full From-address control, To, Subject, Message, Send.
- An email's delivery state belongs beside that email; failures produce a compact actionable notice.
- Account-level pause remains available in sending settings. Never equate Accepted with Delivered.
- A reply revision appears inline only when present; omit the empty permanent draft side panel.
- Healthy security/RFC metadata is behind Details; quarantine remains explicit and locks content.
- Agent setup is one secondary panel with a copyable connection prompt and manage-access link.
- Do not claim an agent is connected without evidence. Default to Connect your agent.
- Plan, retention, API access, and audit stay in one secondary Settings entry.

## Domain setup proposal

Ask the user's goal: Receive email, Send email, or Both.
Show receiving and sending as independent capability states, each with one next action.
Sending-only requires ownership and sending DNS verification, without an inbound test or MX change.
This capability requires backend work; it is a proposed flow, not a current feature.
Existing inbound setup still recommends forwarding when a mail provider exists.
Healthy domains show only hostname, Receiving/Sending states, and Settings.
Show exact DNS records only when action is needed or when expanded.
Do not expose SES identity internals or routing transition mechanics in the default UI.

## Representative content

All demo mail is fictional. Use acme.example and studio.example.
Support: Can I change my subscription? / Alex / support@acme.example.
Customer outreach: Following up on your trial / Taylor / hello@acme.example / Delivered.
Contact: Partnership enquiry / Sam / contact@studio.example.
Archive: Account access restored / Lee / support@acme.example.
One quarantined conversation has locked content. Never invent real personal information.

## Recommended layout

A compact sidebar plus conversation list and readable detail pane; global filter above the list.
On mobile, switch between the list and a full-width thread rather than squeezing both into columns.
Expose receiving history and customer outreach as equally natural actions.
