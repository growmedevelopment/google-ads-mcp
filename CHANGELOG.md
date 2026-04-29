# Changelog — google-ads-mcp (GrowME fork)

This file tracks GrowME's modifications on top of upstream
`googleads/google-ads-mcp`. Upstream's own changes are documented in
their commit history; this file only records what we add, change, or
diverge on.

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
`Marketing/ads-mcp-installer/` project's `MCP_PIPX_SOURCE` constant
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
