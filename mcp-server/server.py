#!/usr/bin/env python3
"""
Google Tag Manager MCP Server — list/inspect/mutate tags, triggers, variables,
and publish container versions across all GTM accounts visible to the OAuth user.

Reuses the existing hybrid-conversions GTM OAuth setup:
  Token:          ~/Developer/hybrid-conversions/hybrid_gtm_token.json
  Client secrets: ~/Developer/hybrid_gtm_client_secrets.json

Runs as a local stdio MCP server.

Usage:
    ~/Developer/gtm/.venv/bin/python3 ~/Developer/gtm/mcp-server/server.py
"""

import os
import sys
import time
import random
from typing import Optional

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Auth + Google API setup
# ---------------------------------------------------------------------------

TOKEN_PATH = os.path.expanduser("~/Developer/hybrid-conversions/hybrid_gtm_token.json")
CLIENT_SECRETS_PATH = os.path.expanduser("~/Developer/hybrid_gtm_client_secrets.json")

# Match the scopes already granted in hybrid-conversions/auth_hybrid_gtm.py.
SCOPES = [
    "https://www.googleapis.com/auth/tagmanager.readonly",
    "https://www.googleapis.com/auth/tagmanager.edit.containers",
    "https://www.googleapis.com/auth/tagmanager.edit.containerversions",
    "https://www.googleapis.com/auth/tagmanager.publish",
    "https://www.googleapis.com/auth/tagmanager.manage.accounts",
]

_service = None
_accounts_cache = None  # list[dict] — populated on first list/find call


def _get_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not os.path.exists(TOKEN_PATH):
        return None
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return creds if creds and creds.valid else None


def _build_service():
    global _service
    if _service is not None:
        return True
    creds = _get_credentials()
    if not creds:
        return False
    from googleapiclient.discovery import build
    _service = build("tagmanager", "v2", credentials=creds, cache_discovery=False)
    return True


def _require_service():
    if not _build_service():
        return (
            "ERROR: Not authenticated. Token at "
            f"{TOKEN_PATH} is missing or invalid. Run:\n"
            "  ~/Developer/hybrid-conversions/.venv/bin/python3 "
            "~/Developer/hybrid-conversions/auth_hybrid_gtm.py"
        )
    return None


def _api(request, max_retries: int = 6):
    """Execute a request with exponential backoff on 429/5xx."""
    from googleapiclient.errors import HttpError
    for attempt in range(max_retries):
        try:
            return request.execute()
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            if status in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                time.sleep((2 ** attempt) + random.random())
                continue
            raise


# ---------------------------------------------------------------------------
# Helpers — domain → container resolution
# ---------------------------------------------------------------------------

def _bare_domain(s: str) -> str:
    s = (s or "").strip().lower()
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    s = s.rstrip("/").split("/")[0]
    if s.startswith("www."):
        s = s[4:]
    return s


def _ensure_accounts():
    """List + cache accounts. The cache lives for the MCP server lifetime."""
    global _accounts_cache
    if _accounts_cache is not None:
        return _accounts_cache
    resp = _api(_service.accounts().list())
    _accounts_cache = resp.get("account", [])
    return _accounts_cache


def _list_containers_for_account(account_id: str):
    return _api(
        _service.accounts().containers().list(parent=f"accounts/{account_id}")
    ).get("container", [])


def _resolve_container(domain_or_public_id: str) -> Optional[dict]:
    """
    Resolve a domain (e.g. 'phoenixk9trainers.com') or a GTM public ID
    (e.g. 'GTM-MM8HPN8L') to a container dict. Iterates all accounts.
    Returns the matching container dict (with 'accountId' filled in) or None.
    """
    s = (domain_or_public_id or "").strip()
    bare = _bare_domain(s)
    is_public_id = s.upper().startswith("GTM-")

    for acct in _ensure_accounts():
        aid = acct["accountId"]
        try:
            containers = _list_containers_for_account(aid)
        except Exception:
            continue
        for c in containers:
            c["_account_name"] = acct.get("name", "")
            if is_public_id and c.get("publicId", "").upper() == s.upper():
                return c
            if not is_public_id:
                # Match domains[] entries OR substring of container name.
                for d in c.get("domainName", []) or []:
                    if _bare_domain(d) == bare:
                        return c
                if bare and bare in (c.get("name", "") or "").lower():
                    return c
    return None


def _default_workspace_path(container_path: str) -> Optional[str]:
    workspaces = _api(
        _service.accounts().containers().workspaces().list(parent=container_path)
    ).get("workspace", [])
    if not workspaces:
        return None
    default = next(
        (w for w in workspaces if (w.get("name") or "").lower() == "default workspace"),
        workspaces[0],
    )
    return default["path"]


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "gtm",
    instructions=(
        "Google Tag Manager tools: list accounts/containers, find container by "
        "domain, inspect tags/triggers/variables, pause/update/delete tags, and "
        "publish container versions. All write operations default to dry_run=True; "
        "set dry_run=False to apply changes."
    ),
)


# ---- Discovery -----------------------------------------------------------------

@mcp.tool()
def list_accounts() -> dict:
    """List all GTM accounts visible to the authenticated user."""
    err = _require_service()
    if err:
        return {"error": err}
    accts = _ensure_accounts()
    return {
        "count": len(accts),
        "accounts": [
            {"account_id": a["accountId"], "name": a.get("name", ""), "path": a["path"]}
            for a in accts
        ],
    }


@mcp.tool()
def list_containers(account_id: str) -> dict:
    """List containers under a single GTM account."""
    err = _require_service()
    if err:
        return {"error": err}
    containers = _list_containers_for_account(account_id)
    return {
        "count": len(containers),
        "containers": [
            {
                "container_id": c["containerId"],
                "public_id": c.get("publicId", ""),
                "name": c.get("name", ""),
                "domains": c.get("domainName", []),
                "path": c["path"],
            }
            for c in containers
        ],
    }


@mcp.tool()
def find_container(domain_or_public_id: str) -> dict:
    """
    Resolve a site domain (e.g. 'phoenixk9trainers.com') OR a GTM public ID
    (e.g. 'GTM-MM8HPN8L') to a container. Searches all accessible accounts.

    Returns container metadata + the default workspace path, which is what
    most downstream tools (list_tags, list_triggers, etc.) require.
    """
    err = _require_service()
    if err:
        return {"error": err}
    c = _resolve_container(domain_or_public_id)
    if not c:
        return {"error": f"No container found for '{domain_or_public_id}'."}
    try:
        wpath = _default_workspace_path(c["path"])
    except Exception as e:
        wpath = None
    return {
        "account_id": c["accountId"],
        "account_name": c.get("_account_name", ""),
        "container_id": c["containerId"],
        "public_id": c.get("publicId", ""),
        "name": c.get("name", ""),
        "domains": c.get("domainName", []),
        "container_path": c["path"],
        "default_workspace_path": wpath,
    }


# ---- Tags / triggers / variables (read) ---------------------------------------

def _resolve_workspace_path(domain_or_public_id: str, workspace_path: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Returns (wpath, error). Either pass workspace_path directly, or a domain/public_id."""
    if workspace_path:
        return workspace_path, None
    if not domain_or_public_id:
        return None, "Provide either workspace_path or domain_or_public_id."
    c = _resolve_container(domain_or_public_id)
    if not c:
        return None, f"No container found for '{domain_or_public_id}'."
    wpath = _default_workspace_path(c["path"])
    if not wpath:
        return None, f"No workspace found in container {c.get('publicId')}."
    return wpath, None


@mcp.tool()
def list_tags(
    domain_or_public_id: str = "",
    workspace_path: str = "",
    type_filter: str = "",
) -> dict:
    """
    List all tags in a container's default workspace.

    Pass either domain_or_public_id (e.g. 'phoenixk9trainers.com' or 'GTM-MM8HPN8L')
    OR workspace_path directly (faster if you already have it from find_container).

    type_filter: case-insensitive substring match on tag.type (e.g. 'awct', 'awcc',
    'gaawe', 'html'). Empty = no filter.
    """
    err = _require_service()
    if err:
        return {"error": err}
    wpath, err = _resolve_workspace_path(domain_or_public_id, workspace_path)
    if err:
        return {"error": err}
    tags = _api(
        _service.accounts().containers().workspaces().tags().list(parent=wpath)
    ).get("tag", [])
    if type_filter:
        tf = type_filter.lower()
        tags = [t for t in tags if tf in (t.get("type") or "").lower()]
    return {
        "workspace_path": wpath,
        "count": len(tags),
        "tags": [
            {
                "tag_id": t["tagId"],
                "name": t.get("name", ""),
                "type": t.get("type", ""),
                "paused": bool(t.get("paused", False)),
                "firing_trigger_ids": t.get("firingTriggerId", []),
                "blocking_trigger_ids": t.get("blockingTriggerId", []),
                "path": t["path"],
            }
            for t in tags
        ],
    }


@mcp.tool()
def get_tag(tag_path: str) -> dict:
    """
    Fetch a single tag's full configuration by path
    (e.g. 'accounts/.../containers/.../workspaces/.../tags/N').
    Use this to see tag parameters before updating.
    """
    err = _require_service()
    if err:
        return {"error": err}
    tag = _api(
        _service.accounts().containers().workspaces().tags().get(path=tag_path)
    )
    return tag


@mcp.tool()
def list_triggers(
    domain_or_public_id: str = "",
    workspace_path: str = "",
) -> dict:
    """List all triggers in a container's default workspace."""
    err = _require_service()
    if err:
        return {"error": err}
    wpath, err = _resolve_workspace_path(domain_or_public_id, workspace_path)
    if err:
        return {"error": err}
    triggers = _api(
        _service.accounts().containers().workspaces().triggers().list(parent=wpath)
    ).get("trigger", [])
    return {
        "workspace_path": wpath,
        "count": len(triggers),
        "triggers": [
            {
                "trigger_id": t["triggerId"],
                "name": t.get("name", ""),
                "type": t.get("type", ""),
                "path": t["path"],
            }
            for t in triggers
        ],
    }


@mcp.tool()
def list_variables(
    domain_or_public_id: str = "",
    workspace_path: str = "",
    type_filter: str = "",
) -> dict:
    """List all user-defined variables in a container's default workspace."""
    err = _require_service()
    if err:
        return {"error": err}
    wpath, err = _resolve_workspace_path(domain_or_public_id, workspace_path)
    if err:
        return {"error": err}
    vars_ = _api(
        _service.accounts().containers().workspaces().variables().list(parent=wpath)
    ).get("variable", [])
    if type_filter:
        tf = type_filter.lower()
        vars_ = [v for v in vars_ if tf in (v.get("type") or "").lower()]
    return {
        "workspace_path": wpath,
        "count": len(vars_),
        "variables": [
            {
                "variable_id": v["variableId"],
                "name": v.get("name", ""),
                "type": v.get("type", ""),
                "path": v["path"],
            }
            for v in vars_
        ],
    }


# ---- Tag mutations (write) ----------------------------------------------------

@mcp.tool()
def pause_tag(tag_path: str, paused: bool = True, dry_run: bool = True) -> dict:
    """
    Pause or unpause a tag. Default action pauses (paused=True). Set paused=False
    to unpause an existing paused tag. Set dry_run=False to actually apply.

    Note: this only changes the workspace state — call create_version_and_publish
    afterward to push the change live to the container.
    """
    err = _require_service()
    if err:
        return {"error": err}
    tag = _api(
        _service.accounts().containers().workspaces().tags().get(path=tag_path)
    )
    current = bool(tag.get("paused", False))
    if current == paused:
        return {
            "no_change": True,
            "tag_id": tag["tagId"],
            "name": tag.get("name", ""),
            "paused": current,
        }
    if dry_run:
        return {
            "dry_run": True,
            "tag_id": tag["tagId"],
            "name": tag.get("name", ""),
            "from_paused": current,
            "to_paused": paused,
            "would_call": "tags().update",
            "hint": "Re-run with dry_run=False, then call create_version_and_publish.",
        }
    tag["paused"] = paused
    updated = _api(
        _service.accounts().containers().workspaces().tags().update(
            path=tag_path, body=tag
        )
    )
    return {
        "tag_id": updated["tagId"],
        "name": updated.get("name", ""),
        "paused": bool(updated.get("paused", False)),
        "fingerprint": updated.get("fingerprint"),
    }


@mcp.tool()
def delete_tag(tag_path: str, dry_run: bool = True) -> dict:
    """
    Delete a tag from the workspace. Default dry_run=True. Set dry_run=False to apply.
    Call create_version_and_publish afterward to push the change live.
    """
    err = _require_service()
    if err:
        return {"error": err}
    tag = _api(
        _service.accounts().containers().workspaces().tags().get(path=tag_path)
    )
    if dry_run:
        return {
            "dry_run": True,
            "tag_id": tag["tagId"],
            "name": tag.get("name", ""),
            "type": tag.get("type", ""),
            "would_call": "tags().delete",
        }
    _api(
        _service.accounts().containers().workspaces().tags().delete(path=tag_path)
    )
    return {"deleted": True, "tag_id": tag["tagId"], "name": tag.get("name", "")}


@mcp.tool()
def delete_variable(variable_path: str, dry_run: bool = True) -> dict:
    """
    Delete a user-defined variable from the workspace. Default dry_run=True.
    Call create_version_and_publish afterward to push the change live.
    """
    err = _require_service()
    if err:
        return {"error": err}
    var = _api(
        _service.accounts().containers().workspaces().variables().get(path=variable_path)
    )
    if dry_run:
        return {
            "dry_run": True,
            "variable_id": var["variableId"],
            "name": var.get("name", ""),
            "type": var.get("type", ""),
            "would_call": "variables().delete",
        }
    _api(
        _service.accounts().containers().workspaces().variables().delete(path=variable_path)
    )
    return {"deleted": True, "variable_id": var["variableId"], "name": var.get("name", "")}


@mcp.tool()
def delete_trigger(trigger_path: str, dry_run: bool = True) -> dict:
    """
    Delete a trigger from the workspace. Default dry_run=True.
    Call create_version_and_publish afterward to push the change live.
    """
    err = _require_service()
    if err:
        return {"error": err}
    trig = _api(
        _service.accounts().containers().workspaces().triggers().get(path=trigger_path)
    )
    if dry_run:
        return {
            "dry_run": True,
            "trigger_id": trig["triggerId"],
            "name": trig.get("name", ""),
            "type": trig.get("type", ""),
            "would_call": "triggers().delete",
        }
    _api(
        _service.accounts().containers().workspaces().triggers().delete(path=trigger_path)
    )
    return {"deleted": True, "trigger_id": trig["triggerId"], "name": trig.get("name", "")}


# ---- Versions / publish -------------------------------------------------------

@mcp.tool()
def create_version_and_publish(
    workspace_path: str,
    name: str,
    notes: str = "",
    dry_run: bool = True,
) -> dict:
    """
    Create a container version from a workspace and immediately publish it live.

    workspace_path: from find_container().default_workspace_path
    name: short version name (shown in GTM UI)
    notes: longer description (shown in version detail)
    dry_run: True = preview only.

    GTM mutations stay in the workspace until a version is created + published.
    Without this step, paused/deleted/updated tags do NOT take effect on the
    deployed container.
    """
    err = _require_service()
    if err:
        return {"error": err}
    if dry_run:
        return {
            "dry_run": True,
            "workspace_path": workspace_path,
            "version_name": name,
            "notes": notes,
            "would_call": "workspaces.create_version + versions.publish",
        }
    ver_resp = _api(
        _service.accounts().containers().workspaces().create_version(
            path=workspace_path,
            body={"name": name, "notes": notes},
        )
    )
    version = ver_resp.get("containerVersion", {})
    vpath = version.get("path")
    if not vpath:
        return {
            "error": "create_version returned no containerVersion path",
            "raw": ver_resp,
        }
    pub = _api(_service.accounts().containers().versions().publish(path=vpath))
    return {
        "published": True,
        "version_id": version.get("containerVersionId"),
        "version_path": vpath,
        "version_name": version.get("name"),
        "compiler_error": pub.get("compilerError", False),
    }


@mcp.tool()
def list_workspaces(domain_or_public_id: str = "", container_path: str = "") -> dict:
    """
    List all workspaces in a container. After a publish, the default workspace is
    marked 'submitted' and becomes read-only — `update_tag`/`delete_tag` will then
    fail with 400 'Workspace is already submitted.' Use create_workspace to spawn
    a fresh writable one.
    """
    err = _require_service()
    if err:
        return {"error": err}
    if not container_path:
        if not domain_or_public_id:
            return {"error": "Provide either domain_or_public_id or container_path."}
        c = _resolve_container(domain_or_public_id)
        if not c:
            return {"error": f"No container found for '{domain_or_public_id}'."}
        container_path = c["path"]
    workspaces = _api(
        _service.accounts().containers().workspaces().list(parent=container_path)
    ).get("workspace", [])
    return {
        "container_path": container_path,
        "count": len(workspaces),
        "workspaces": [
            {
                "workspace_id": w["workspaceId"],
                "name": w.get("name", ""),
                "description": w.get("description", ""),
                "path": w["path"],
            }
            for w in workspaces
        ],
    }


@mcp.tool()
def create_workspace(
    container_path: str = "",
    domain_or_public_id: str = "",
    name: str = "",
    description: str = "",
    dry_run: bool = True,
) -> dict:
    """
    Create a fresh workspace in a container. Use this when the default workspace
    is in 'submitted' (read-only) state after a recent publish, or to isolate
    a multi-step change before publishing.

    A new workspace syncs from the latest published version, so all existing
    tags/triggers/variables are present and editable.

    Pass either container_path directly or domain_or_public_id (the MCP will resolve).
    """
    err = _require_service()
    if err:
        return {"error": err}
    if not container_path:
        if not domain_or_public_id:
            return {"error": "Provide either container_path or domain_or_public_id."}
        c = _resolve_container(domain_or_public_id)
        if not c:
            return {"error": f"No container found for '{domain_or_public_id}'."}
        container_path = c["path"]
    if not name:
        return {"error": "name is required."}
    body = {"name": name}
    if description:
        body["description"] = description
    if dry_run:
        return {
            "dry_run": True,
            "container_path": container_path,
            "workspace_name": name,
            "would_call": "workspaces.create",
        }
    ws = _api(
        _service.accounts().containers().workspaces().create(
            parent=container_path, body=body
        )
    )
    return {
        "workspace_id": ws["workspaceId"],
        "name": ws.get("name"),
        "description": ws.get("description", ""),
        "path": ws["path"],
    }


@mcp.tool()
def delete_workspace(workspace_path: str, dry_run: bool = True) -> dict:
    """
    Delete a workspace. Useful for cleaning up stale `fix-*` workspaces that
    accumulate from prior automation runs. Cannot delete the Default Workspace.
    """
    err = _require_service()
    if err:
        return {"error": err}
    ws = _api(
        _service.accounts().containers().workspaces().get(path=workspace_path)
    )
    name = ws.get("name", "")
    if name.lower() == "default workspace":
        return {"error": "Cannot delete the Default Workspace."}
    if dry_run:
        return {
            "dry_run": True,
            "workspace_id": ws["workspaceId"],
            "name": name,
            "would_call": "workspaces.delete",
        }
    _api(
        _service.accounts().containers().workspaces().delete(path=workspace_path)
    )
    return {"deleted": True, "workspace_id": ws["workspaceId"], "name": name}


@mcp.tool()
def update_tag(tag_path: str, patch: dict, dry_run: bool = True) -> dict:
    """
    Generic tag update. Fetches the current tag, deep-merges `patch` over the
    top, then writes it back. Use this for changes beyond pause/unpause —
    swapping a Clarity HTML body, changing an awct tag's conversion action ID,
    renaming a tag, retargeting firing triggers, etc.

    Examples:
      patch = {"name": "Renamed Tag"}
      patch = {"firingTriggerId": ["12345"]}
      patch = {"parameter": [{"type": "TEMPLATE", "key": "html",
                              "value": "<script>...</script>"}]}

    Note: 'parameter' is REPLACED, not deep-merged — fetch via get_tag first
    to see the current parameter list, then provide the full replacement.
    """
    err = _require_service()
    if err:
        return {"error": err}
    if not isinstance(patch, dict) or not patch:
        return {"error": "patch must be a non-empty dict of fields to overwrite."}

    tag = _api(
        _service.accounts().containers().workspaces().tags().get(path=tag_path)
    )
    # Top-level keys in patch overwrite — caller must replace lists wholesale.
    new_tag = dict(tag)
    new_tag.update(patch)

    if dry_run:
        diff = {k: {"from": tag.get(k), "to": new_tag[k]} for k in patch.keys()}
        return {
            "dry_run": True,
            "tag_id": tag["tagId"],
            "name": tag.get("name", ""),
            "diff": diff,
            "would_call": "tags.update",
            "hint": "Re-run with dry_run=False, then call create_version_and_publish.",
        }
    updated = _api(
        _service.accounts().containers().workspaces().tags().update(
            path=tag_path, body=new_tag
        )
    )
    return {
        "tag_id": updated["tagId"],
        "name": updated.get("name", ""),
        "fingerprint": updated.get("fingerprint"),
    }


@mcp.tool()
def create_tag(
    workspace_path: str,
    body: dict,
    dry_run: bool = True,
) -> dict:
    """
    Create a new tag in a workspace. Caller supplies the full GTM tag body dict.

    Required keys in body: `name`, `type` (e.g. 'awct', 'awcc', 'gaawe', 'html').
    Common optional keys: `parameter` (list of {type, key, value/list} dicts),
    `firingTriggerId` (list of trigger IDs), `blockingTriggerId`, `tagFiringOption`,
    `paused`.

    Example (Custom HTML / Clarity):
      body = {
        "name": "Microsoft Clarity",
        "type": "html",
        "parameter": [{"type": "TEMPLATE", "key": "html",
                       "value": "<script>(function(c,l,a,r,i,t,y){...})(...);</script>"}],
        "firingTriggerId": ["2147479553"]  # All Pages trigger
      }

    For complex tag types (awct, awcc, gaawe), inspect an existing same-type tag
    via get_tag first and copy its parameter shape.
    """
    err = _require_service()
    if err:
        return {"error": err}
    if not isinstance(body, dict):
        return {"error": "body must be a dict."}
    for required in ("name", "type"):
        if required not in body:
            return {"error": f"body must include '{required}'."}
    if dry_run:
        return {
            "dry_run": True,
            "workspace_path": workspace_path,
            "tag_name": body.get("name"),
            "tag_type": body.get("type"),
            "firing_trigger_ids": body.get("firingTriggerId", []),
            "would_call": "tags.create",
            "hint": "Re-run with dry_run=False, then call create_version_and_publish.",
        }
    created = _api(
        _service.accounts().containers().workspaces().tags().create(
            parent=workspace_path, body=body
        )
    )
    return {
        "tag_id": created["tagId"],
        "name": created.get("name", ""),
        "type": created.get("type", ""),
        "path": created["path"],
        "fingerprint": created.get("fingerprint"),
    }


@mcp.tool()
def create_trigger(
    workspace_path: str,
    body: dict,
    dry_run: bool = True,
) -> dict:
    """
    Create a new trigger in a workspace. Caller supplies the full GTM trigger body dict.

    Required keys: `name`, `type` (e.g. 'pageview', 'linkClick', 'click', 'customEvent').
    Optional: `filter`, `customEventFilter`, `autoEventFilter` (each a list of
    condition clauses with `type`, `parameter`).

    Example (click on tel: link):
      body = {
        "name": "Click - tel: link",
        "type": "linkClick",
        "filter": [
          {"type": "startsWith",
           "parameter": [
             {"type": "TEMPLATE", "key": "arg0", "value": "{{Click URL}}"},
             {"type": "TEMPLATE", "key": "arg1", "value": "tel:"}
           ]}
        ]
      }

    For complex trigger types, inspect an existing trigger via list_triggers
    + the raw API response shape, then copy.
    """
    err = _require_service()
    if err:
        return {"error": err}
    if not isinstance(body, dict):
        return {"error": "body must be a dict."}
    for required in ("name", "type"):
        if required not in body:
            return {"error": f"body must include '{required}'."}
    if dry_run:
        return {
            "dry_run": True,
            "workspace_path": workspace_path,
            "trigger_name": body.get("name"),
            "trigger_type": body.get("type"),
            "would_call": "triggers.create",
            "hint": "Re-run with dry_run=False, then call create_version_and_publish.",
        }
    created = _api(
        _service.accounts().containers().workspaces().triggers().create(
            parent=workspace_path, body=body
        )
    )
    return {
        "trigger_id": created["triggerId"],
        "name": created.get("name", ""),
        "type": created.get("type", ""),
        "path": created["path"],
        "fingerprint": created.get("fingerprint"),
    }


@mcp.tool()
def list_versions(domain_or_public_id: str = "", container_path: str = "") -> dict:
    """List version headers (history) for a container. Pass either domain/public_id or container_path."""
    err = _require_service()
    if err:
        return {"error": err}
    if not container_path:
        if not domain_or_public_id:
            return {"error": "Provide either domain_or_public_id or container_path."}
        c = _resolve_container(domain_or_public_id)
        if not c:
            return {"error": f"No container found for '{domain_or_public_id}'."}
        container_path = c["path"]
    headers = _api(
        _service.accounts().containers().version_headers().list(parent=container_path)
    ).get("containerVersionHeader", [])
    return {
        "container_path": container_path,
        "count": len(headers),
        "versions": [
            {
                "version_id": h.get("containerVersionId"),
                "name": h.get("name", ""),
                "deleted": bool(h.get("deleted", False)),
                "num_tags": h.get("numTags", "0"),
                "num_triggers": h.get("numTriggers", "0"),
                "num_variables": h.get("numVariables", "0"),
                "path": h.get("path"),
            }
            for h in headers
        ],
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
