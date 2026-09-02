# Changelog — google-ads-mcp (GrowME fork)

This file tracks GrowME's modifications on top of upstream
`googleads/google-ads-mcp`. Upstream's own changes are documented in
their commit history; this file only records what we add, change, or
diverge on.

## 0.0.1.post1+growme.4 — 2026-09-02 — fix what +growme.3 missed (paging, classification, CI lint)

An adversarial review of `+growme.3` (three refuters on the diagnosis, three
code lenses on the diff) found two blockers and several real defects. The
diagnosis held; the fix did not, in these places:

### Blockers
- **Every page after the first bypassed the pacer, the retry and the error
  conversion.** `generate_keyword_ideas` returns a lazy pager, and
  `+growme.3` returned that pager from inside the protected block: the
  `for idea in response:` loop then issued one fresh RPC per `next_page_token`,
  back to back, outside everything the change existed to add. A 429 on page 2
  escaped as a raw `ResourceExhausted` — the original bug, reintroduced on the
  fix's own happy path, and a regression against the pre-fix code where that
  loop at least sat inside `except GoogleAdsException`. Paging is now explicit
  (`page_token`), so each page is an independent RPC through the pacer and the
  retry. Driving the pager's own generator was tried and rejected: once a page
  fetch raises, that generator is finished, so a retry silently resumes at "no
  more pages" and truncates the result instead of failing.
- **CI lint failed.** `noxfile.py` runs `black --check -l 80`; the new files
  were formatted at black's default 88. Both are now formatted at 80 and the
  exact CI command exits 0. (Commit `0fc4a0a` was this same mistake on this
  same file, so it is now checked with the repo's own command before pushing.)

### Correctness
- **The daily-vs-rate classifier ignored `rate_scope`, the field that actually
  separates them.** It matched free text for "day"/"daily"/"operations", while
  Google's documented `rate_name` examples ("Requests per account", "Get
  requests for standard access") contain none of those, and `QuotaRateScope`
  spells it out: ACCOUNT is the per-customer bucket, DEVELOPER the token's
  daily one. A real daily-cap rejection was therefore reported to the model as
  the per-second limit and retried against an exhausted quota. Scope is now
  read first; `rate_name` is a fallback and needs an explicit day token, since
  "operations" alone also appears in per-minute buckets.
- **A quota rejection with no detail is now reported as ambiguous, not as the
  rate limit.** Google answers both limits with the same code; when nothing
  distinguishes them the tool says so, says which is likelier and why, and
  gives the 60-second test that separates them.
- **The QuotaError details are recovered rather than discarded.** The library's
  `Interceptor` short-circuits gRPC RESOURCE_EXHAUSTED, returning the raw
  RpcError without building a `GoogleAdsException` — which is why the old
  handler never saw these. `from_grpc_error` keeps the original error, so the
  trailing metadata is now searched for a `GoogleAdsFailure` and parsed when
  present; the metadata keys are logged when it is absent, so one real
  rejection settles whether Google attaches one. (`+growme.3` asserted it does
  not. Nothing had ever checked; the reproduction only captured `str(exc)`.)
- `TooManyRequests` is caught rather than only its `ResourceExhausted` subclass.
- The pacer is keyed on customer ID, matching the per-CID limit the docstring
  already advises callers to exploit, instead of one global gate.
- The pacer refuses to queue a caller longer than 10 s (FastMCP's sync-tool
  pool is 40 threads and the tool has no timeout, so an unbounded queue could
  stall every other tool in the server); it raises a ToolError telling the
  model to serialize instead.
- `assert` on the terminal invariant replaced with a real error (asserts vanish
  under `python -O`), and the proto type lookups hoisted out of the batch loop
  (each one builds a fresh `GoogleAdsClient` and credentials).

### Honesty about results
- `page_size` is documented correctly: rows per PAGE (API max 10,000), not a
  cap on the result. The tool walks every page, so results are capped at 2,000
  rows with a logged warning, and a page costs its own operation and its own
  rate-limit slot — a smaller `page_size` means MORE requests, not fewer.
  `+growme.3` still claimed "each call is 1 operation regardless of returned-row
  count", which paging makes false.

### Tests — 30 in this file, 48 total
Mutation-checked, not just added: replacing the pacer lock with a null context
now fails exactly one test (it previously failed none). New coverage for
request shape (language, geo targets, adult filter, page size, network enum,
per batch), multi-page merge and page-token round-trips, a 429 on a later page
raising rather than truncating, the result cap, per-customer pacing, the queue
ceiling, scope-based classification in both directions, ambiguous-429 wording,
and trailing-metadata recovery. Pacer waits and retry backoffs now go through
separate indirections, so tests no longer patch stdlib `time.sleep` globally.

### The same 429 could still escape from the other tools
`search` and `list_customer_clients` caught only `GoogleAdsException` too, so
the identical raw 429 could reach the model from the highest-volume tool and
reproduce the same "daily cap" reading on a different surface. Both now catch
`TooManyRequests` and raise `utils.quota_tool_error_message(ex)`, which states
that Google uses one code for both limits, that a rate limit is far likelier
and clears in seconds, and gives the 60-second test. They do NOT get the 1.1 s
pacer: they are not Keyword Planning methods and are not metered that way.

### Also
- `fastmcp` pinned to `>=4.0,<5`: the smoke goldens encode one exact
  serialization, and an unpinned resolver turns that job red on someone else's
  release, whose reflex fix is to regenerate the golden.
- ⚠️ **Known regression absorbed by the +growme.3 golden regeneration, recorded
  here rather than left silent:** under fastmcp 4 the four resources lost their
  declared `idempotentHint`/`readOnlyHint` annotations on the wire, and the
  `search` tool's six parameter descriptions vanished from its inputSchema (its
  raw "Args:" block leaks into the description instead). Both are real losses in
  what the model sees, neither is caused by this change, and fixing `search`'s
  schema belongs in its own commit.

## 0.0.1.post1+growme.3 — 2026-09-01 — pace + retry `generate_keyword_ideas` (the "429 quota" ticket)

### Why
Abas kept getting `429 ... Resource has been exhausted (e.g. check quota)` from
`generate_keyword_ideas` during keyword research (Asana "MCP Quota Issues",
2026-08-19 onward) and Claude read it as the Basic Access daily cap. It was not.
Cloud Monitoring for `growme-ads` shows no day above ~210 Google Ads API requests
(42-day peak 208 on 2026-08-05; the cap is 15,000 over a sliding 24 h window). Every
429 since 2026-07-08 (36: 35 on `KeywordPlanIdeaService.GenerateKeywordIdeas` from
the MCP, 1 on `GenerateKeywordHistoricalMetrics` from the forecast app) sat in a
minute that also returned 200s, and the same bursts exist on 07-16, 08-05 and 08-18,
weeks before the ticket. The API-version label separates the two callers on this account: every 429 is on
`v24` (this Python MCP), while the forecast app's Node client is `v23` and took one rejection in 42 days.
(`request_count` counts requests; for GenerateKeywordIdeas one request is one operation, so the MCP's own
spend really is ~1.4% of the cap.) Google meters the Keyword Planning
methods separately: **1 request per second per customer ID**. An LLM that fires
several keyword-idea calls in one turn trips it at once.

Reproduced 2026-09-01 against the live API (same developer token, MCC and GCP
project as the team installs, a different OAuth client; page_size 1000 and 50):
4 simultaneous calls pass; 8 simultaneous calls get 5 rejections and the next
call is rejected too. Enforcement behaves as a burst of about 4 in flight
refilling about once a second, so Google's "60 requests per 60 seconds" wording
is not the operative rule. The scope (per customer ID rather than per token) is
consistent with the documented per-CID limit and identified by elimination. The rejection arrives as
`google.api_core.exceptions.ResourceExhausted` (a bare HTTP 429 from the API
front end), **not** as a `GoogleAdsException` carrying a `QuotaError`, so the
tool's `except GoogleAdsException` never saw it and the raw 429 reached the
model. With the change below, 8 simultaneous calls through the tool function
(in-process, not via the stdio server) succeed 8/8 (paced over ~9 s).

### What changed
- `ads_mcp/tools/keyword_planner.py`
  - **Pacer:** one process-wide lock spaces Keyword Planning calls at least
    1.1 s apart, so parallel tool calls queue instead of racing Google's
    per-second bucket.
  - **Retry:** a quota rejection in either shape (`api_core.ResourceExhausted`
    or `GoogleAdsException` with `QuotaError RESOURCE_EXHAUSTED` /
    `RESOURCE_TEMPORARILY_EXHAUSTED`) is retried up to 3 attempts, waiting
    Google's `retry_delay` when it sends one, else 2 s then 4 s. Waits above
    30 s are reported, not slept.
  - **Honest error:** the `ToolError` now states the quota facts (code, error
    shape, Google's message, rate name/scope/retry_delay when present,
    attempts, total wait, request ID) and says whether it is the per-second
    RATE limit or the DAILY operations quota (a rejection whose rate name
    mentions day/daily/operations and carries no short retry_delay). The
    guidance tells the model to stop parallel calls and batch seeds.
  - **Seed batching:** more than 20 seeds (the API's per-request cap) are sent
    as consecutive batches of 20 through the pacer and merged, de-duplicated
    on `keyword_text`. Empty seed lists are rejected before any API call.
  - **Docstring:** a "RATE LIMIT — READ BEFORE CALLING" block for the model.
- `pyproject.toml`: pytest now ignores macOS AppleDouble `._*` sidecars, which
  break collection on an external volume (`--ignore-glob=*/._*`); run
  `.venv/bin/python -m pytest -q tests --ignore=tests/smoke` or the nox session.
- `tests/tools/keyword_planner_test.py` (new, 16 tests): pacing, retry after
  `retry_delay`, default backoff, persistent rate limit wording, daily-quota
  classification (no retry), long-delay bail-out, non-quota errors untouched,
  bare-429 retry and wording, >20-seed batching + dedupe, empty seeds.
- `tests/smoke/golden_tools_list.json` + `golden_resources_list.json`
  regenerated under the resolved dependencies (fastmcp 4.0.0). Both smoke
  golden tests were already failing at the previous HEAD with current deps
  (fastmcp 4 changed the tools/resources listing shape); regenerated so the
  smoke suite is green again.
- `.gitignore`: `._*` (macOS AppleDouble files from external volumes).
- `pyproject.toml`: version `0.0.1.post1+growme.3`.

### Team action
Reinstall to pick it up (uninstall first: the installer's notes record that
`pipx install --force` over an existing venv has corrupted fastmcp before):
`pipx uninstall google-ads-mcp && pipx install git+https://github.com/growmedevelopment/google-ads-mcp.git`
(uv: `uv tool uninstall google-ads-mcp && uv tool install git+https://github.com/growmedevelopment/google-ads-mcp.git`),
then fully restart Claude Desktop / Claude Code. The pacer is per MCP process
(Desktop and Code each run their own) and the forecast server shares the MCC's
bucket, which is what the retry is for; passing the client's own customer_id
gives a research session its own per-CID bucket.
Standard Access is **not** needed for this: it lifts the daily operations
cap, which we use ~1% of, and does not change the planning per-second limit.

## OAuth migrated `growme-217600` → `growme-ads` (#517652724337) — 2026-05-20

### Why
The old GCP hub project `growme-217600` (#1085020492895) was suspended
2026-05-19 for a leaked-key abuse incident, reinstated 2026-05-20, and
**every OAuth client under it was deleted** as part of the post-recovery
hub teardown. The Desktop OAuth client the team MCP used
(`1085020492895-aldkh0ch47e9tt260rdsldoqj1evjsld.apps.googleusercontent.com`)
is gone — calls against it now return `deleted_client`. A new dedicated
GCP project was provisioned to host this MCP's OAuth client cleanly,
away from the now-decoupled hub.

### New GCP project (replaces growme-217600 for this MCP)
- **Project ID:** `growme-ads`
- **Project number:** `517652724337`
- **Folder:** GrowME Internal (387614648962)
- **APIs enabled:** `googleads.googleapis.com`
- **Billing:** `01A67F-17D7E9-CFA36D` (GrowME Marketing Billing)
- **Hardened:** $10 CAD budget cap, 50/90/100% billing alerts
- **New Desktop OAuth client:** `517652724337-vn43qklsp8e4f6qpqlqlkdgr8j7ud04g.apps.googleusercontent.com` (type: `installed`, consent screen: "GrowME Google Ads", user type: Internal — no unverified-app warning)

### Team re-auth required
Every team member (Ammar, Abas, Grace) must:
1. Pull the new `client_secret.json` from 1Password to
   `~/.config/growme-ads/client_secret.json` (chmod 600). Distribute via
   1Password only — NEVER via Slack/email/chat.
2. Delete the stale `~/.config/growme-ads/client_secret_desktop.json` if
   present (was the old hub one, now dead).
3. Re-mint ADC:
   ```shell
   gcloud auth application-default login \
     --client-id-file="$HOME/.config/growme-ads/client_secret.json" \
     --scopes=https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform
   ```
   **Sign in as `access@growme.ca`** in the Google account picker (NOT a
   personal Google account — the MCC manager grants live on access@).
4. `cp ~/.config/gcloud/application_default_credentials.json ~/.config/growme-ads/adc.json && chmod 600 ~/.config/growme-ads/adc.json`.
5. Restart Claude Desktop / Claude Code so the MCP subprocess re-picks
   up the new ADC.
6. Smoke-check: `list_customer_clients` should return ~150 ENABLED
   accounts under MCC `9755129455`.

### Account-level credentials that did NOT change
- `GOOGLE_ADS_DEVELOPER_TOKEN` — MCC-bound, still valid (in 1Password and
  `apps/google-ads/.env.local`).
- `GOOGLE_ADS_LOGIN_CUSTOMER_ID = 9755129455` — MCC, unchanged.
- Claude `mcpServers.google-ads.env` block in `~/.claude/settings.json`
  references the ADC file path + the two env vars above — all
  project-agnostic. No edit needed.

### Source code
Credential rotation only. No package, tool surface, or version change.
The on-disk filename (`client_secret.json`) and ADC path
(`~/.config/growme-ads/adc.json`) are unchanged; only what each file
contains has been re-issued under the new GCP project.

### Cross-reference
- Incident report:
  `Code/ops/gcp-audit/reports/post-recovery-growme-217600-2026-05-20.md`.

### Files changed in this entry
- `CHANGELOG.md` — this entry.
- `CLAUDE.md` — Authentication section + Resume Point updated to
  reference the new GCP project / OAuth client.
- `.gitignore` — added `client_secret*.json` and `adc*.json` guards so a
  credential file dropped into the working tree during future rotations
  cannot be accidentally committed.

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
