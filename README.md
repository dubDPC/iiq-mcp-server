# Incident IQ MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that
turns common [Incident IQ](https://www.incidentiq.com/) help-desk operations
into tools you can call from Claude (Claude Desktop / Cowork). With it, Claude
can read your ticket queue, open a ticket and its full conversation thread,
self-assign, post comments, and resolve tickets — all against your own IIQ
tenant, using your own API token.

**Works with any Incident IQ tenant.** Nothing about a specific district is
baked in — every tenant-specific value (base URL, site/product IDs, token,
team IDs, resolution/closed-status IDs) is read from your `.env`.

## Tools

### Read (no side effects)
| Tool | What it does |
|---|---|
| `list_my_tickets(me_only=False, team_only=False, limit=50)` | Open tickets assigned to you, plus unassigned tickets sitting on your team queues. |
| `get_ticket(number_or_id)` | One ticket by number (e.g. `"14558"`) or TicketId UUID — summary fields, no thread. |
| `get_ticket_thread(number_or_id)` | The ticket plus its full activity thread (comments + internal notes, chronological), with attachments and work logs listed. Read this before drafting a reply. |
| `search_tickets(query, include_closed=False, limit=25)` | Case-insensitive substring scan over number/subject/requester/description/category/assignee. |

### Write (side-effecting — these change the ticket in IIQ)
| Tool | What it does |
|---|---|
| `assign_to_me(number_or_id)` | Self-assign (the UI "Assign to me" action). Tries a few request-body shapes and verifies the assignment actually stuck. |
| `post_comment(number_or_id, message, internal=False)` | Post a comment. Does **not** change status. `internal=True` posts a private/internal note. |
| `resolve_ticket(number_or_id, message, resolution_notes="", internal=False, assign_first=True)` | Full close: assign → comment → (optional) resolution action → status → closed/resolved. Stops and reports if any step fails, so nothing is left half-done silently. |

## Prerequisites

- **Python 3.10 or newer.** The `mcp` 2.x SDK requires it; Python 3.9 will not work.
- **An Incident IQ API token**, plus your tenant's **SiteId** and **ProductId**.
  These come from your Incident IQ tenant's admin / Developer Tools area. If you
  don't have them, ask your IIQ administrator — issuing API tokens is an admin
  function. (Exact menu paths vary by IIQ version, so check your tenant's admin
  / Developer settings rather than following a fixed click-path.)
- Your tenant's base URL, i.e. `https://<yourdistrict>.incidentiq.com`.

## Install

```bash
git clone <your-fork-or-repo-url> iiq-mcp-server
cd iiq-mcp-server

# Create a Python 3.10+ virtual environment (use whatever 3.10+ you have)
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Configure

```bash
cp .env.example .env
# then edit .env and fill in your tenant's values
```

Required keys: `IIQ_BASE_URL`, `IIQ_SITE_ID`, `IIQ_PRODUCT_ID`, `IIQ_API_TOKEN`,
`USER_EMAIL`, `IIQ_TEAM_IDS`. Optional: `IIQ_MY_USER_ID` / `IIQ_AGENT_ID`,
`IIQ_RESOLUTION_ACTION_ID`, `IIQ_CLOSED_STATUS_ID`, `IIQ_ENV_PATH`. Every key is
documented inline in `.env.example`.

The server reads `.env` from its own directory by default; set `IIQ_ENV_PATH`
to load it from somewhere else.

### Quick smoke test

```bash
.venv/bin/python3 server.py
```

It will sit quietly waiting on stdio — that's correct (MCP stdio servers talk
over stdin/stdout, not a port). Press **Ctrl-C** to stop; it exits cleanly.
All logs go to **stderr** only.

## Register in Claude Desktop

Add this to your Claude Desktop config
(`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS),
then restart Claude. Use **absolute paths**, and point `command` at the
**3.10+** interpreter inside the venv you just made:

```json
{
  "mcpServers": {
    "incident-iq": {
      "command": "/absolute/path/to/iiq-mcp-server/.venv/bin/python3",
      "args": ["/absolute/path/to/iiq-mcp-server/server.py"],
      "cwd": "/absolute/path/to/iiq-mcp-server"
    }
  }
}
```

> **Python version matters here.** `mcp` 2.x needs Python 3.10+, so `command`
> must be a 3.10+ interpreter. If you point it at a 3.9 Python, the server will
> fail to import `mcp`.

## Security

- **Never commit `.env`.** It contains your API token. It is listed in
  `.gitignore`; keep it there. The `.env.example` in the repo holds placeholders
  only.
- **Ticket data is sensitive.** Tickets routinely contain staff and student
  names, emails, and ID numbers (PII). Any local `my_tickets.json` is
  gitignored. Don't paste ticket contents into places that leave your
  organization.
- **The server never prints ticket contents.** It logs only to stderr, and only
  minimal metadata — never ticket bodies or secrets. (Keeping stdout clean is
  also required by the MCP stdio protocol.)
- Ticket fields are passed through to the MCP host as-is for display; the server
  adds no redaction of its own.

## Troubleshooting

- **`ModuleNotFoundError: No module named 'mcp.server.fastmcp'`** — you're on
  `mcp` 2.x (which this server targets) but running old code, or vice-versa.
  This server uses the 2.x `MCPServer` API. `pip install mcp` pulls 2.x, which
  requires the `MCPServer` entry point used here.
- **The server won't start / `mcp` won't import** — check your Python version
  (`.venv/bin/python3 --version`). It must be **3.10+**. Python 3.9 cannot run
  `mcp` 2.x. Recreate the venv with a newer interpreter.
- **"IIQ_BASE_URL is still the placeholder"** — you haven't edited `.env`; set
  `IIQ_BASE_URL` to your real tenant URL.
- **Closing a ticket fails at the status step** — your tenant may require a
  resolution action. Set `IIQ_RESOLUTION_ACTION_ID` in `.env` to your tenant's
  action UUID. Likewise set `IIQ_CLOSED_STATUS_ID` if closed-status
  auto-discovery can't find it.

## License

MIT — see [LICENSE](LICENSE). Fill in your name/org in the copyright line.
