# Incident IQ MCP Server

An [MCP](https://modelcontextprotocol.io) server for [Incident IQ](https://www.incidentiq.com/)
that exposes a help-desk agent's core ticket operations as tools: list your
queue, read a ticket and its full activity thread, self-assign, comment, and
resolve — all against your own IIQ tenant via its REST API.

Works with any Incident IQ tenant and any MCP-compatible client.

## Tools

| Tool | Type | Description |
|------|------|-------------|
| `list_my_tickets` | read | Open tickets assigned to you + unassigned tickets on your team queues |
| `get_ticket` | read | One ticket's key fields |
| `get_ticket_thread` | read | A ticket plus its full activity thread (comments + internal notes) |
| `search_tickets` | read | Substring search across your queue |
| `assign_to_me` | write | Self-assign a ticket (`POST /tickets/{id}/assign`) |
| `post_comment` | write | Post a public or internal comment, no status change |
| `resolve_ticket` | write | Assign → comment → resolution action → status to closed/resolved |

Write tools are side-effecting and documented as such. The server logs only to
stderr and never prints ticket contents.

## Requirements

- **Python 3.10+** (the `mcp` 2.x SDK requires it).
- An Incident IQ **API token**, plus your tenant's **SiteId** and **ProductId**.
  These come from your IIQ tenant's admin / Developer Tools — ask your IIQ
  administrator if you don't have API access.

## Install

```bash
git clone https://github.com/DubdPC/iiq-mcp-server.git
cd iiq-mcp-server

# Python 3.10+ virtual environment
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Configure

```bash
cp .env.example .env
# edit .env and fill in your tenant's values
```

Required keys: `IIQ_BASE_URL`, `IIQ_SITE_ID`, `IIQ_PRODUCT_ID`, `IIQ_API_TOKEN`,
`USER_EMAIL`, `IIQ_TEAM_IDS`. Optional keys (agent UUID, resolution-action ID,
closed-status ID, a custom env path) are documented in `.env.example`.

Smoke-test it — it should sit quietly on stdio (Ctrl-C to stop):

```bash
.venv/bin/python server.py
```

## Register with your MCP client

The server speaks MCP over **stdio**, so register it with whatever MCP client
you use, following that client's documentation for adding a local/stdio server.
Most clients take three things — the command, its arguments, and a working
directory. Use **absolute paths**, and point the command at the Python 3.10+
interpreter inside your venv. A typical configuration looks like:

```json
{
  "mcpServers": {
    "incident-iq": {
      "command": "/absolute/path/to/iiq-mcp-server/.venv/bin/python",
      "args": ["/absolute/path/to/iiq-mcp-server/server.py"],
      "cwd": "/absolute/path/to/iiq-mcp-server"
    }
  }
}
```

Restart (or reload) your client so it picks up the new server.

## Usage

Once registered, ask your assistant in plain language — it will call the tools.
Start with a read before any write:

```
"List my open Incident IQ tickets."
      -> list_my_tickets

"Show me the full thread for ticket T-12345."
      -> get_ticket_thread(number_or_id="T-12345")

"Search my queue for anything about printers."
      -> search_tickets(query="printer")

"Assign T-12345 to me."
      -> assign_to_me(number_or_id="T-12345")

"Reply on T-12345: 'Rebooting the switch now, back shortly.'"
      -> post_comment(number_or_id="T-12345", message="Rebooting the switch now, back shortly.")

"Resolve T-12345 with the message 'Replaced the cable — back online. Closing this out.'"
      -> resolve_ticket(number_or_id="T-12345",
                        message="Replaced the cable — back online. Closing this out.")
```

`resolve_ticket` runs the full close in order (assign → comment → resolution
action → status), and stops and reports if any step fails so nothing is left
half-done.

## Security

- **Never commit `.env`** — it holds your API token. It's already gitignored.
- Ticket data can contain staff and student PII. `my_tickets.json` and the
  `.cache/` directory are gitignored; keep it that way.
- Credentials are read only from `.env`; they are never accepted as tool
  arguments or written to logs.

## Troubleshooting

- **`ModuleNotFoundError` / import errors on `mcp`** — you're likely on Python
  3.9 or earlier. The `mcp` 2.x SDK needs 3.10+. Rebuild the venv with a newer
  interpreter.
- **`check_config` error about missing env vars** — a required key in `.env` is
  blank or still a placeholder. Fill in all the Required keys.
- **Closing a ticket fails at the status step** — set `IIQ_CLOSED_STATUS_ID` in
  `.env`, or let the server discover it (it needs at least one already-closed
  ticket to learn the UUID). If your tenant enforces a resolution action, set
  `IIQ_RESOLUTION_ACTION_ID` too.

## License

MIT — see [LICENSE](LICENSE).
