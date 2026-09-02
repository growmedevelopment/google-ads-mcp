# Copyright 2026 Google LLC.
# Modifications Copyright 2026 GrowME Marketing Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tool for generating Keyword Planner ideas via KeywordPlanIdeaService.

GrowME fork addition (2026-04-28). Wraps the Google Ads API's
KeywordPlanIdeaService.GenerateKeywordIdeas method so Claude can pull
keyword volume / competition / CPC bid data directly during a
conversation, without leaving for the Google Ads UI.

2026-09-01: added a client-side pacer + quota-aware retry. Google meters
the Keyword Planning methods separately from everything else ("1 request
per second per CID"). An LLM that fires several keyword-idea calls in one
turn trips that limit at once: reproduced on 2026-09-01 with 8 simultaneous
calls (5 rejected) while 4 simultaneous calls, and 4 paced calls, all passed;
the limiter behaves as a burst of about 4 in flight refilling about once a
second, so Google's "60 requests per 60 seconds" wording is not the rule. The rejection reaches
this process as a bare HTTP 429 ``google.api_core.exceptions.ResourceExhausted``
("Resource has been exhausted (e.g. check quota)"), NOT as a
``GoogleAdsException`` with a QuotaError, so the old ``except
GoogleAdsException`` never saw it and the model read the 429 as a daily cap.
This module now (a) spaces calls at least ``_MIN_INTERVAL_S`` apart inside
the server process, (b) retries either rejection shape after Google's
``retry_delay`` when one is sent and a short backoff otherwise, and (c)
reports the quota facts in the ToolError so the model can tell a
per-second rate limit from the daily operations quota.

Read-only — never mutates an ad account.
"""

import threading
import time
from typing import Any, Dict, List, Optional

from ads_mcp.coordinator import mcp
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations
import ads_mcp.utils as utils

from google.ads.googleads.errors import GoogleAdsException
from google.api_core import exceptions as api_exceptions
from fastmcp.exceptions import ToolError

# Google Ads API "API limits and quotas": KeywordPlanIdeaService methods
# are "limited to 1 request per second per CID". Pace slightly slower than
# that so clock skew between us and Google never lands two calls in one
# of Google's seconds.
_MIN_INTERVAL_S = 1.1

# GenerateKeywordIdeasRequest.keyword_seed accepts at most 20 keywords.
_MAX_SEEDS_PER_REQUEST = 20

# Retry budget for a quota rejection. Google's retry_delay for the planning
# rate limit is a few seconds; anything longer than _MAX_RETRY_WAIT_S is
# reported to the caller instead of blocking the MCP call.
_MAX_ATTEMPTS = 3
_DEFAULT_RETRY_WAIT_S = 2.0
_MAX_RETRY_WAIT_S = 30.0

_QUOTA_CODES = ("RESOURCE_EXHAUSTED", "RESOURCE_TEMPORARILY_EXHAUSTED")

# One pacer for the whole server process. Parallel tool calls from the
# model queue here instead of racing Google's per-second bucket.
_pacer_lock = threading.Lock()
_last_call_monotonic: Optional[float] = None


def _wait_for_slot() -> float:
    """Blocks until at least _MIN_INTERVAL_S has passed since the previous
    Keyword Planning call made by this process. Returns the seconds waited."""
    global _last_call_monotonic
    with _pacer_lock:
        waited = 0.0
        now = time.monotonic()
        if _last_call_monotonic is not None:
            remaining = _MIN_INTERVAL_S - (now - _last_call_monotonic)
            if remaining > 0:
                time.sleep(remaining)
                waited = remaining
        _last_call_monotonic = time.monotonic()
        return waited


def _quota_error(ex: GoogleAdsException) -> Optional[Dict[str, Any]]:
    """Returns the quota facts from a GoogleAdsException, or None when the
    failure is not a QuotaError.

    Keys: code (enum name), retry_delay_s (float or None), rate_scope
    (enum name or None), rate_name (str or None), message (str),
    shape ("google-ads-failure")."""
    failure = getattr(ex, "failure", None)
    for error in getattr(failure, "errors", None) or []:
        code = getattr(getattr(error, "error_code", None), "quota_error", None)
        name = getattr(code, "name", None)
        if name not in _QUOTA_CODES:
            continue
        info: Dict[str, Any] = {
            "code": name,
            "retry_delay_s": None,
            "rate_scope": None,
            "rate_name": None,
            "message": getattr(error, "message", "") or "",
            "shape": "google-ads-failure",
        }
        details = getattr(getattr(error, "details", None), "quota_error_details", None)
        if details is not None:
            delay = getattr(details, "retry_delay", None)
            if delay is not None:
                seconds = getattr(delay, "seconds", 0) or 0
                nanos = getattr(delay, "nanos", 0) or 0
                total = seconds + nanos / 1e9
                info["retry_delay_s"] = total if total > 0 else None
            scope = getattr(details, "rate_scope", None)
            info["rate_scope"] = getattr(scope, "name", None)
            rate_name = getattr(details, "rate_name", None)
            info["rate_name"] = (
                rate_name if isinstance(rate_name, str) and rate_name else None
            )
        return info
    return None


def _bare_429(ex: api_exceptions.ResourceExhausted) -> Dict[str, Any]:
    """The shape the planning rate limit actually arrives in (2026-09-01
    reproduction): an HTTP 429 from the API front end with no
    GoogleAdsFailure attached, so no rate name, scope or retry delay."""
    return {
        "code": "RESOURCE_EXHAUSTED",
        "retry_delay_s": None,
        "rate_scope": None,
        "rate_name": None,
        "message": str(ex),
        "shape": "http-429",
    }


def _looks_like_daily_quota(info: Dict[str, Any]) -> bool:
    """Google uses RESOURCE_EXHAUSTED for both the planning rate limit and
    the developer token's daily operations quota. A daily-quota rejection
    carries no short retry_delay and names a per-day / operations bucket.
    A bare 429 carries no name at all and is treated as the rate limit."""
    rate_name = (info.get("rate_name") or "").lower()
    if info.get("retry_delay_s"):
        return False
    return any(word in rate_name for word in ("day", "daily", "operations"))


def _quota_tool_error(
    info: Dict[str, Any], attempts: int, waited_s: float, request_id: str
) -> ToolError:
    facts = (
        f"{info['code']} ({info.get('shape')})"
        f"; Google's message: {info.get('message') or 'none'}"
        f"; rate: {info.get('rate_name') or 'not stated'}"
        f"; scope: {info.get('rate_scope') or 'not stated'}"
        f"; retry_delay: {info.get('retry_delay_s') if info.get('retry_delay_s') is not None else 'none'}"
        f"; attempts: {attempts}; waited {waited_s:.1f}s in total"
        f"; request ID: {request_id or 'not available'}"
    )
    if _looks_like_daily_quota(info):
        guidance = (
            "This is the developer token's DAILY operations quota (15,000 "
            "operations per day on Basic Access, shared by every user and app "
            "on the token, over a sliding 24 hour window). Do not retry now; report the "
            "quota facts above to the user."
        )
    else:
        guidance = (
            "This is the Keyword Planning RATE limit (1 request per second "
            "per customer account), not the daily quota. The server already "
            "paced calls 1 s apart and retried. Do not issue keyword-idea "
            "calls in parallel; call this tool once at a time, wait for the "
            "result, and put up to 20 seed keywords in one call instead of "
            "one call per seed. A rate rejection clears within seconds; only "
            "if a single call still fails after a 60 second pause is the daily "
            "operations quota (15,000 per sliding 24 hour window) exhausted."
        )
    return ToolError(
        f"Google Ads API quota error on GenerateKeywordIdeas: {facts}. {guidance}"
    )


def _chunks(items: List[str], size: int) -> List[List[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def generate_keyword_ideas(
    customer_id: str,
    seed_keywords: List[str],
    geo_target_ids: List[str],
    language_id: str = "1000",
    network: str = "GOOGLE_SEARCH",
    page_size: int = 1000,
) -> List[Dict[str, Any]]:
    """Generate keyword ideas with historical metrics via KeywordPlanIdeaService.

    Use this when a marketer needs search volume, competition level, and
    top-of-page CPC bid ranges for a list of seed keywords — typical
    use case is scoping a new client brief or expanding a keyword list
    during campaign planning.

    RATE LIMIT — READ BEFORE CALLING: Google allows 1 Keyword Planning
    request per second per customer account (about 4 in flight at once,
    then rejections). Never call this tool several
    times in parallel; call it once, wait for the result, then call again.
    Put up to 20 seed keywords in ONE call rather than one call per seed
    (more than 20 seeds are sent as consecutive batches of 20, paced 1 s
    apart, and the results are merged). The server paces calls and retries
    a rate rejection for you, so a returned error means the limit was hit
    persistently or the daily quota is gone — the error text says which.

    Args:
        customer_id: 10-digit Google Ads customer ID without dashes
            (e.g. "1234567890"). Must be a customer the OAuth user
            has access to — typically the MCC, or any child account.
            Prefer the client's own account for research: the planning
            rate limit is per customer account, so calls against the MCC
            share one bucket with every other tool and teammate using it.
        seed_keywords: list of seed terms to expand from. Example:
            ["criminal defense lawyer Calgary", "DUI lawyer Calgary"].
            Up to 20 per API request; longer lists are batched.
        geo_target_ids: list of geo target constant IDs (numeric strings).
            Common values:
              "2840" = United States
              "2124" = Canada
              "2826" = United Kingdom
              "2036" = Australia
              "2356" = India
            Pass multiple to broaden geographic scope, e.g.
            ["2124", "2840"] for Canada + US.
        language_id: language constant ID (numeric string). Defaults to
            "1000" = English. Other useful values:
              "1003" = Spanish
              "1001" = German
              "1002" = French
              "1019" = Portuguese
        network: which Google network to source data from. Options:
              "GOOGLE_SEARCH" (default, Search only)
              "GOOGLE_SEARCH_AND_PARTNERS" (includes Search Network partners)
        page_size: max keyword ideas to return. Default 1000 (KP's hard
            cap per call). Lowering doesn't save quota — each call is
            1 operation regardless of returned-row count.

    Returns: list of dicts, one per keyword idea. Each dict contains:
      - keyword_text:        str — the suggested keyword
      - avg_monthly_searches: int — historical avg monthly search volume
      - competition_level:    "LOW" | "MEDIUM" | "HIGH" | "UNKNOWN"
      - competition_index:    int 0-100, or None if KP didn't supply one
      - low_cpc:              float — top-of-page bid range low (currency
                              unit matches the customer_id's account currency)
      - high_cpc:             float — top-of-page bid range high

    Quota note: each GenerateKeywordIdeas request counts as 1 operation
    against the developer token's daily quota (15,000 per day on Basic
    Access, shared by everyone using the token). Typical keyword research
    uses well under 100 operations a day; the limit that actually bites
    is the per-second rate limit described above. The returned data is
    read-only — this tool never modifies any ad account.

    Errors: any Google Ads API error (e.g. invalid geo ID, missing
    permission on customer_id) is surfaced as a ToolError with the
    request ID and underlying error messages. Quota errors additionally
    carry Google's rate name, scope and retry delay when Google sends them.
    """
    seeds = [s.strip() for s in seed_keywords if isinstance(s, str) and s.strip()]
    if not seeds:
        raise ToolError("seed_keywords must contain at least one non-empty keyword.")

    kp_service = utils.get_googleads_service("KeywordPlanIdeaService")
    googleads_service = utils.get_googleads_service("GoogleAdsService")
    client = utils.get_googleads_client()

    clean_customer_id = customer_id.replace("-", "")
    merged: List[Dict[str, Any]] = []
    seen: set = set()

    for batch in _chunks(seeds, _MAX_SEEDS_PER_REQUEST):
        request = utils.get_googleads_type("GenerateKeywordIdeasRequest")
        request.customer_id = clean_customer_id
        request.language = googleads_service.language_constant_path(language_id)
        request.geo_target_constants.extend(
            [googleads_service.geo_target_constant_path(g) for g in geo_target_ids]
        )
        request.include_adult_keywords = False
        request.keyword_plan_network = client.enums.KeywordPlanNetworkEnum[network]
        request.page_size = page_size

        keyword_seed = utils.get_googleads_type("KeywordSeed")
        keyword_seed.keywords.extend(batch)
        request.keyword_seed = keyword_seed

        utils.logger.info(
            f"ads_mcp.generate_keyword_ideas customer={clean_customer_id} "
            f"seed={batch} geo={geo_target_ids} lang={language_id} "
            f"network={network} page_size={page_size}"
        )

        response = _call_with_pacing_and_retry(kp_service, request)
        for idea in response:
            m = idea.keyword_idea_metrics
            text = idea.text
            if text in seen:
                continue
            seen.add(text)
            merged.append(
                {
                    "keyword_text": text,
                    "avg_monthly_searches": int(m.avg_monthly_searches or 0),
                    "competition_level": (
                        m.competition.name if m.competition else "UNKNOWN"
                    ),
                    "competition_index": (
                        int(m.competition_index) if m.competition_index else None
                    ),
                    "low_cpc": (m.low_top_of_page_bid_micros or 0) / 1_000_000,
                    "high_cpc": (m.high_top_of_page_bid_micros or 0) / 1_000_000,
                }
            )
    return merged


def _call_with_pacing_and_retry(kp_service, request):
    """Runs one GenerateKeywordIdeas request through the pacer, retrying a
    quota rejection (either error shape) after Google's stated retry_delay
    when present, else a short backoff, within a bounded budget."""
    waited_total = 0.0
    last_quota: Optional[Dict[str, Any]] = None
    last_request_id = ""
    attempt = 0
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        waited_total += _wait_for_slot()
        try:
            return kp_service.generate_keyword_ideas(request=request)
        except GoogleAdsException as ex:
            quota = _quota_error(ex)
            if quota is None:
                error_msgs = [
                    f"Google Ads API Error: {error.message}"
                    for error in ex.failure.errors
                ]
                raise ToolError(
                    f"Request ID: {ex.request_id}\n" + "\n".join(error_msgs)
                )
            last_request_id = ex.request_id
        except api_exceptions.ResourceExhausted as ex:
            quota = _bare_429(ex)
            last_request_id = ""
        last_quota = quota
        if _looks_like_daily_quota(quota) or attempt == _MAX_ATTEMPTS:
            break
        delay = quota.get("retry_delay_s") or _DEFAULT_RETRY_WAIT_S * attempt
        if delay > _MAX_RETRY_WAIT_S:
            break
        utils.logger.warning(
            f"ads_mcp.generate_keyword_ideas quota {quota['code']} "
            f"({quota.get('shape')}, rate={quota.get('rate_name')}, "
            f"scope={quota.get('rate_scope')}); retrying in {delay:.1f}s "
            f"(attempt {attempt}/{_MAX_ATTEMPTS})"
        )
        time.sleep(delay)
        waited_total += delay
    assert last_quota is not None
    raise _quota_tool_error(last_quota, attempt, waited_total, last_request_id)


# Register with FastMCP. Mirrors the search-tool registration pattern in
# tools/search.py. readOnlyHint=True lets MCP-aware clients (Claude
# Desktop) display this as a read-only tool, no consent prompt for
# mutation-style risk.
mcp.add_tool(
    Tool.from_function(
        generate_keyword_ideas,
        annotations=ToolAnnotations(readOnlyHint=True),
    )
)
