# Operational Inbox implementation QA

9 October 2026 · local `main` · approved Superdesign v9

**Findings**

No actionable P0, P1, or P2 findings remain. The comparison history below records the issues fixed before acceptance. This is a production implementation of the approved information architecture; expected differences from the fixture-only prototype are explicit below.

**Comparison target and evidence**

Source visual truth: [approved proposal](/Users/onurmatik/Projects/OperationalInbox/.superdesign/simplify/proposal.html), [saved Superdesign v9](https://p.superdesign.dev/draft/e21c2fe9-0642-4f5c-8ed6-629c410e7aac), and these source captures:

| State | Source capture | Rendered Django capture | Combined comparison |
| --- | --- | --- | --- |
| Desktop Inbox, Alex selected, draft collapsed | [14](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/14-proposed-conversations.png) | [07](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/07-desktop-final.png) | [23](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/23-desktop-comparison.png) |
| Mobile Inbox list | [20](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/20-proposed-mobile-list.png) | [29](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/29-mobile-list-final.png) | [36](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/36-mobile-list-comparison-final.png) |
| Mobile Alex thread, draft collapsed | [21](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/21-proposed-mobile-thread.png) | [45](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/45-mobile-thread-final-valid.png) | [46](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/46-mobile-thread-comparison-final.png) |
| Desktop Domains | [18](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/18-proposed-domains.png) | [42](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/42-desktop-domains-final.png) | [48](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/48-desktop-domains-comparison-final.png) |
| Mobile Domains | [22](/Users/onurmatik/Projects/OperationalInbox/.superdesign/audit/2026-10-09/22-proposed-mobile-domains.png) | [43](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/43-mobile-domains-final.png) | [47](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/47-mobile-domains-comparison-final.png) |

Desktop CSS viewport and both source/implementation captures are 1440 × 900; mobile is 390 × 844. Device pixel ratio is 1. No density resizing was used. Each combined image places equal-size captures side by side and adds a 32px caption strip. The viewer reduced some 2880px full comparisons for display; focused comparisons were opened separately to inspect readable detail.

Focused evidence: [navigation and list](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/24-desktop-navigation.png), [thread typography and controls](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/25-desktop-thread.png), [domain header and rows](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/49-desktop-domains-focused-final.png), and [mobile domain header/card](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/50-mobile-domains-focused-final.png). The 780px mobile comparisons are readable at native size and serve as both full-view and detail evidence.

Implementation routes were `/app/inbox/`, `/app/inbox/?conversation=…`, `/app/domains/`, `/app/domains/new/`, domain detail, and `/app/settings/` on `http://127.0.0.1:57496/`. Authenticated synthetic owner on Pro; light theme; separate disposable SQLite database; no worker. The Alex subject and full message body match the source. Dynamic dates, sender display names, unread state, recipient addresses, and domain readiness differ. Opening a thread marks it read. The source has two example domains; the runtime fixture has four, including the send-only domain created during QA. Domain-state comparisons judge the structure and controls, not identical fixture contents.

An IAB repeated-capture artifact omitted the sticky toolbar in some intermediate thread PNGs although the DOM remained visible and hit testing found the toolbar. Those frames are not final evidence. Capture 45 was taken as one complete frame with its toolbar visible and was opened again in comparison 46. Capture 03 also used the wrong viewport and was excluded.

**Comparison history**

| Finding and initial result | Impact | Fix | Post-fix evidence |
| --- | --- | --- | --- |
| P2: extra thread header/list summary, different list density and mail typography. Initial comparison blocked. | Reduced reading space and shifted the message below the source position. | Removed redundant header, made list summary screen-reader-only, restored 400px list, 48px desktop reading padding, 24/32 heading and 16/26 body, compact rows. | [Initial comparison 02](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/02-initial-comparison.png), then final 23, 24, 25. |
| P2: draft and compose forms reused subject/body input IDs. Interaction QA blocked. | Compose labels could resolve to the hidden draft form. | Separate Django form `auto_id` prefixes; POST field names preserved. | [Filled compose 10](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/10-compose-filled.png); Subject/Message labels filled the correct form; final DOM had no duplicate IDs. |
| P1: send-only domain detail returned 500. Flow QA blocked. | A newly connected send-only domain could not show its next step. | Added missing receiving-enable URL; added web create → detail → enable-receiving regression coverage. | [Failure 18](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/18-send-only-provisioning.png), [corrected detail 20](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/20-send-only-corrected.png); test passes. |
| P2: mobile Connect domain wrapped into two lines; primary mail controls were below the specified 44px touch size. Responsive QA blocked. | Cramped CTA and small controls on touch devices. | Stacked mobile heading/CTA, prevented label wrapping; set primary buttons, search/domain controls and thread actions to 44px. | Final 36, 46, 47; DOM measured all four thread actions at 44 × 44 and search/New email at 44px tall. |
| P2: Domains used an overly wide contiguous list instead of the source's centered cards. Visual QA blocked. | Changed the major-region proportions and mobile scanning rhythm. | Centered 896px desktop content, separate 88px rows with 16px gaps, stacked mobile capability lines and next action; removed redundant eyebrow/divider. | Earlier [39](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/39-desktop-domains-comparison.png), then final 47, 48, 49, 50. |

**Required fidelity surfaces**

| Surface | Final evaluation |
| --- | --- |
| Fonts and typography | Self-hosted Inter for interface/body, Space Grotesk 600 for headings, JetBrains Mono for hostnames/technical values. Desktop message heading, body size, line height, paragraph spacing and wrapping match the source in 25. Small list metadata remains readable; full sender address substitutes for unavailable display names. Mobile subject uses the full line beneath actions. |
| Spacing and layout rhythm | Sidebar 224px, desktop list 400px, toolbar 56px, tabs 44px. Final desktop message aligns with the source. Domain content width/cards restored. Mobile list → detail uses the full 350px reading width; no horizontal overflow at 390px or 768px. Mobile toolbar is taller to accommodate 44px controls. |
| Colors and tokens | Paper, white surfaces, forest action/navigation, ink, muted text and flat dividers follow the approved palette. Quarantine uses the design system's purple semantic token rather than the prototype's coral badge. Status is stated in text as well as color. No decorative gradients/shadows. |
| Image quality and assets | Exact existing logo reused. Official Lucide SVG assets and licenses included; consistent stroke/size, no handmade icon substitutions. WOFF2 fonts are local and licensed. No raster product imagery or illustrations were required by the source. |
| Copy and content | Main navigation, folder names and email fields follow the brief. Production controls use “Connect domain”, “Review reply draft”, explicit Receiving/Sending state and actual delivery facts. No audit/design prompt text appears in product copy. Message prose is synthetic fixture content, not a server-side AI feature promise. |

**Intentional implementation differences / open questions**

No unresolved question blocks acceptance. The following differences are classified as intentional product constraints:

- Domains retains the reusable app toolbar/navigation rather than the prototype's bare Menu label. Its desktop content starts lower; content width and card rhythm match the intended design.
- Mobile thread moves actions to a separate row for 44px touch targets, gives the subject full width, and hides folder tabs while reading. The labeled Back action returns to the current list. This changes subject wrapping and above-fold density without hiding mail controls.
- A small Filters disclosure, Trash action, RFC Details, tags disclosure, timestamps and actual mailbox addresses retain existing mail-management functions through progressive disclosure. The source omitted some of these functional states.
- Hostname text uses a stronger weight for scanning. Redundant setup-goal labels are omitted from domain cards; explicit Receiving/Sending capabilities state whether each is Ready, Not enabled or Setup needed.
- Relative mock timestamps, decorative unread flags and static domain status labels are replaced by server facts. A just-opened thread is read; quarantined content remains locked.

**Browser interactions and supporting evidence**

- Desktop list/thread and native compose; filled From with an arbitrary local part, To, Subject, Message; submitted synthetic mail to the isolated queue. [Queued Sent state](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/11-queued-sent.png). No provider worker ran.
- Collapsed draft → exact-revision review dialog → Close. [Review](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/08-draft-review.png). Approval/send was not clicked.
- Mobile list → thread → Back; Archive → Restore returned the fixture conversation to Inbox.
- Explicit domain filter, search and empty result retained URL scope. [Empty state](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/15-mobile-empty-search.png).
- Mobile compose fit the viewport; Escape closed the dialog and restored focus to New email. [Compose](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/14-mobile-compose.png).
- Mobile menu, Domains, send-only choice/create/detail; receiving was not required. Existing-provider forwarding DNS instructions and Copy host worked. [DNS](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/21-mobile-dns-setup.png).
- Quarantine body/attachments stayed unavailable. [Locked thread](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/32-mobile-quarantine.png).
- Agent connection dialog and Settings tabs. [Agent](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/33-mobile-agent.png), [Settings](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/22-mobile-settings.png).
- Tablet list at 768 × 900: page scroll width 768px; search and New email 44px tall. [Tablet](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/35-tablet-list.png).
- Browser console checked after fixes: no warning/error entries in the final fresh session. [Console evidence](/Users/onurmatik/Projects/OperationalInbox/.superdesign/implementation/2026-10-09/console-errors.json). Earlier server-side 500 is recorded above, fixed and covered by a regression test.

Preview supervisor was stopped after QA: URL `http://127.0.0.1:57496/`, port 57496, final process group 84216; `CODEX_DJANGO_STOPPED=1`, closed socket and removed heartbeat confirmed. Temporary final QA tab closed and viewport reset. The user's pre-existing server was left running.

**Validation and remaining test scope**

- Full suite: 417 passed, 1 skipped; coverage 80.98% against the required 70%. The skip is PostgreSQL row-lock concurrency on the SQLite test backend.
- Latest template/CSS changes: 72 relevant web/compose/history tests pass. The focused suite runs with `--no-cov`; whole-project coverage is established by the full suite above.
- Ruff passes; Mypy passes for 77 source files; Django system checks, migration drift check, OpenAPI export check and Git whitespace check pass.
- Migration 0015 tested forward and reverse with populated retired models. Local main migration applied after a protected SQLite backup; retained record counts unchanged, integrity check `ok`, zero foreign-key violations, retired tables absent.
- Real provider DNS changes, mail delivery and production deployment were outside this local QA. Full screen-reader compliance and text-zoom testing were not performed.

**Implementation checklist**

- [x] Apply approved Conversations/Domains information architecture.
- [x] Preserve receiving/sending, exact revision approval, ownership and tenant boundaries.
- [x] Fix every identified P1/P2 issue and capture post-fix evidence.
- [x] Open final full and focused side-by-side comparisons.
- [x] Check desktop, mobile, tablet, interactions and console.
- [x] Stop coupled preview and save report.

**Follow-up polish**

No P3 item is required for handoff.

final result: passed
