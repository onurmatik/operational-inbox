# Reply composer

The local Django project is authoritative. Continue the existing Inbox draft and preserve its
shell, list, message timeline, split-pane resizing, design tokens and mobile navigation.

Replace the interaction pane with one Reply composer: To, collapsed Subject, Message textarea,
Write with AI, Review, Save draft, Send reply. Review is available only with nonempty reply text.
Write with AI reveals a small Instructions textarea and up to three contextual intent chips.
For the synthetic link-exchange thread use Accept and Decline; neither is selected automatically.
Selecting a chip only fills Instructions. Generate draft produces a preview, not a send or save.
Review shows brief notes plus a suggested subject/body. Apply copies the suggestion into the
composer; Dismiss preserves the original. Save draft explicitly saves; Send reply sends exactly
the editor content. Keep concise inline loading/error/saved indicators and keyboard access.

Remove Take over, Reply with agent, policy/automation status, agent history, automatic delivery,
follow-up and handling-related Needs review UI. Connect your agent remains a separate navigation
entry for MCP setup. No autonomous actions, no AI chat, no domain knowledge settings.
Use progressive disclosure and the existing visual style. No new dashboard cards or explanations.
