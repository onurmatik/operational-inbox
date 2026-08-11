# Operational Inbox — one-paste setup for Codex

The user wants to connect Codex to Operational Inbox. Complete every applicable step below in order.
Pause only when native OAuth needs the browser. Do not create a custom OAuth client, PKCE script,
API wrapper, or mailbox smoke-test program.

The setup is: **install the skill → configure native MCP → authenticate → restart Codex**.

## Step 1 — Install or update the standalone skill

If `$operational-inbox` is already loaded from the public `onurmatik/operational-inbox` repository,
continue to Step 2. Otherwise run:

```bash
npx -y skills@latest add onurmatik/operational-inbox \
  -g -a codex -s operational-inbox -y --copy
```

This installs the skill persistently at `~/.agents/skills/operational-inbox` with its UI metadata,
logo, operating rules, and native MCP dependency.

## Step 2 — Configure Codex's native MCP connection

Run the deterministic installer shipped with the skill:

```bash
python3 ~/.agents/skills/operational-inbox/scripts/install_codex_mcp.py
```

The installer preserves the rest of `~/.codex/config.toml`, refuses to replace a conflicting server,
and configures `https://operationalinbox.com/mcp` with OAuth and write-action approvals.

## Step 3 — Use native OAuth

If the installed `codex` CLI supports MCP commands, run:

```bash
codex mcp login operational-inbox
```

Wait for the user to finish Operational Inbox sign-in and consent in the browser. If the CLI does not
support `codex mcp`, do not improvise another OAuth flow. Tell the user to restart Codex, open
**Settings → MCP servers → operational-inbox**, and select **Authenticate**.

Never ask the user to paste an OAuth code, access token, refresh token, API key, password, or other
secret into chat.

## Step 4 — Stop at the reload boundary

Do not try to call Operational Inbox from the current task after changing Codex configuration. Tell the
user to restart Codex and start a new task so the skill and MCP tools load cleanly.

In that new task, use `$operational-inbox` and call `list_domains` for a read-only connection check.
Do not read mailbox content merely to prove setup succeeded.

For clients other than Codex, use `https://operationalinbox.com/mcp-docs/` to configure the same remote
MCP endpoint through that client's native OAuth flow.
