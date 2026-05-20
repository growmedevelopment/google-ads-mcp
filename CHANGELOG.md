# Changelog — google-ads-mcp (GrowME fork)

This file tracks GrowME's modifications on top of upstream
`googleads/google-ads-mcp`. Upstream's own changes are documented in
their commit history; this file only records what we add, change, or
diverge on.

## 0.0.1.post1+growme.2 — 2026-05-13

### Added
- **`ads_mcp/tools/core.py` → `list_customer_clients`** — new tool that
  walks an MCC's manager hierarchy via the `customer_client` resource
  and returns every linked child account (including those inherited
  through MCC access, which upstream's `list_accessible_customers`
  omits). Takes optional `manager_customer_id`; defaults to the
  `GOOGLE_ADS_LOGIN_CUSTOMER_ID` env var. Read-only.

### Changed
- **`ads_mcp/tools/core.py` → `list_accessible_customers` docstring** —
  rewritten to make the direct-vs-MCC distinction explicit. Old wording
  ("customers directly accessible by the user") was technically correct
  but consistently misled both humans and LLMs into using this tool when
  they wanted the full MCC roster. New docstring routes callers to the
  new `list_customer_clients` tool for that case.
- **`README.md`** — tools section now documents `list_customer_clients`
  and `generate_keyword_ideas` (the latter was already shipping in
  growme.1 but wasn't listed). `list_accessible_customers` description
  clarified to flag the direct-access limitation.

### Why this matters
The GrowME Corp MCC (9755129455) has 150 ENABLED child accounts, but
`list_accessible_customers` only returned 24 — the subset where
access@growme.ca had been added with a direct user grant. Every other
account was invisible to the MCP, and the LLM had no way to discover
them. This made every "audit our accounts" or "find every active
campaign across the MCC" workflow silently undercount. The new tool
returns the full set in a single call.

### Files added
- None.

### Upgrade notes
- pipx / uv users: `pipx install --force git+...` (or `uv tool install
  --force git+...`) picks up the new tool on next install.
- Claude config files don't need updating; the new tool registers
  automatically through `ads_mcp/server.py`'s `core` import.
- Restart Claude Desktop / Claude Code after upgrading so the MCP
  subprocess re-launches with the new tool list.

## 0.0.1.post1+growme.1 — 2026-04-28

### Added
- **`ads_mcp/tools/keyword_planner.py`** — new `generate_keyword_ideas`
  tool wrapping `KeywordPlanIdeaService.GenerateKeywordIdeas`. Returns
  keyword text + historical metrics (avg monthly searches, competition
  level, competition index, top-of-page CPC bid range) for a list of
  seed keywords in the requested geo + language. Read-only; counts as
  1 operation against the Google Ads developer-token quota per call.

### Changed
- **`ads_mcp/server.py`** — added `keyword_planner` to the tool import
  list so FastMCP registers it at startup.
- **`pyproject.toml`** — bumped version to `0.0.1.post1+growme.1` (PEP
  440 local-version identifier marking the downstream fork). Updated
  description to mention the new tool. Added GrowME as a co-author.

### Files added (governance)
- **`NOTICE`** — Apache 2.0 attribution: lists upstream copyright and
  GrowME modifications. Required by the license.
- **`CHANGELOG.md`** — this file (only tracks GrowME diff from upstream).
- **`CLAUDE.md`** — AI session handoff for this fork.

### Why this fork exists
Upstream's MCP only ships two tools (`search` for GAQL, plus
`list_accessible_customers`). Neither calls KP-related Google Ads
services. We need direct keyword data in Claude conversations for new
client briefs and ongoing campaign expansion, so we added the missing
tool here. Distribution: pipx/uv install from
`git+https://github.com/growmeapps/google-ads-mcp.git`. The
`marketing/ads-mcp-installer/` project's `MCP_PIPX_SOURCE` constant
points at this fork instead of upstream as of the same date.

### Upstream sync plan
We track upstream via the `upstream` git remote (`git remote add
upstream https://github.com/googleads/google-ads-mcp.git`). When upstream
publishes a new release we'll merge in via:

```bash
git fetch upstream
git merge upstream/main           # may need conflict resolution if upstream
                                   # touches server.py's tool import block
```

The keyword_planner.py file is fully isolated, so conflicts only happen
in `server.py` (the import line) and `pyproject.toml` (the version).
Both are trivial to resolve.
