# Design QA

## Agent copy/paste onboarding refinement · 2026-08-11

### Comparison target

- Source visual truth: the user-provided `Screenshot 2026-08-11 at 13.50.17.png`, preserved as `.superdesign/qa/agents-bootstrap-reference.png`.
- Browser-rendered implementation: `.superdesign/qa/agents-bootstrap-implementation-1440x900.jpg`.
- Combined comparison input: `.superdesign/qa/agents-bootstrap-comparison.jpg` (reference left, implementation right).
- Responsive evidence: `.superdesign/qa/agents-bootstrap-implementation-780x900.jpg`.
- Comparison normalization: the 2048 × 1157 reference was proportionally resized to 1440 × 814; the implementation remains a 1:1 1440 × 900 browser capture. Both are shown without cropping in one labeled comparison image.
- State: authenticated Pro owner with one ready domain; Agents is active; menus are closed; the page is at its top position.

### Findings

- No actionable P0, P1, or P2 mismatch remains for the referenced copy/paste onboarding pattern.
- The primary experience now mirrors the reference's two-step hierarchy: open an agent, send one instruction, then use a high-contrast full-width Copy action.
- [Intentional product adaptation] The reference's marketing hero, video, and agent-logo strip are omitted. Operational Inbox keeps its authenticated product shell and replaces decorative content with official install, plugin-manifest, and MCP documentation links.
- [Intentional content correction] The prompt explains that copy/paste cannot create an unconfigured MCP connection by itself. It directs the agent to the canonical install contract, requests browser OAuth authorization without asking for secrets, verifies authorized domains, and limits the initial smoke test to read-only work.

### Required fidelity surfaces

- Hierarchy and layout: the two numbered steps stay adjacent to a bordered instruction card at 1440px. At 780px they stack, the prompt becomes an internal scroll area, and the primary Copy button remains fully visible in the initial 900px viewport.
- Typography and color: the existing Space Grotesk, Inter, and JetBrains Mono stacks and paper, surface, ink, forest, muted, and line tokens are preserved. The prompt action uses the same high-contrast black/forest treatment as the reference without introducing a second visual system.
- Copy and content: the visible prompt contains the canonical `INSTALL.md`, plugin manifest, MCP endpoint and documentation URLs; OAuth 2.1 with PKCE; a no-secrets boundary; a connection verification step; and read-only initial behavior.
- Accessibility: the setup is a labeled semantic section with an ordered list, real links, a real button, `aria-live` status feedback, keyboard focus styling, and scroll-contained long prompt text.

### Behavior and validation

- Copy behavior: browser testing showed `Copied`, reset to `Copy agent prompt` after 1.6 seconds, and macOS clipboard verification confirmed all 1,364 prompt characters, the canonical install URL, and the no-secrets instruction were copied.
- Responsive behavior: the 780 × 900 render measured `clientWidth: 780` and `scrollWidth: 780`; no horizontal document overflow is present.
- Browser console: no errors after the final desktop interaction pass.
- Automated validation: Django checks, Tailwind production CSS, JavaScript syntax, and `git diff --check` pass. The full suite passes with `290 passed` and 79.22% coverage.

### Comparison history

- Pass 1: translated the reference's prominent agent-instruction card into the Operational Inbox design system and kept the existing task prompts as secondary post-setup examples.
- Pass 2: constrained the stacked prompt panel so the primary Copy action is visible at 780 × 900 while retaining the full prompt in an internal scroll area.
- Pass 3: combined the attached reference and final browser render in one comparison image, verified the system clipboard contents, and found no remaining P0/P1/P2 issue.

## Agents integration guide · 2026-08-11

### Comparison target

- Selected visual: `https://p.superdesign.dev/draft/2a62f557-452f-4658-9290-fe5ec789c5a2`
- Source visual truth: `.superdesign/qa/agents-task-oriented-reference-1440x900.png`
- Browser-rendered implementation: `.superdesign/qa/agents-implementation-1440x900.png`
- Full-view comparison evidence: `.superdesign/qa/agents-desktop-comparison.png` (source left, implementation right)
- Focused content comparison: `.superdesign/qa/agents-content-comparison.png` (page header, integration cards, and prompt gallery at 1:1 scale)
- Responsive evidence: `.superdesign/qa/agents-implementation-780x900.png`
- Viewport and density: source and implementation are both 1440 × 900 pixels at a 1440 × 900 CSS viewport and DPR 1. No density normalization or resizing was applied before comparison.
- State: authenticated Pro owner with one ready domain; Agents is the active top-bar destination; Settings and profile menus are closed.

### Findings

- No actionable P0, P1, or P2 visual mismatch remains.
- [Intentional shell difference] The implementation preserves the production active-domain label and profile menu. The selected mock replaces that live context with static “Agent Center / Integration Guide” copy and omits the profile control.
- [Intentional active-state difference] The implementation fills the Agents tab with the existing forest active-navigation treatment instead of using the mock's weaker outlined state. `aria-current="page"` communicates the same state semantically.
- [P3] The generated mock's decorative card and prompt icons are omitted. The production app has no shared icon-library runtime for these symbols; the implementation uses numbered mono labels and avoids fake glyphs or handcrafted SVGs.
- [Intentional content correction] Prompt and skill text follows the checked-in plugin and skill contracts. Unsupported audit notification behavior and ambiguous conditional-send copy from the generated mock were not reproduced.

### Required fidelity surfaces

- Fonts and typography: the implementation uses the product's Space Grotesk heading, Inter body, and JetBrains Mono technical stacks with the same 30px title, 18px section heading, 14px body, and compact uppercase-label hierarchy. System fallbacks remain intentional because production loads no remote fonts.
- Spacing and layout rhythm: the 224px rail, 56px top bar, 1440px content maximum, three equal integration cards, two-column prompt grid, hairline section dividers, and 4px-based spacing match the source composition. The 780px capture stacks cards and retains every top-bar control without clipping.
- Colors and visual tokens: paper, surface, ink, muted, forest, and line tokens are used directly. Panels stay flat and square; no new gradient, glass effect, shadow, or radius language was introduced.
- Image quality and assets: the only visible branded image is the existing canonical vector logo. The view adds no decorative raster imagery or placeholder assets.
- Copy and content: Plugin, Skill, and Prompt are distinguished accurately; all four bundled skill names are exact; example prompts state send/resend boundaries; OAuth 2.1 with PKCE, personal API tokens, and the `read`, `write`, `manage_domains`, and `send` scopes reflect repository source-of-truth documentation.
- Accessibility: one page-level heading, semantic section labels, real buttons, visible focus styles, `aria-current`, `aria-live` copy feedback, a scroll-safe scope table, and a keyboard-operable responsive navigation are present.

### Behavior and validation

- Copy controls: browser testing confirmed prompt text reaches the clipboard, the control announces `Copied`, and its label resets to `Copy` after 1.6 seconds.
- Navigation: Agents is present and active before Settings; Settings opens and closes with Escape; the API-token settings link resolves to `/app/settings/api-tokens/`.
- Responsive navigation: at 780 × 900 the Agents tab remains visible, the mobile menu opens the primary rail, and the backdrop closes it.
- Browser console: no errors after the final desktop reload.
- Automated validation: Django system checks pass; Tailwind production CSS builds; all 36 tests in `tests/test_web_flows.py` pass.

### Comparison history

- Pass 1: compared the selected mock and first browser render at equal 1440 × 900 dimensions. The composition, panel geometry, density, typography hierarchy, and palette matched; production shell and content-truth differences were classified as intentional.
- Pass 1 interaction finding [P1]: Clipboard API completion was not reliable in the in-app browser, so Copy controls could fail to show deterministic success.
- Fix: replaced the permission-dependent write with a user-gesture-scoped textarea selection and `execCommand("copy")` fallback, preserving `Copied` and reset feedback.
- Pass 2: browser verification confirmed copied text, success/reset feedback, keyboard-operable Settings, responsive rail behavior, and zero console errors. No actionable P0/P1/P2 findings remained.

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
- Copy: the offer highlights the verified 20-domain limit, unlimited routed addresses, the all-domain agent feed, optional drafts, and approved outbound sending; personal API access is presented on both plans without overstating mailbox provisioning.
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
