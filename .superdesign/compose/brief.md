# Compose: authorized single direction

New route /app/compose/ in existing Django application. Use AppShell with Outbox active. Header eyebrow menu.fit, h1 Compose, helper: Send a new email from your domain.

Single form, native fields, in this order: Domain (select menu.fit), From (local part input onur plus visibly fixed suffix @menu.fit); To (single email input), Subject (single-line), Body (large plain text textarea). On desktop Domain and From share a two-column row; To, Subject, Body span full form. On mobile stack everything, keep suffix readable without horizontal overflow.

Only Send primary and Cancel secondary footer actions. No approval screen, save draft, autosave, template, formatting toolbar, CC/BCC, attachments or extra cards. Do not add placeholders implying supported HTML. Preserve typed content on validation/pause/quota errors. Success navigates to domain-filtered Outbox with authoritative status. Unsent content is not persisted. Domain changes update fixed suffix.

Compose entry buttons belong in Inbox and Outbox headers. The selected current domain is the default.
