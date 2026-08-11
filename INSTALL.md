# Operational Inbox — one-paste setup for agents

The user wants to connect the current agent client to Operational Inbox. First identify the current
client, then complete only the applicable steps below in order. Pause only when native OAuth needs
the browser. Do not create a custom OAuth client, PKCE script, API wrapper, or mailbox smoke-test
program.

The setup is: **install the skill → configure native MCP → authenticate → verify the connection**.

## Step 1 — Install or update the standalone skill

Run this even when `$operational-inbox` is already available so the shared copy matches the current
public setup and operating instructions:

```bash
npx -y skills@latest add onurmatik/operational-inbox \
  -g -s operational-inbox -y --copy
```

The global install intentionally places one shared copy at `~/.agents/skills/operational-inbox` for
every compatible agent that uses the common Agent Skills directory. The installer may skip a
detected client that does not support global skills. The shared skill includes its UI metadata,
logo, operating rules, and native MCP dependency.

## Step 2 — Configure the client's native MCP connection

If the current client is Codex, run the deterministic installer shipped with the skill:

```bash
python3 ~/.agents/skills/operational-inbox/scripts/install_codex_mcp.py
```

The installer preserves the rest of `~/.codex/config.toml`, refuses to replace a conflicting server,
and configures `https://operationalinbox.com/mcp` with OAuth and write-action approvals. It then
finds an MCP-capable Codex CLI—checking both `PATH` and the CLI bundled with the macOS Codex
application—and starts the client's native OAuth login.

For any other client, do not run the Codex installer or edit `~/.codex/config.toml`. Follow
`https://operationalinbox.com/mcp-docs/` to add `https://operationalinbox.com/mcp` through that
client's native Streamable HTTP MCP configuration.

## Step 3 — Complete native OAuth

The Codex installer normally starts this command itself with the required scopes:

```bash
codex mcp login --scopes read,write,manage_domains,send operational-inbox
```

Wait for the user to finish Operational Inbox sign-in and consent in the browser. If the CLI does not
support `codex mcp` and no application-bundled CLI is available, do not improvise another OAuth
flow. Follow the installer's fallback: open **Settings → MCP servers → operational-inbox** and
select **Authenticate**. Restart Codex only if the newly configured server or its authentication
control is not visible.

In another client, use that client's native MCP authentication control. Do not substitute a custom
OAuth implementation.

Never ask the user to paste an OAuth code, access token, refresh token, API key, password, or other
secret into chat.

## Step 4 — Verify before any reload

After OAuth completes, first check whether the `operational-inbox` MCP tools are available in the
current task. If they are, call `list_domains` for a read-only connection check. If it succeeds,
setup is complete; do not ask the user to reload or restart anything. Do not read mailbox content
merely to prove setup succeeded.

Only when the tools are unavailable should you ask the user to use that client's native reload or
restart action, start a new task if the client requires it, and retry `list_domains` once. Do not
prescribe a universal restart for other agents. If the tool is available but returns an
authentication error, reconnect with the client's native OAuth control instead of restarting.
