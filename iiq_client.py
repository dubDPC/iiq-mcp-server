#!/usr/bin/env python3
"""
IIQ API client for the MCP server.

A thin wrapper around the Incident IQ REST API that holds the auth headers
and the exact request/payload shapes proven in production against a live
Incident IQ tenant. Nothing here is experimental protocol -- the calls are
factored out so server.py can stay small. It works against ANY Incident IQ
tenant; everything tenant-specific comes from .env.

Credentials are read from a .env in this project's OWN directory (override
with the IIQ_ENV_PATH env var) via python-dotenv. No token or secret is ever
returned, logged, or printed. Logging goes to stderr only (an MCP stdio
server must keep stdout clean for the protocol).
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

# This project is self-contained: config and caches live in THIS directory,
# not a parent repo. The .env location can be overridden with IIQ_ENV_PATH.
PROJECT_DIR = Path(__file__).resolve().parent
ENV_PATH = Path(os.environ.get("IIQ_ENV_PATH") or (PROJECT_DIR / ".env"))
if load_dotenv is not None:
    load_dotenv(ENV_PATH)

log = logging.getLogger("iiq_mcp.client")

# --- config, same variable names as the existing scripts --------------------
IIQ_BASE_URL = os.environ.get("IIQ_BASE_URL", "https://YOURDISTRICT.incidentiq.com/api/v1.0")
# Web (UI) base for building human ticket links, derived from the API base.
IIQ_WEB_BASE = (
    IIQ_BASE_URL.split("/api/")[0] if "/api/" in IIQ_BASE_URL else IIQ_BASE_URL.rstrip("/")
)
IIQ_SITE_ID = os.environ.get("IIQ_SITE_ID", "")
IIQ_PRODUCT_ID = os.environ.get("IIQ_PRODUCT_ID", "")
IIQ_API_TOKEN = os.environ.get("IIQ_API_TOKEN", "")

IIQ_TEAM_ID = os.environ.get("IIQ_TEAM_ID", "")  # legacy single
_team_ids_extra = os.environ.get("IIQ_TEAM_IDS", "")
IIQ_TEAM_IDS = {
    tid.strip()
    for tid in ([IIQ_TEAM_ID] + _team_ids_extra.split(","))
    if tid and tid.strip()
}

USER_EMAIL = os.environ.get("USER_EMAIL", "").lower()
USER_NAME = os.environ.get("USER_NAME", "").lower()

# Caches live in this project's own ./.cache/ dir (created on demand). They
# hold only discovered UUIDs/labels -- no secrets, no ticket data.
CACHE_DIR = PROJECT_DIR / ".cache"
AGENT_CACHE = CACHE_DIR / "agent.json"
ASSIGN_CACHE = CACHE_DIR / "assign_body.json"
WORKFLOW_CACHE = CACHE_DIR / "workflow.json"
# Optional fast path ONLY: a my_tickets.json produced by some other tool. It
# is never required -- number->id resolution falls through to the live API
# when this file is absent.
TICKETS_JSON = PROJECT_DIR / "my_tickets.json"

# Confirmed endpoints (validated live against an Incident IQ tenant).
ASSIGN_ENDPOINT = "/tickets/{tid}/assign"

# The resolution action (type 8) is required before closing a ticket only on
# tenants that enforce it, and the valid ResolutionActionId is tenant-specific.
# There is deliberately NO baked-in default: set IIQ_RESOLUTION_ACTION_ID in
# .env to your tenant's action UUID. When it is unset, resolve_ticket skips the
# resolution-action step rather than guessing a UUID.
RESOLUTION_ACTION_ID = os.environ.get("IIQ_RESOLUTION_ACTION_ID", "").strip()

ZERO_UUID = "00000000-0000-0000-0000-000000000000"


class IIQConfigError(RuntimeError):
    """Raised when required credentials are missing from .env."""


def check_config() -> None:
    missing = [
        k
        for k in ("IIQ_API_TOKEN", "IIQ_SITE_ID", "IIQ_PRODUCT_ID", "IIQ_BASE_URL", "USER_EMAIL")
        if not os.environ.get(k)
    ]
    if missing:
        raise IIQConfigError(
            "Missing required env vars: " + ", ".join(missing)
            + ". Copy .env.example to .env and fill it in."
        )
    if "YOURDISTRICT" in os.environ.get("IIQ_BASE_URL", ""):
        raise IIQConfigError(
            "IIQ_BASE_URL is still the placeholder. Set it to your tenant's API "
            "base, e.g. https://<district>.incidentiq.com/api/v1.0"
        )


def headers() -> dict:
    """The proven IIQ auth header shape (close_ticket.py / pull_my_tickets.py)."""
    return {
        "Authorization": f"Bearer {IIQ_API_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Client": "ApiClient",
        "SiteId": IIQ_SITE_ID,
        "ProductId": IIQ_PRODUCT_ID,
    }


# --------------------------------------------------------------------------- #
#  Low-level HTTP
# --------------------------------------------------------------------------- #
def _get(path: str, **kwargs):
    return requests.get(f"{IIQ_BASE_URL}{path}", headers=headers(), timeout=30, **kwargs)


def _post(path: str, **kwargs):
    return requests.post(f"{IIQ_BASE_URL}{path}", headers=headers(), timeout=30, **kwargs)


# --------------------------------------------------------------------------- #
#  Ticket fetching / shaping  (from pull_my_tickets.py)
# --------------------------------------------------------------------------- #
def fetch_all_pages(max_pages: int = 100, page_size: int = 200):
    """Page through every ticket; yield raw ticket dicts. POST /tickets."""
    page = 0
    while page < max_pages:
        resp = _post(
            "/tickets",
            json={"OnlyShowDeletedTickets": False},
            params={"$s": page_size, "$p": page, "$o": "TicketId"},
        )
        resp.raise_for_status()
        items = resp.json().get("Items", [])
        if not items:
            break
        for it in items:
            yield it
        page += 1


def fetch_full(ticket_id: str) -> dict:
    """GET /tickets/{id} -> the one ticket (payload is {"Item": {...}})."""
    resp = _get(f"/tickets/{ticket_id}")
    resp.raise_for_status()
    d = resp.json()
    return d.get("Item", d)


def is_assigned_to_me(ticket: dict) -> bool:
    user = ticket.get("AssignedToUser", {}) or {}
    email = (user.get("Email", "") or user.get("Username", "") or "").lower()
    name = (user.get("Name", "") or "").lower()
    if USER_EMAIL and email and USER_EMAIL == email:
        return True
    if USER_NAME and name and USER_NAME in name:
        return True
    if USER_EMAIL and name:
        local = USER_EMAIL.split("@")[0].replace(".", " ")
        if local and local in name:
            return True
    return False


def _has_individual_assignee(ticket: dict) -> bool:
    user = ticket.get("AssignedToUser") or {}
    if not isinstance(user, dict):
        user = {}
    uid = ticket.get("AssignedToUserId") or user.get("UserId") or ""
    if not uid or uid == ZERO_UUID:
        return False
    return True


def is_assigned_to_my_team(ticket: dict) -> bool:
    if not IIQ_TEAM_IDS:
        return False
    if ticket.get("AssignedToTeamId") not in IIQ_TEAM_IDS:
        return False
    return not _has_individual_assignee(ticket)


def shape_ticket(t: dict) -> dict:
    """Slim, JSON-friendly view of one ticket (pull_my_tickets.shape_ticket)."""
    workflow = t.get("WorkflowStep", {}) or {}
    issue = t.get("Issue", {}) or {}
    for_user = t.get("For", {}) or {}
    assigned_user = t.get("AssignedToUser", {}) or {}
    assigned_team = t.get("AssignedToTeam", {}) or {}
    location = t.get("Location", {}) or {}
    room = t.get("LocationRoom", {}) or {}

    priority_map = {0: "None", 1: "Low", 2: "Medium", 3: "High", 4: "Critical"}
    priority_val = t.get("Priority", 0)
    ticket_id = t.get("TicketId", "")
    status = workflow.get("StatusName", "Unknown")
    created = t.get("CreatedDate", "")
    modified = t.get("ModifiedDate", "")

    age_days = None
    if created:
        try:
            age_days = (datetime.now() - datetime.fromisoformat(created[:19])).days
        except (ValueError, TypeError):
            pass

    return {
        "id": ticket_id,
        "number": t.get("TicketNumber", ""),
        "subject": t.get("Subject", "") or "(no subject)",
        "status": status,
        "is_closed": t.get("IsClosed", False),
        "priority": priority_map.get(priority_val, str(priority_val)),
        "created": created,
        "modified": modified,
        "age_days": age_days,
        "category": issue.get("IssueCategoryName", ""),
        "issue": issue.get("Name", ""),
        "requester": for_user.get("Name", "Unknown"),
        "requester_email": for_user.get("Email", ""),
        "location": location.get("Name", ""),
        "location_abbr": location.get("Abbreviation", ""),
        "room": room.get("Name", ""),
        "assigned_to": assigned_user.get("Name", "Unassigned"),
        "assigned_to_email": assigned_user.get("Email", ""),
        "assigned_team": assigned_team.get("TeamName", ""),
        "description": t.get("IssueDescription", "") or "",
        "url": f"{IIQ_WEB_BASE}/tickets/{ticket_id}",
    }


# --------------------------------------------------------------------------- #
#  Activity thread  (from pull_my_tickets.py, classified by shape)
# --------------------------------------------------------------------------- #
_COMMENTS_CANDIDATES = [
    ("GET", "/tickets/{ticket_id}/activities"),
    ("POST", "/tickets/{ticket_id}/activities"),
]


def _flatten_activities(outer_items: list):
    for outer in outer_items:
        if not isinstance(outer, dict):
            continue
        inners = outer.get("ActivityItems") or []
        if not isinstance(inners, list):
            continue
        for inner in inners:
            if isinstance(inner, dict):
                yield outer, inner


def _nonempty(inner: dict, *fields) -> str:
    for f in fields:
        v = inner.get(f)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _classify_activity(inner: dict) -> str:
    """Classify a nested activity item by shape, not TicketActivityTypeId.
    (See pull_my_tickets.py: typeId 6 is both comments AND attachments.)"""
    if inner.get("TicketActivityAttachmentId") or inner.get("FileId"):
        return "attachment"
    if inner.get("TicketActivityCommentId") or _nonempty(inner, "Comments", "CommentText"):
        return "comment"
    if (
        inner.get("LaborTypeId")
        or inner.get("ResolutionActionId")
        or inner.get("Effort")
        or inner.get("LaborCost")
    ):
        return "worklog"
    body = _nonempty(inner, "Notes", "Body", "Text", "Content")
    if body:
        return "note"
    return ""


def _shape_activity(outer: dict, inner: dict, kind: str) -> dict:
    owner = outer.get("Owner") or {}
    if not isinstance(owner, dict):
        owner = {}

    if kind == "comment":
        body = _nonempty(inner, "Comments", "CommentText")
    elif kind == "attachment":
        body = ""
    else:
        body = _nonempty(inner, "Notes", "Body", "Text", "Content")

    if kind == "attachment":
        ident = inner.get("TicketActivityAttachmentId") or inner.get("FileId") or ""
    elif kind == "comment":
        ident = inner.get("TicketActivityCommentId") or inner.get("TicketActivityId") or ""
    else:
        ident = (
            inner.get("TicketActivityWorkflowLogId")
            or inner.get("TicketActivityActionId")
            or inner.get("TicketActivityId")
            or ""
        )

    return {
        "kind": kind,
        "filename": inner.get("FileName", "") if kind == "attachment" else "",
        "id": ident or outer.get("TicketActivityId") or "",
        "date": (
            inner.get("CreatedDate")
            or outer.get("CreatedDate")
            or inner.get("ModifiedDate")
            or ""
        ),
        "author": owner.get("Name") or owner.get("FullName") or "",
        "author_email": owner.get("Email") or owner.get("Username") or "",
        "body": body,
        "is_public": bool(outer.get("IsPublic", True)) and bool(inner.get("IsPublic", True)),
    }


def fetch_activities(ticket_id: str) -> list:
    """Fetch the full activity thread for a ticket -- comments, internal notes,
    attachments, work logs -- classified by shape. Probes GET then POST."""
    last_status = None
    for method, path_template in _COMMENTS_CANDIDATES:
        path = path_template.format(ticket_id=ticket_id)
        try:
            if method == "GET":
                resp = _get(path, params={"$s": 500, "$p": 0})
            else:
                resp = _post(path, json={}, params={"$s": 500, "$p": 0})
        except requests.RequestException as e:
            log.warning("activities fetch failed for %s: %s", ticket_id, e)
            continue

        last_status = resp.status_code
        if resp.status_code == 404 or resp.status_code >= 400:
            continue
        try:
            data = resp.json()
        except ValueError:
            continue

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("Items") or data.get("Item") or []
            if isinstance(items, dict):
                items = [items]
        else:
            items = []

        shaped, seen = [], set()
        for outer, inner in _flatten_activities(items):
            kind = _classify_activity(inner)
            if not kind:
                continue
            entry = _shape_activity(outer, inner, kind)
            key = (entry["kind"], entry["id"])
            if entry["id"] and key in seen:
                continue
            seen.add(key)
            shaped.append(entry)
        shaped.sort(key=lambda e: e.get("date") or "")
        return shaped

    log.info("no working activities endpoint (last status %s)", last_status)
    return []


# --------------------------------------------------------------------------- #
#  Ticket-number -> id resolution  (close_ticket.find_ticket_id, plus live)
# --------------------------------------------------------------------------- #
def _looks_like_uuid(value: str) -> bool:
    v = (value or "").strip()
    return len(v) == 36 and v.count("-") == 4


def find_ticket_id_cached(number: str) -> tuple:
    """Look up TicketId from my_tickets.json by ticket number.
    Returns (ticket_id, ticket_dict) or ("", {})."""
    if not TICKETS_JSON.exists():
        return "", {}
    try:
        data = json.loads(TICKETS_JSON.read_text())
    except (json.JSONDecodeError, OSError):
        return "", {}
    for t in data.get("tickets", []):
        if str(t.get("number")) == str(number):
            return t.get("id", ""), t
    return "", {}


def resolve_ticket_id(number_or_id: str) -> str:
    """Resolve a ticket NUMBER (e.g. 14558) or UUID to a TicketId.

    1. If it already looks like a UUID, use it directly.
    2. Otherwise check my_tickets.json (close_ticket.find_ticket_id pattern).
    3. Otherwise scan the live ticket list (POST /tickets paged) for a
       matching TicketNumber. Returns "" if not found.
    """
    val = str(number_or_id).strip()
    if _looks_like_uuid(val):
        return val
    cached, _ = find_ticket_id_cached(val)
    if cached:
        return cached
    for raw in fetch_all_pages():
        if str(raw.get("TicketNumber", "")) == val:
            return raw.get("TicketId", "")
    return ""


# --------------------------------------------------------------------------- #
#  Agent (me) discovery  (close_ticket.discover_my_agent_id)
# --------------------------------------------------------------------------- #
def _cache_agent_id(agent_id: str, email: str) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        AGENT_CACHE.write_text(json.dumps({"agent_id": agent_id, "email": email}, indent=2))
    except OSError:
        pass


def discover_my_agent_id() -> str:
    """Find the current user's AgentId (UUID).

    Order: IIQ_AGENT_ID / IIQ_MY_USER_ID env -> agent cache -> GET /users/me
    -> GET /users filtered by USER_EMAIL. Returns "" on failure."""
    explicit = (
        os.environ.get("IIQ_AGENT_ID", "").strip()
        or os.environ.get("IIQ_MY_USER_ID", "").strip()
    )
    if explicit:
        return explicit

    if AGENT_CACHE.exists():
        try:
            cache = json.loads(AGENT_CACHE.read_text())
            if cache.get("agent_id"):
                return cache["agent_id"]
        except (json.JSONDecodeError, OSError):
            pass

    try:
        resp = _get("/users/me")
        if resp.status_code == 200:
            data = resp.json()
            uid = data.get("UserId") or data.get("Id") or ""
            if uid and uid != ZERO_UUID:
                _cache_agent_id(uid, data.get("Email", ""))
                return uid
    except requests.RequestException:
        pass

    email = os.environ.get("USER_EMAIL", "")
    if not email:
        return ""
    try:
        resp = _get(
            "/users",
            params={"$filter": f"Email eq '{email}'", "$s": 10, "$p": 0},
        )
        if resp.status_code == 200:
            body = resp.json()
            items = body.get("Items") or ([body] if isinstance(body, dict) else [])
            for item in items:
                if (item.get("Email") or "").lower() == email.lower():
                    uid = item.get("UserId") or item.get("Id") or ""
                    if uid and uid != ZERO_UUID:
                        _cache_agent_id(uid, email)
                        return uid
    except requests.RequestException:
        pass

    return ""


def _ticket_assignee(ticket_id: str) -> str:
    """Current AssignedToUserId (lowercased) for a ticket, or ''."""
    try:
        resp = _get(f"/tickets/{ticket_id}")
        if resp.status_code != 200:
            return ""
        t = resp.json()
        t = t.get("Item", t)
        u = t.get("AssignedToUser") or {}
        return (t.get("AssignedToUserId") or u.get("UserId") or "").lower()
    except requests.RequestException:
        return ""


# --------------------------------------------------------------------------- #
#  Write operations  (close_ticket.py, verbatim payload shapes)
# --------------------------------------------------------------------------- #
def assign_ticket_to_me(ticket_id: str) -> dict:
    """Self-assign the ticket. POST /tickets/{id}/assign, trying a few body
    shapes and VERIFYING each 2xx actually stuck (re-GET). Caches the winner.
    Returns {"ok": bool, "detail": str}."""
    agent_id = discover_my_agent_id()
    if not agent_id:
        return {
            "ok": False,
            "detail": "Could not determine your agent UUID. Set IIQ_AGENT_ID "
            "or IIQ_MY_USER_ID in .env.",
        }
    agent_lc = agent_id.lower()

    if _ticket_assignee(ticket_id) == agent_lc:
        return {"ok": True, "detail": "Already assigned to you."}

    path = ASSIGN_ENDPOINT.format(tid=ticket_id)
    body_candidates = [
        ("empty", {}),
        ("UserId", {"UserId": agent_id}),
        ("Users", {"Users": [{"UserId": agent_id}]}),
        ("UserIds", {"UserIds": [agent_id]}),
        ("AssignedToUserId", {"AssignedToUserId": agent_id}),
    ]
    if ASSIGN_CACHE.exists():
        try:
            lbl = json.loads(ASSIGN_CACHE.read_text()).get("body_label")
            if lbl:
                body_candidates.sort(key=lambda bc: 0 if bc[0] == lbl else 1)
        except (json.JSONDecodeError, OSError):
            pass

    for label, body in body_candidates:
        try:
            resp = _post(path, json=body)
        except requests.RequestException as e:
            log.warning("assign (%s) error: %s", label, e)
            continue
        if not (200 <= resp.status_code < 300):
            continue
        if _ticket_assignee(ticket_id) == agent_lc:
            try:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                ASSIGN_CACHE.write_text(
                    json.dumps({"endpoint": ASSIGN_ENDPOINT, "body_label": label}, indent=2)
                )
            except OSError:
                pass
            return {"ok": True, "detail": f"Assigned to you (body shape: {label})."}

    return {
        "ok": False,
        "detail": "POST /tickets/{id}/assign did not take with any known body "
        "shape. Use the IIQ UI 'Assign to me' button and retry.",
    }


def post_comment(ticket_id: str, message: str, is_public: bool) -> dict:
    """Post a comment via POST /tickets/{id}/activities/new (type 6).
    Returns {"ok": bool, "detail": str}."""
    payload = {
        "TicketId": ticket_id,
        "ActivityItems": [
            {
                "$type": "Spark.Shared.Models.TicketActivityComment, Spark.Shared",
                "TicketActivityTypeId": 6,
                "Comments": message,
                "IsPublic": bool(is_public),
                "WaitForResponse": False,
                "TicketWasUpdated": True,
            }
        ],
        "WaitForResponse": False,
        "TicketWasUpdated": True,
    }
    try:
        resp = _post(f"/tickets/{ticket_id}/activities/new", json=payload)
    except requests.RequestException as e:
        return {"ok": False, "detail": f"request error: {type(e).__name__}"}
    if 200 <= resp.status_code < 300:
        return {"ok": True, "detail": f"Comment posted (HTTP {resp.status_code})."}
    return {"ok": False, "detail": f"HTTP {resp.status_code}: {resp.text[:300]}"}


def post_resolution_action(ticket_id: str, resolution_action_id: str, notes: str) -> dict:
    """Post a TicketActivityAction (type 8) -- required before closing on
    tenants that enforce a resolution action. Returns {"ok", "detail"}."""
    payload = {
        "TicketId": ticket_id,
        "ActivityItems": [
            {
                "$type": "Spark.Shared.Models.TicketActivityAction, Spark.Shared",
                "TicketActivityTypeId": 8,
                "ResolutionActionId": resolution_action_id,
                "Notes": notes or "",
                "IsPublic": True,
            }
        ],
        "WaitForResponse": False,
        "TicketWasUpdated": True,
    }
    try:
        resp = _post(f"/tickets/{ticket_id}/activities/new", json=payload)
    except requests.RequestException as e:
        return {"ok": False, "detail": f"request error: {type(e).__name__}"}
    if 200 <= resp.status_code < 300:
        return {"ok": True, "detail": f"Resolution action posted (HTTP {resp.status_code})."}
    return {"ok": False, "detail": f"HTTP {resp.status_code}: {resp.text[:300]}"}


def discover_closed_status_id() -> tuple:
    """Find the WorkflowStepId for the closed/Resolved status on this tenant.

    Order: IIQ_CLOSED_STATUS_ID env -> workflow cache -> scan tickets ordered
    by TicketId for the first IsClosed=True one and read its WorkflowStep.
    Returns (status_id, status_name) or ("", "")."""
    explicit = os.environ.get("IIQ_CLOSED_STATUS_ID", "").strip()
    if explicit:
        return explicit, "(from IIQ_CLOSED_STATUS_ID)"

    if WORKFLOW_CACHE.exists():
        try:
            cache = json.loads(WORKFLOW_CACHE.read_text())
            if cache.get("closed_status_id"):
                return cache["closed_status_id"], cache.get("status_name", "")
        except (json.JSONDecodeError, OSError):
            pass

    page = 0
    page_size = 200
    seen_steps = {}
    while page < 25:
        try:
            resp = _post(
                "/tickets",
                json={"OnlyShowDeletedTickets": False},
                params={"$s": page_size, "$p": page, "$o": "TicketId"},
            )
            resp.raise_for_status()
            items = resp.json().get("Items", [])
        except requests.RequestException as e:
            log.warning("closed-status discovery error on page %s: %s", page, e)
            return "", ""
        if not items:
            break
        for item in items:
            if not item.get("IsClosed"):
                continue
            step = item.get("WorkflowStep") or {}
            sid = step.get("WorkflowStepId") or step.get("Id")
            sname = (step.get("StatusName") or step.get("Name") or "").strip()
            if sid and sname:
                seen_steps.setdefault(sname, sid)
        if seen_steps:
            break
        page += 1

    if not seen_steps:
        return "", ""

    chosen_name = ""
    # At APS the closed step is named "Resolved"; fall back to Closed-ish.
    for target in ("resolved", "closed"):
        for name in seen_steps:
            if name.lower() == target:
                chosen_name = name
                break
        if chosen_name:
            break
    if not chosen_name:
        for name in seen_steps:
            if name.lower().startswith("closed") or name.lower().startswith("resolved"):
                chosen_name = name
                break
    if not chosen_name:
        chosen_name = next(iter(seen_steps))

    sid = seen_steps[chosen_name]
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        WORKFLOW_CACHE.write_text(
            json.dumps(
                {
                    "closed_status_id": sid,
                    "status_name": chosen_name,
                    "all_closed_steps": seen_steps,
                },
                indent=2,
            )
        )
    except OSError:
        pass
    return sid, chosen_name


def change_status(ticket_id: str, status_id: str) -> dict:
    """POST /tickets/{id}/status/{statusId}. Returns {"ok", "detail"}."""
    try:
        resp = _post(f"/tickets/{ticket_id}/status/{status_id}", json={})
    except requests.RequestException as e:
        return {"ok": False, "detail": f"request error: {type(e).__name__}"}
    if 200 <= resp.status_code < 300:
        return {"ok": True, "detail": f"Status changed (HTTP {resp.status_code})."}
    return {"ok": False, "detail": f"HTTP {resp.status_code}: {resp.text[:300]}"}
