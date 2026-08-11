# Operational Inbox — one-paste setup for agents

The user wants to connect the current agent client to Operational Inbox. First identify the current
client, then complete only the applicable steps below in order. Pause only when native OAuth needs
the browser. Do not create a custom OAuth client, PKCE script, API wrapper, or mailbox smoke-test
program.

The setup is: **install the skill → configure native MCP → authenticate → reload the agent**.

## Step 1 — Install or update the standalone skill

If `$operational-inbox` is already loaded from the public `onurmatik/operational-inbox` repository,
continue to Step 2. Otherwise run:

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
and configures `https://operationalinbox.com/mcp` with OAuth and write-action approvals.

For any other client, do not run the Codex installer or edit `~/.codex/config.toml`. Follow
`https://operationalinbox.com/mcp-docs/` to add `https://operationalinbox.com/mcp` through that
client's native Streamable HTTP MCP configuration.

## Step 3 — Use native OAuth

In Codex, if the installed `codex` CLI supports MCP commands, run:

```bash
codex mcp login operational-inbox
```

Wait for the user to finish Operational Inbox sign-in and consent in the browser. If the CLI does not
support `codex mcp`, do not improvise another OAuth flow. Tell the user to restart Codex, open
**Settings → MCP servers → operational-inbox**, and select **Authenticate**.

In another client, use that client's native MCP authentication control. Do not substitute a custom
OAuth implementation.

Never ask the user to paste an OAuth code, access token, refresh token, API key, password, or other
secret into chat.

## Step 4 — Stop at the reload boundary

Do not try to call Operational Inbox from the current task when the client requires a reload after
skill or MCP configuration changes. Tell the user to reload that agent and start a new task so the
skill and MCP tools load cleanly.

In that new task, use `$operational-inbox` and call `list_domains` for a read-only connection check.
Do not read mailbox content merely to prove setup succeeded.
