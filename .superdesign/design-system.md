# Operational Inbox Design System

## Product context

Operational Inbox is a security-conscious control layer for official inbound email. The interface must make it easy for a single organization owner to connect domains, inspect every message, understand security verdicts, review scheduled reports, and approve an exact reply revision. It is an operational application, not a personal mail client and not a marketing automation product.

Primary journeys:

- Account creation, email verification, organization and first-project onboarding.
- Domain provisioning through direct SES MX or provider catch-all forwarding.
- DNS verification, test delivery, and separate inbound/outbound readiness.
- Dashboard review, complete inbox search, conversation inspection, and quarantine handling.
- Draft review, immutable revision editing, exact approval, and outbound delivery tracking.
- Reports, notifications, retention, audit history, and scoped API-token settings.

## Visual direction

Use a technical-minimalist control-panel language derived from structural blueprints and high-end operational tools. The experience is calm, exact, and trustworthy. It uses a warm paper background, a prestige forest-green brand color, hairline dividers, flat surfaces, and strong negative space. Dense pages should remain highly scannable.

Do not use gradients, glass effects, oversized radii, decorative illustrations, floating cards, or deep shadows. Never hide critical status in color alone.

## Color tokens

- `paper`: `#F7F7F5` — application background.
- `surface`: `#FFFFFF` — primary work surfaces.
- `ink`: `#18211C` — primary text.
- `muted`: `#68726C` — secondary text.
- `forest`: `#1A3C2B` — brand, primary actions, active navigation.
- `forest-strong`: `#11291D` — hover and strong emphasis.
- `line`: `#D9DEDA` — default hairline.
- `line-strong`: `#AEB8B1` — focused separators.
- `mint`: `#DDF6E5` / `#23643B` — ready, delivered, healthy.
- `gold`: `#FFF2C2` / `#715700` — pending, waiting, warning.
- `coral`: `#FFE0D5` / `#8B3420` — suspicious, failed, destructive.
- `blue`: `#E5EEF9` / `#244E7B` — informational, outbound activity.
- `quarantine`: `#EEE7F4` / `#5C3B70` — quarantined content.

All foreground/background status pairs must meet WCAG AA contrast. Add a symbol and text label to every status.

## Typography

- Headings and key navigation: `Space Grotesk`, with `Inter`, `ui-sans-serif`, and system fallbacks. Use tight tracking but never condensed text.
- Body and form controls: `Inter`, `ui-sans-serif`, and system fallbacks.
- IDs, timestamps, DNS values, API scopes, technical labels, and status metadata: `JetBrains Mono`, `ui-monospace`, and system fallbacks.
- Page title: 30px/36px, 650 weight.
- Section heading: 18px/26px, 650 weight.
- Body: 14px/21px.
- Compact metadata: 11px/16px, uppercase only for short labels, tracking `0.08em`.

Long email bodies use the body font, a 68ch measure, and 16px/26px sizing. Never render message bodies in a tiny control-panel size.

## Layout

- Desktop application shell: 224px left rail, flexible content, optional 360px context panel.
- The left rail has a 56px brand header, organization/project switcher, primary navigation, and a bottom account area.
- Main content max width is 1440px with 24px page padding; inbox rows and conversation timelines may use the full available width.
- Mobile: the rail becomes a drawer and detail pages are single-column. No horizontal page scrolling except code/DNS value blocks.
- Use 1px dividers to establish structure. Panels align to a 4px base grid.
- Default spacing scale: 4, 8, 12, 16, 24, 32, 48.
- Radius: 0px for table-like structural regions, 2px for controls, 4px maximum for dialogs and toast containers.
- No box shadows except a subtle `0 12px 32px rgb(17 41 29 / 0.12)` on modal dialogs and mobile drawers.

## Core components

- Primary button: forest background, white label, 38px height, 2px radius. Hover uses forest-strong. Focus has a 2px paper gap plus a 2px forest outline.
- Secondary button: transparent/paper background, 1px line-strong border, ink label.
- Destructive button: coral-dark text and border; filled only in the final destructive confirmation.
- Inputs: 40px minimum height, white background, 1px line border, visible label, help text, and inline field errors. Never rely on placeholders as labels.
- Status badge: compact inline flex, square 7px status marker plus text, 1px tinted border, mono 10px label. Use symbols for high-risk states.
- Metric tile: flat surface with hairline outline; mono eyebrow, 28px value, concise interpretation. Avoid vanity metrics.
- Inbox row: sender/avatar marker, subject/snippet, project/domain labels, security/priority state, owner-visible age, and unread indicator. Whole row is keyboard-focusable.
- Timeline item: vertical hairline with direction/security marker, header summary, recipients, sanitized body, attachment list, classifications, and audit expansion.
- DNS record: record type, host, exact value in copyable monospace block, TTL, verification result, and actionable error.
- Approval modal: names the exact revision, recipients, subject, and body hash; requires an explicit approval action and explains that future edits invalidate approval.
- Empty states are instructive and contain one next action. Error states preserve user data and include a request ID.

## Navigation and content hierarchy

Primary navigation order: Overview, Inbox, Reports, Notifications. A Settings group contains Domains, Schedules & retention, API tokens, and Audit. Show unread notification and quarantined-message counts without animation.

Breadcrumbs include organization and project where ambiguity is possible. Cross-project views must label every item with its project. Destructive and send actions remain project-scoped.

## Interaction and motion

- Use native navigation for primary page changes and CSRF-protected `fetch` for inline filters, polling, status changes, and draft actions.
- Motion is functional and respects `prefers-reduced-motion`.
- Standard transition is 120ms ease-out for color/border/opacity. No spring or looping animation.
- DNS and outbound polling show a text timestamp and non-animated progress state.
- Toasts remain until dismissed for failures; success toasts may dismiss after six seconds.
- All dialogs trap focus, close with Escape when safe, restore focus, and require an explicit button for send/destructive confirmation.

## Page-specific requirements

- Authentication pages use a centered 440px technical form with corner markers, a short trust statement, and no application rail.
- Onboarding is a numbered, resumable sequence. Show limits before the user reaches them.
- Dashboard leads with items requiring action, then security/health, domain readiness, and recent activity.
- Inbox supports query, project/domain/state/classification/security filters, opaque-cursor pagination, and a complete unfiltered baseline.
- Conversation view favors the timeline. AI output is explicitly labeled and visually secondary to original message content.
- Quarantine view never offers direct preview or download. It explains the verdict and records any administrative action.
- Domain wizard first detects existing MX. Existing mail service yields provider-forwarding as the recommended safe option; direct MX remains an explicit alternative.
- Reports clearly distinguish deterministic fallback reports from AI-generated reports.
- Settings expose retention defaults and hard product limits without suggesting unsupported billing/team features.

## Brand mark

Use a simple square forest mark containing an abstract white inbox tray intersected by a vertical routing line. Pair it with the full `Operational Inbox` wordmark in the navigation and `OI` only in compact contexts. Do not use generic envelope clip art.

## Implementation constraints

- Django templates, Tailwind CSS, vanilla JavaScript, and progressively enhanced HTML only.
- English interface copy throughout.
- Semantic landmarks, correct heading order, keyboard operation, 44px touch targets on mobile, and visible focus states.
- External images in message HTML are removed. The product UI itself does not depend on third-party runtime images or fonts.
- Use ONLY the fonts, colors, spacing, and component styles defined here. Do not introduce unrelated colors, fonts, gradients, shadows, or decorative visual styles.
