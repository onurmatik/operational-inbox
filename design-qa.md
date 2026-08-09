# Design QA

## Billing upgrade offer · 2026-08-02

### Comparison target

- Approved visual: `https://p.superdesign.dev/draft/3c832055-6edb-41b6-8a0e-2336ac62a44b`
- Source capture: `.superdesign/qa/approved-upgrade-reference.png`
- Implementation capture: `.superdesign/qa/implementation-upgrade-desktop-final.png`
- Side-by-side evidence: `.superdesign/qa/upgrade-desktop-comparison.png` (source left, implementation right)
- Compact-layout evidence: `.superdesign/qa/implementation-upgrade-compact-780.png`
- State: authenticated Free account, one ready domain, Stripe configured, USD 9.99 comparison price, and USD 4.99 monthly promotional price.
- Desktop normalization: both comparison captures are 1440 × 900 pixels at a 1440 × 900 CSS viewport and DPR 1. The composite preserves both images at 1:1 scale.
- Compact normalization: full-page 780 × 1688 CSS viewport at DPR 1. A separate 390 × 844 DOM pass measured `scrollWidth: 390`, `main: 390`, page header `358`, and plan section `358`, confirming no horizontal document overflow. The browser's narrow device-scale screenshot was excluded because its capture did not preserve CSS-coordinate fidelity.

### Findings

- No actionable P0, P1, or P2 visual mismatch remains.
- The promotion retains the existing product's flat panels, square geometry, forest/gold palette, type hierarchy, and restrained density.
- The price hierarchy is explicit and semantic: `USD 9.99` is rendered in `<del>`, `USD 4.99 / month` is primary, and both the badge and label describe the offer as limited-time.
- The original comparison-table defect is resolved in the rebuilt production Tailwind CSS. Capability, Free, and Pro render as three aligned columns in the desktop and compact captures.
- [Intentional product-truth difference] The implementation says “Receive at any address · no per-address fee” instead of promising created mailboxes or “unlimited email addresses.” The system accepts arbitrary local parts but does not provision separate mailboxes.
- [Intentional shell difference] The implementation preserves the real active Plan & billing navigation item, Free status badge, and global “Upgrade to connect domain” action. The approved visual's detached Free badge was not copied.
- [Intentional icon difference] Decorative check glyphs from the generated reference were omitted. The existing design system does not use them here, and no fake symbol or handcrafted asset was introduced.

### Required fidelity surfaces

- Typography: Space Grotesk headings, Inter body text, and JetBrains Mono labels preserve the approved hierarchy, weights, line height, and tracking.
- Spacing: the plan panel, price block, benefit dividers, full-width CTA, and comparison panel align to the approved vertical rhythm at desktop size.
- Color and shape: existing paper, surface, ink, forest, muted, line, and gold tokens are used directly; no new palette, radius language, shadow, or gradient was introduced.
- Copy: the offer highlights the verified 20-domain limit, unlimited routed addresses, the all-domain agent feed, scoped API access, optional drafts, and approved outbound sending without overstating mailbox provisioning.
- Accessibility: price comparison uses semantic deletion markup, highlights are a labeled list, the checkout remains a semantic POST form, and existing focus-visible behavior is preserved.

### Behavior and validation

- The checkout CTA was inspected without submission; automated coverage asserts the exact POST action and `Upgrade to Pro · USD 4.99/month` label.
- Promotion display is restricted to exactly USD 4.99 with a USD 9.99 comparison price. USD 5.99, EUR 4.99, and USD 29.00 remain standard-price states.
- Free/unconfigured, Free/promotional, non-promotional, and active Pro states are covered. The checkout price payload is asserted at 499 cents.
- Browser console check: no warnings or errors.
- Verification: `18 passed` in `tests/test_billing_freemium.py`; Ruff, Django system checks, Tailwind production build, and `git diff --check` pass.

### Comparison history

- Pass 1: rebuilt the stale Tailwind output, restoring the three-column comparison table.
- Pass 2: implemented the approved offer, then tightened spacing to align the plan-panel bottom edge and CTA placement with the source.
- Pass 3: compared equal-size desktop captures side by side. Remaining differences were reviewed as intentional product-shell or product-truth decisions, not fidelity defects.

## Historical landing-page QA

- Source capture: `.superdesign/qa/reference-desktop.png`
- Implementation capture: `.superdesign/qa/implementation-desktop.png`
- Side-by-side evidence: `.superdesign/qa/desktop-comparison.png`
- Viewport/state: anonymous landing page, light theme, initial domain form, 1280 × 720 CSS viewport.
- Result: no actionable P0, P1, or P2 mismatch remained; the canonical SVG logo and library-sourced Lucide ArrowRight were intentionally retained.

final result: passed
