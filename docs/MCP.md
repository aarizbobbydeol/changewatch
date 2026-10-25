# Ask your AI assistant about releases (MCP server)

`mcp-server/changewatch_mcp.py` lets an assistant that supports the Model Context Protocol (Claude Code,
Claude Desktop and others) read ChangeWatch for one project: "is the deploy I just shipped safe?",
"what broke in incident 25?", "write the postmortem for it".

- **Read-only.** It only makes GET requests with the project's **read key**. It cannot acknowledge,
  resolve, mute or change anything.
- **Nothing to install.** One Python file, standard library only (Python 3.9 or newer).
- **HTTPS.** It refuses plain `http://` except to `localhost` (set `CHANGEWATCH_ALLOW_INSECURE=1` to override),
  and never follows a redirect with the key.

## Tools

| Tool | What it returns |
|---|---|
| `get_release_verdict` | `safe` / `watch` / `rollback` / `no_data` for a commit, with the reasons |
| `list_incidents`, `get_incident` | Incidents: file and line, whether that line changed, owners, the AI investigation, the full timeline |
| `get_postmortem` | The blameless postmortem draft (Markdown) for an incident |
| `get_deploy_review` | The risk review of a deploy's diff: risk, findings with file and line, tests to add |
| `list_deploys` | Recent deploys and their verdicts (without the diffs) |
| `list_issues`, `get_issue` | Error groups with counts and recent occurrences (secrets removed) |
| `get_status` | Whether ChangeWatch is healthy and the app's data is arriving |

## Claude Code

```bash
claude mcp add changewatch --env CHANGEWATCH_URL=https://changewatch.example.com --env CHANGEWATCH_READ_KEY=cw_read_... -- python3 /path/to/changewatch/mcp-server/changewatch_mcp.py
```

## Claude Desktop

Add this to `claude_desktop_config.json` (Settings → Developer → Edit Config), then restart the app:

```json
{
  "mcpServers": {
    "changewatch": {
      "command": "python3",
      "args": ["/path/to/changewatch/mcp-server/changewatch_mcp.py"],
      "env": {
        "CHANGEWATCH_URL": "https://changewatch.example.com",
        "CHANGEWATCH_READ_KEY": "cw_read_..."
      }
    }
  }
}
```

The read key gives the assistant the same view as the dashboard for that project, including the
code around failing lines and the AI investigation results. Use a project's read key, never an
ingest or admin key.
