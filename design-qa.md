# Contextual reply composer

The latest implementation and verification are recorded in the final section below. Earlier
sections describe superseded reply and handling flows.

- Source visual truth: `/Users/onurmatik/.codex/generated_images/01a190b7-7570-7250-a112-8cb54057e9cc/exec-3214b03d-62ec-415f-907f-c0751409758d.png`
- Target: selected inline composer, with conversation-specific suggestions, editable instructions,
  and separate delivery choices. Existing application layout and design tokens are retained.
- Intended state: conversation open, Reply with agent expanded, one suggestion selected,
  draft delivery selected. Also check custom, loading, unavailable, stale and submit error states.
- Intended viewports: desktop 1440 × 1000 and mobile 390 × 844; source is a reading-pane concept.
- Implementation screenshot: unavailable.
- Density normalization, full-view comparison and focused comparison: not performed.
- Browser access: CUA reports “Local browser use is disabled in Work settings”.

## Findings

- Visual verification is blocked. Automated server and syntax tests cannot establish visual
  fidelity or browser interaction correctness.
- Fonts/typography: existing Inter and Space Grotesk reused; rendered wrapping unverified.
- Spacing/layout rhythm: inline panel, wrapping choices and 44px controls implemented;
  responsive layout and overflow unverified.
- Colors/tokens: existing forest, paper, ink and line tokens reused; rendered contrast unverified.
- Assets: no new raster assets required; existing app icons retained.
- Copy/content: goals come from conversation context; no fixed fallback goal menu.
  Custom instructions remain available when generation fails. Delivery is explicit.
- Keyboard/focus, browser console errors, primary interaction flow and screenshot comparison
  still require browser access. No visual comparison iteration has been completed.

## Remaining verification

1. Enable local browser use and open the isolated synthetic preview.
2. Verify open/cancel, contextual goals, custom edits, review/automatic delivery and errors.
3. Capture desktop/mobile and compare the reading pane against the selected reference.
4. Fix visible issues and repeat comparison before marking the design ready.

## Bounded behavioral QA — November 11, 2026

The browser restriction above describes the original implementation pass. Browser access was
available for this follow-up. The checks below used the current Django templates and production
JavaScript against a loopback-only synthetic HTTP fixture. No real mailbox, model API, scheduler,
or outbound delivery was used.

Two reproduced defects were fixed locally:

- The hidden `name="action"` field shadowed `form.action`, so JavaScript posted to
  `[object HTMLInputElement]` instead of the conversation handling endpoint. Submission now reads
  the explicit form action attribute.
- Changing delivery while a submission was pending replaced the pending label. After failure,
  the old label was restored even if the selected delivery had changed. The label now remains
  `Queuing…` while pending and reflects the current delivery choice after failure.

Browser checks passed for opening and cancelling the editor, reopening with custom instructions,
editing a suggestion, separate draft/automatic labels, preserving typed instructions while
suggestions load, retrying unavailable suggestions, stale-suggestion blocking, submission error
recovery, and a successful mobile submission to the synthetic endpoint. The fixture rejects
unexpected endpoint paths. Desktop and mobile sizes were 1440 × 1000 and 390 × 844.

Four dependency-free JavaScript regression tests exercise the actual production handlers:
`npm run test:ui`. They cover both delivery-change directions during failure, duplicate
submission suppression, stale submission blocking, the named-action collision, and successful
submission reload. The Python suite passed with 565 tests passed, one PostgreSQL-only test
skipped, and 81.76% coverage. Lint, format, mypy, migration drift, CSS build and Django deploy
checks passed. CDK synthesis was blocked by network policy for `cli.cdk.dev-tools.aws.dev`.

This is behavioral verification, not a complete comparison to the original design reference.
The original reference-image comparison remains unperformed. Production delivery and the
post-deploy scheduler were not tested in this pass. The fixes have not been committed, pushed,
or deployed.

Current result: bounded behavioral QA passed; reference comparison and release remain pending.

## Manual reply and mode switching — November 14, 2026

The production conversation exposed only agent instructions. Cancel closed that panel and
preserved the owner's text, but there was no manual reply editor to return to. The unpublished
named-action fix above was retained.

The local implementation now includes an owner-authored reply editor, a reversible
Reply with agent / Reply myself toggle, and Cancel returning to the manual editor. Active agent
handling still requires Take over before the manual editor appears. Sending snapshots the exact
owner text in the existing thread, persists takeover and uses an idempotent outbound request.

Browser checks used the current Django views, templates and JavaScript with a temporary synthetic
SQLite database on loopback. Manual text survived both mode switches and Cancel. Validation errors
stayed in the editor; a successful manual send appeared as Queued in the same synthetic thread.
The agent form posted successfully to the handling endpoint, and Take over restored the manual
editor. No scheduler or provider delivery ran. The original production reply text was verified
unchanged, and no real email was sent.

Screenshot: `output/playwright/manual-reply-2026-11-14.jpg` (synthetic preview, normal desktop viewport).
The preview tab and server were stopped after verification. No mobile viewport or reference-image
comparison was performed in this pass.

Validation: 583 Python tests passed, one PostgreSQL-only test skipped; eight JavaScript tests
passed. Ruff, mypy, migration drift, CSS build, Django deploy checks and CDK synthesis passed.
CDK reported its existing Node 26 compatibility warning. Changes remain local and unpublished.

## Manual composer and explicit AI assistance — November 14, 2026

Conversation autopilot and its settings, handling state and delivery choices have been removed.
The composer opens the same saved draft exposed through MCP. AI options fill instructions;
Generate draft and Review show a preview, Apply changes the editor, Save draft persists an
immutable revision, and Send reply uses the exact editor content without a second dialog.

The existing Superdesign project and inbox draft were reused (version 12). Only the existing
compact design-system brief and the new reply-flow brief were shared. The implementation retains
the local application shell, design tokens and resizable thread panes.

Browser checks used the actual Django views and JavaScript with an isolated synthetic SQLite
database on loopback. Desktop was 1440 × 900; mobile was 390 × 844. Both layouts remained within
the viewport width. Keyboard navigation reached Generate, Apply and Send. Review focused its
preview and Apply returned focus to the editor. The reply textarea was reduced to five rows so
the main desktop actions fit without scrolling at the tested size.

AI results were stubbed in the browser; Apply context validation, Save and Send used the real
backend. Saved text survived reload. A keyboard Send queued one owner-authorized attempt with
the complete editor text and retained the same draft's three immutable revisions. No worker or
provider delivery ran. The unavailable-model path preserved manual text and controls. The final
page had no browser console errors or warnings.

Screenshots: `output/playwright/reply-desktop.png`, `output/playwright/reply-mobile.png`,
`output/playwright/reply-review-mobile.png`, and `output/playwright/reply-send-queued.png`.

Validation: 530 Python tests passed, one PostgreSQL-only test skipped, 82.14% coverage; nine
JavaScript tests passed. Ruff, format, mypy, Django deploy checks, migration drift, OpenAPI
contract, CSS build and CDK synthesis passed. CDK reported its existing Node 26 compatibility
warning. Tests cover model timeout, concurrent revisions, stale context, tenant
isolation, quarantine, duplicate sending and migration data preservation. Live model responses
and provider delivery were not exercised. Changes remain local and unpublished.
