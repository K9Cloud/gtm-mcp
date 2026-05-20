# gtm-mcp

> **Cloning to a new machine?** Pass the short folder name as the explicit `git clone` target so the local path matches the convention used everywhere else (`~/Developer/CLAUDE.md`, MCP registration, scripts):
>
> ```bash
> git clone git@github.com:K9Cloud/gtm-mcp.git ~/Developer/gtm
> ```
>
> Without the trailing `~/Developer/gtm`, git defaults to creating `~/Developer/gtm-mcp/`, which drifts from the rest of the setup.

A local Claude Code MCP server for **Google Tag Manager** — list/inspect tags, triggers, variables, and workspaces; create/update/pause/delete; create + publish container versions. Built to replace the one-off Python scripts that have been used historically (`cleanup_legacy_awct_gtm.py`, `add_clarity_to_gtm.py`, `fix_clarity_tag_ids.py`, etc.) for ad-hoc GTM work.

## Tools (19)

### Discovery
| Tool | Purpose |
|------|---------|
| `list_accounts` | List all GTM accounts visible to the OAuth user |
| `list_containers` | List containers under one account |
| `find_container` | Resolve a domain (e.g. `phoenixk9trainers.com`) **or** GTM public ID (`GTM-MM8HPN8L`) → container + default workspace path |

### Read
| Tool | Purpose |
|------|---------|
| `list_tags` | List tags in a workspace (optional `type_filter`, e.g. `awct`, `awcc`, `html`) |
| `get_tag` | Full tag config including parameters and trigger refs |
| `list_triggers` | List triggers in a workspace |
| `list_variables` | List user-defined variables (optional `type_filter`) |
| `list_workspaces` | List workspaces in a container (use to detect submitted/read-only state) |
| `list_versions` | Version history for a container |

### Mutate (all default `dry_run=True`)
| Tool | Purpose |
|------|---------|
| `pause_tag` | Pause/unpause a tag |
| `update_tag` | Generic tag patch — rename, swap conversion-action ID, replace HTML body, retarget triggers |
| `create_tag` | Create a new tag (caller supplies full body dict) |
| `delete_tag` | Delete a tag |
| `create_trigger` | Create a new trigger (caller supplies full body dict) |
| `delete_trigger` | Delete a trigger |
| `delete_variable` | Delete a user-defined variable |
| `create_workspace` | Spawn a writable workspace when default is in submitted/read-only state |
| `delete_workspace` | Clean up stale automation workspaces (refuses to delete Default) |
| `create_version_and_publish` | Create container version from workspace + publish live |

## Standard workflow

```
find_container("phoenixk9trainers.com")
  → list_tags(workspace_path=..., type_filter="awct")
  → pause_tag(tag_path=..., dry_run=False)
  → create_version_and_publish(workspace_path=..., name="...", dry_run=False)
```

Without the publish step, workspace edits are invisible to the deployed container.

## Submitted-workspace recovery

After a publish, the source workspace is marked `submitted` and becomes read-only — any `update_tag` / `delete_tag` against it returns `400 "Workspace is already submitted."` Recover by spawning a fresh workspace:

```
create_workspace(domain_or_public_id="...", name="my-edit", dry_run=False)
  → all subsequent edits use the new workspace_path
```

A new workspace syncs from the latest published version, so all existing tags/triggers/variables are present and editable.

## Permission diagnostic

GTM returns `HttpError 404: "Not found or permission denied."` on mutate calls when the OAuth user lacks Edit/Approve/Publish on that container — even when `get_tag` on the same path succeeds. If reads work but writes 404, surface to user and request access; don't burn retry budget. (Seen on the Josh Wilson Team JW account 2026-04-28.)

## OAuth — token reuse

This MCP **reuses the existing `hybrid-conversions` GTM OAuth setup**. No new client_secrets/token files.

- **Token**: `~/Developer/hybrid-conversions/hybrid_gtm_token.json`
- **Client secrets**: `~/Developer/hybrid_gtm_client_secrets.json`
- **Google Cloud project**: K9 Cloud Hybrid Conv Ads API (separate from the `stalwart-camera-484906-k2` project that hosts gdrive/gsc/ga4)
- **Scopes**: `tagmanager.readonly`, `tagmanager.edit.containers`, `tagmanager.edit.containerversions`, `tagmanager.publish`, `tagmanager.manage.accounts`

The same token is used by the legacy Python scripts in `~/Developer/hybrid-conversions/`. Token refreshes are shared.

## Per-machine setup

```bash
# 1. Clone
cd ~/Developer && git clone git@github.com:K9Cloud/gtm-mcp.git gtm

# 2. venv + deps
cd gtm
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install mcp google-api-python-client google-auth-oauthlib google-auth-httplib2

# 3. Ensure the GTM OAuth token exists locally
ls ~/Developer/hybrid-conversions/hybrid_gtm_token.json
# If missing, bootstrap via the existing auth helper:
~/Developer/hybrid-conversions/.venv/bin/python3 ~/Developer/hybrid-conversions/auth_hybrid_gtm.py

# 4. Register with Claude Code (paths use the current machine's username)
claude mcp add -s user gtm \
  "$HOME/Developer/gtm/.venv/bin/python3" \
  "$HOME/Developer/gtm/mcp-server/server.py"

# 5. Verify
claude mcp get gtm
# Should print: Status: ✓ Connected
```

## Layout

```
gtm/
├── README.md                  (this file)
├── .gitignore
├── mcp-server/
│   └── server.py              (the MCP server — 19 tools)
└── .venv/                     (gitignored — per machine)
```

No `auth.py` here — the OAuth flow lives in `hybrid-conversions/auth_hybrid_gtm.py` (this MCP and the legacy scripts share that single token).

## Notes

- **Container resolution caches account list** for the MCP server lifetime. Restart Claude Code to pick up newly-added GTM accounts.
- **All write tools default to `dry_run=True`**. Set `dry_run=False` to apply.
- **Mutations stay in the workspace** until you call `create_version_and_publish`.
- **`update_tag` patch is a top-level overwrite, not a deep merge** — for fields like `parameter` (a list), fetch via `get_tag` first and supply the full replacement.

## Usage notes & gotchas

- **Per-minute rate limit (~60 queries/min/user)** — iterating all 63 accounts to find a container can hit `429 rateLimitExceeded`. **Use steady 1.5s pacing between calls — NOT exponential backoff.** Exponential backoff doubles wait times after each failure, fighting itself when the per-minute window resets every 60s. Steady 1.5s/call ≈ 40 calls/min, well under the limit; a full account scan finishes in ~96s reliably. (Learned 2026-05-04 olk9twincities session, when the count was 64.)
- **Container cache file** — `~/Developer/hybrid-conversions/gtm_container_cache.json` maps `{public_id: container_path}` for ~170 containers, populated during the 2026-05-04 paced scan. Use it to skip the account-scan entirely on future container lookups: `cache = json.load(open(CACHE)); container_path = cache[target_id]`. Re-run the scan to refresh after new containers are created.
- **Workspaces auto-recycle after publish** — the workspace ID returned by `find_container` (`default_workspace_path`) **changes after each `create_version_and_publish`** (e.g. workspace `6` becomes `7` after publish; the old workspace returns 404 on subsequent reads). Always re-fetch the workspace path at the start of each mutation session — never cache the workspace path across sessions.
- **Trigger names cannot contain colons** (`:`). API rejects with `400: name: The name contains invalid character: ":"`. Use parentheses instead — e.g. `"Click - Phone (tel)"` not `"Click - tel:"`.
- **Tools may lazy-load via ToolSearch** — the initial deferred-tool list in a session may show only ~13 of the 19 tools; the rest (`create_tag`, `create_trigger`, `update_tag`, `create_workspace`, `delete_workspace`, `list_workspaces`) appear after a ToolSearch query. Before reaching for direct Python on a missing-looking capability, run `ToolSearch select:mcp__gtm__<tool_name>` first.
- **GA4 Event tag body structure** (`gaawe` type) — useful when creating tags via `create_tag` or direct API:
  ```python
  body = {
      "name": "GA4 Event - generate_lead", "type": "gaawe",
      "parameter": [
          {"type": "template", "key": "measurementIdOverride", "value": "G-XXXXXXXXXX"},
          {"type": "template", "key": "eventName", "value": "generate_lead"},
          {"type": "boolean", "key": "sendEcommerceData", "value": "false"},
      ],
      "firingTriggerId": ["6"],  # existing trigger by ID
      "tagFiringOption": "oncePerEvent",
  }
  ```
  Tel-click trigger: `type=linkClick`, filter `startsWith` on `{{Click URL}}` value `tel:`. Requires the `clickUrl` built-in variable enabled in the workspace.
- **Historical motivation** — replaces one-off scripts like `cleanup_legacy_awct_gtm.py`. The motivating use case was the post-2026-06-01 cleanup of legacy `__awct` form tags + dead `__awec` variables / `wpformsData` macros across all containers.
