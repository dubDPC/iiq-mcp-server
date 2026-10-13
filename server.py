#!/usr/bin/env python3
"""
Incident IQ (IIQ) MCP server
============================

Exposes core Incident IQ help-desk operations as Model Context Protocol
tools, so Claude can read the queue, inspect a ticket and its thread,
self-assign, comment, and resolve tickets directly. It works against ANY
Incident IQ tenant -- everything tenant-specific comes from .env.

SDK: mcp 2.x (MCPServer API). Transport: stdio (for Claude Desktop / Cowork).
Credentials are read from a .env in this project's own directory (override
with IIQ_ENV_PATH) -- never passed as tool arguments. All diagnostic logging
goes to stderr; stdout is reserved for the MCP protocol.

The mcp 2.x SDK requires Python >= 3.10, so run this under a 3.10+ venv
(see README). Run it where it can reach your tenant's incidentiq.com host.

Read tools:   list_my_tickets, get_ticket, get_ticket_thread, search_tickets
Write tools:  post_comment, assign_to_me, resolve_ticket   (side-effecting)
"""

import logging
import sys

from mcp.server.mcpserver import MCPServer

import iiq_client as iiq

# stderr-only logging; stdout belongs to the MCP stdio protocol.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("iiq_mcp.server")

mcp = MCPServer("incident-iq")


def _require_config() -> str:
    """Return an error string if credentials are missing, else ''."""
    try:
        iiq.check_config()
        return ""
    except iiq.IIQConfigError as e:
        return str(e)


# --------------------------------------------------------------------------- #
#  READ TOOLS
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_my_tickets(me_only: bool = False, team_only: bool = False, limit: int = 50) -> dict:
    """[READ] List OPEN tickets that are either assigned to me, or sitting
    unassigned on one of my team queues (the team triage pool). Mirrors
    pull_my_tickets.py's filters.

    Args:
        me_only: only tickets assigned to me (skip the team pool).
        team_only: only the unassigned team-queue tickets (skip mine).
        limit: cap the number of tickets returned (0 = no cap).

    Returns a compact summary plus a `tickets` list of slim ticket dicts
    (no activity threads -- use get_ticket_thread for those).
    """
    err = _require_config()
    if err:
        return {"error": err}

    me_tickets, team_tickets, seen = [], [], set()
    scanned = 0
    try:
        for raw in iiq.fetch_all_pages():
            scanned += 1
            tid = raw.get("TicketId")
            if tid in seen:
                continue
            seen.add(tid)
            if raw.get("IsClosed", False):
                continue
            mine = iiq.is_assigned_to_me(raw) and not team_only
            team = iiq.is_assigned_to_my_team(raw) and not me_only
            if mine:
                me_tickets.append(raw)
            elif team:
                team_tickets.append(raw)
    except Exception as e:  # noqa: BLE001 - surface as a tool error, not a crash
        log.exception("list_my_tickets scan failed")
        return {"error": f"ticket scan failed: {type(e).__name__}: {e}"}

    matched = me_tickets + team_tickets
    if limit and limit > 0:
        matched = matched[:limit]

    out = []
    for raw in matched:
        shaped = iiq.shape_ticket(raw)
        shaped["bucket"] = "me" if iiq.is_assigned_to_me(raw) else "team"
        out.append(shaped)

    return {
        "scanned": scanned,
        "counts": {
            "me": sum(1 for t in out if t["bucket"] == "me"),
            "team": sum(1 for t in out if t["bucket"] == "team"),
            "returned": len(out),
            "matched_total": len(me_tickets) + len(team_tickets),
        },
        "tickets": out,
    }


@mcp.tool()
def get_ticket(number_or_id: str) -> dict:
    """[READ] Fetch one ticket by its ticket number (e.g. "14558") or its
    TicketId UUID. Returns the slim shaped ticket (subject, status, requester,
    description, etc.) without the activity thread.
    """
    err = _require_config()
    if err:
        return {"error": err}
    try:
        tid = iiq.resolve_ticket_id(number_or_id)
        if not tid:
            return {"error": f"Ticket '{number_or_id}' not found."}
        raw = iiq.fetch_full(tid)
        if not raw:
            return {"error": f"Ticket '{number_or_id}' ({tid}) returned no data."}
        return {"ticket": iiq.shape_ticket(raw)}
    except Exception as e:  # noqa: BLE001
        log.exception("get_ticket failed")
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def get_ticket_thread(number_or_id: str) -> dict:
    """[READ] Fetch a ticket plus its full activity thread -- public comments
    and human internal notes in chronological order, with attachment filenames
    and work-log entries listed separately. Read this before drafting a reply.

    Activities are classified by shape, not TicketActivityTypeId (type 6 covers
    both comments and attachments on this tenant).
    """
    err = _require_config()
    if err:
        return {"error": err}
    try:
        tid = iiq.resolve_ticket_id(number_or_id)
        if not tid:
            return {"error": f"Ticket '{number_or_id}' not found."}
        raw = iiq.fetch_full(tid)
        if not raw:
            return {"error": f"Ticket '{number_or_id}' ({tid}) returned no data."}
        shaped = iiq.shape_ticket(raw)
        activity = iiq.fetch_activities(tid)
        comments = [a for a in activity if a["kind"] == "comment"]
        notes = [a for a in activity if a["kind"] == "note"]
        attachments = [a for a in activity if a["kind"] == "attachment"]
        worklogs = [a for a in activity if a["kind"] == "worklog"]
        shaped["thread"] = [a for a in activity if a["kind"] in ("comment", "note")]
        shaped["comment_count"] = len(comments)
        shaped["note_count"] = len(notes)
        shaped["attachments"] = attachments
        shaped["attachment_count"] = len(attachments)
        shaped["worklogs"] = worklogs
        return {"ticket": shaped}
    except Exception as e:  # noqa: BLE001
        log.exception("get_ticket_thread failed")
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def search_tickets(query: str, include_closed: bool = False, limit: int = 25) -> dict:
    """[READ] Scan the ticket queue and return tickets whose number, subject,
    requester, description, category or assignee contains the query string
    (case-insensitive substring match). Open tickets only unless
    include_closed is true.

    Note: this scans pages client-side (the IIQ API ignores unknown server-side
    filters on this tenant), so a broad query over a large queue is slower than
    list_my_tickets.
    """
    err = _require_config()
    if err:
        return {"error": err}
    q = (query or "").strip().lower()
    if not q:
        return {"error": "Provide a non-empty query."}
    results, seen = [], set()
    try:
        for raw in iiq.fetch_all_pages():
            tid = raw.get("TicketId")
            if tid in seen:
                continue
            seen.add(tid)
            if raw.get("IsClosed", False) and not include_closed:
                continue
            shaped = iiq.shape_ticket(raw)
            haystack = " ".join(
                str(shaped.get(f, "") or "")
                for f in (
                    "number", "subject", "requester", "requester_email",
                    "description", "category", "issue", "assigned_to",
                    "location", "status",
                )
            ).lower()
            if q in haystack:
                results.append(shaped)
                if limit and len(results) >= limit:
                    break
    except Exception as e:  # noqa: BLE001
        log.exception("search_tickets failed")
        return {"error": f"ticket scan failed: {type(e).__name__}: {e}"}
    return {"query": query, "count": len(results), "tickets": results}


# --------------------------------------------------------------------------- #
#  WRITE TOOLS  (side-effecting -- these change the ticket in IIQ)
# --------------------------------------------------------------------------- #
@mcp.tool()
def assign_to_me(number_or_id: str) -> dict:
    """[WRITE / side-effecting] Self-assign a ticket -- the API behind the UI's
    "Assign to me" button. Needed before closing a team-queue ticket so it has
    an owner on record. Tries several body shapes and verifies the assignment
    actually stuck by re-reading the ticket.
    """
    err = _require_config()
    if err:
        return {"error": err}
    try:
        tid = iiq.resolve_ticket_id(number_or_id)
        if not tid:
            return {"error": f"Ticket '{number_or_id}' not found."}
        result = iiq.assign_ticket_to_me(tid)
        result["ticket_id"] = tid
        return result
    except Exception as e:  # noqa: BLE001
        log.exception("assign_to_me failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def post_comment(number_or_id: str, message: str, internal: bool = False) -> dict:
    """[WRITE / side-effecting] Post a comment to a ticket via
    /tickets/{id}/activities/new (TicketActivityComment, type 6). This does NOT
    change the ticket status -- the ticket stays open.

    Args:
        number_or_id: ticket number or TicketId UUID.
        message: the comment body.
        internal: True posts a private/internal comment (not visible to the
            requester); default False posts a public reply.
    """
    err = _require_config()
    if err:
        return {"error": err}
    if not (message or "").strip():
        return {"error": "message is empty."}
    try:
        tid = iiq.resolve_ticket_id(number_or_id)
        if not tid:
            return {"error": f"Ticket '{number_or_id}' not found."}
        result = iiq.post_comment(tid, message, is_public=not internal)
        result["ticket_id"] = tid
        result["internal"] = bool(internal)
        return result
    except Exception as e:  # noqa: BLE001
        log.exception("post_comment failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def resolve_ticket(
    number_or_id: str,
    message: str,
    resolution_notes: str = "",
    internal: bool = False,
    assign_first: bool = True,
) -> dict:
    """[WRITE / side-effecting] Close a ticket end-to-end, reusing
    close_ticket.py's exact sequence:
        1. (optional) self-assign so the closed ticket has an owner,
        2. post the closing comment (type 6),
        3. post the required resolution action (type 8, "Other" by default),
        4. change status to the tenant's closed/resolved step.

    Stops and reports if any step fails so nothing is left half-done silently
    (a posted comment with a failed close is reported, not hidden).

    Args:
        number_or_id: ticket number or TicketId UUID.
        message: the closing comment sent to the requester.
        resolution_notes: notes attached to the resolution action (optional).
        internal: post the closing comment as internal/private (default public).
        assign_first: self-assign before closing (default True; recommended
            for team-queue tickets).
    """
    import os

    err = _require_config()
    if err:
        return {"error": err}
    if not (message or "").strip():
        return {"error": "message is empty; a closing comment is required."}

    steps = {}
    try:
        tid = iiq.resolve_ticket_id(number_or_id)
        if not tid:
            return {"error": f"Ticket '{number_or_id}' not found."}

        if assign_first:
            steps["assign"] = iiq.assign_ticket_to_me(tid)
            if not steps["assign"].get("ok"):
                return {
                    "ok": False,
                    "ticket_id": tid,
                    "stopped_at": "assign",
                    "steps": steps,
                    "detail": "Self-assign failed; did not comment or close. "
                    "Assign in the UI and retry, or call resolve_ticket with "
                    "assign_first=False.",
                }

        steps["comment"] = iiq.post_comment(tid, message, is_public=not internal)
        if not steps["comment"].get("ok"):
            return {
                "ok": False,
                "ticket_id": tid,
                "stopped_at": "comment",
                "steps": steps,
                "detail": "Comment failed; status not changed.",
            }

        resolution_id = os.environ.get("IIQ_RESOLUTION_ACTION_ID", "").strip()
        if not resolution_id:
            # No tenant resolution-action UUID configured. Many tenants do not
            # enforce a resolution action, so skip the step rather than guess a
            # UUID. If your tenant requires one, set IIQ_RESOLUTION_ACTION_ID in
            # .env and the status change below will succeed.
            steps["resolution_action"] = {
                "ok": True,
                "skipped": True,
                "detail": "IIQ_RESOLUTION_ACTION_ID not set; resolution-action "
                "step skipped. Set it in .env if your tenant requires it.",
            }
        else:
            steps["resolution_action"] = iiq.post_resolution_action(
                tid, resolution_id, resolution_notes
            )
        if not steps["resolution_action"].get("ok"):
            return {
                "ok": False,
                "ticket_id": tid,
                "stopped_at": "resolution_action",
                "steps": steps,
                "detail": "Resolution action failed; status change would likely "
                "fail too. Comment was posted. Finish in the IIQ UI.",
            }

        status_id, status_name = iiq.discover_closed_status_id()
        if not status_id:
            return {
                "ok": False,
                "ticket_id": tid,
                "stopped_at": "status_discovery",
                "steps": steps,
                "detail": "Could not determine the closed-status UUID. Set "
                "IIQ_CLOSED_STATUS_ID in .env, or close in the UI. Comment and "
                "resolution action were posted.",
            }
        steps["status"] = iiq.change_status(tid, status_id)
        steps["status"]["status_name"] = status_name
        if not steps["status"].get("ok"):
            return {
                "ok": False,
                "ticket_id": tid,
                "stopped_at": "status",
                "steps": steps,
                "detail": f"Comment and resolution posted but status change to "
                f"'{status_name}' failed. Close in the UI.",
            }

        return {
            "ok": True,
            "ticket_id": tid,
            "closed_as": status_name,
            "steps": steps,
            "detail": f"Ticket resolved (status '{status_name}').",
        }
    except Exception as e:  # noqa: BLE001
        log.exception("resolve_ticket failed")
        return {"ok": False, "steps": steps, "error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    try:
        mcp.run()
    except KeyboardInterrupt:
        # Clean shutdown on Ctrl-C (e.g. during a manual smoke test).
        import sys
        print("incident-iq MCP server stopped.", file=sys.stderr)
        sys.exit(0)
