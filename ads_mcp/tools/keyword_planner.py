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
The mechanism is in the client library: ``Interceptor._get_error_from_response``
short-circuits gRPC RESOURCE_EXHAUSTED (it sits in ``_RETRY_STATUS_CODES``)
and returns the raw RpcError without building a GoogleAdsException, which
``wrap_method`` then remaps to ``api_core`` ``ResourceExhausted``. Whether
Google still attaches a GoogleAdsFailure in the trailing metadata was never
observed, so this module now tries to recover one and logs the metadata keys
when it cannot — one real rejection will settle it.
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
# NOTE: request/type construction is hoisted out of the batch loop below,
# because utils.get_googleads_type() builds a fresh GoogleAdsClient (and
# credentials) on every call.
_MAX_SEEDS_PER_REQUEST = 20

# Retry budget for a quota rejection. Google's retry_delay for the planning
# rate limit is a few seconds; anything longer than _MAX_RETRY_WAIT_S is
# reported to the caller instead of blocking the MCP call.
_MAX_ATTEMPTS = 3
_DEFAULT_RETRY_WAIT_S = 2.0
_MAX_RETRY_WAIT_S = 30.0

_QUOTA_CODES = ("RESOURCE_EXHAUSTED", "RESOURCE_TEMPORARILY_EXHAUSTED")

# Separate indirections for the two kinds of wait, so a test can tell a pacing
# gap from a retry backoff without patching stdlib time.sleep process-wide.
_pacer_sleep = time.sleep
_retry_sleep = time.sleep

# Hard ceiling on how long a caller may queue behind the pacer. FastMCP runs
# sync tools on a 40-slot worker pool, so an unbounded queue would starve the
# server's other tools; past this the caller is told to serialize instead.
_MAX_PACER_QUEUE_S = 10.0

# Upper bound on rows returned, so a broad seed set cannot dump an unbounded
# page-walk into the model's context.
_MAX_RESULTS = 2000

# The limit Google enforces is per customer ID, so the pacer is keyed on the
# customer id rather than being one global gate: research across several
# client accounts is not throttled against itself. Parallel tool calls from
# the model queue here instead of racing Google's per-second bucket.
_pacer_lock = threading.Lock()
_last_call_monotonic: Dict[str, float] = {}
_MAX_TRACKED_CUSTOMERS = 256


def _wait_for_slot(customer_id: str) -> float:
    """Blocks until at least _MIN_INTERVAL_S has passed since this process's
    previous Keyword Planning call against `customer_id`. Returns the seconds
    waited. Raises ToolError rather than queueing longer than
    _MAX_PACER_QUEUE_S, so a fan-out cannot pin the whole worker pool."""
    with _pacer_lock:
        now = time.monotonic()
        previous = _last_call_monotonic.get(customer_id)
        remaining = 0.0
        if previous is not None:
            remaining = _MIN_INTERVAL_S - (now - previous)
        if remaining > _MAX_PACER_QUEUE_S:
            raise ToolError(
                "Too many keyword-idea calls are already queued for customer "
                f"{customer_id} (next slot is {remaining:.1f}s away). Call this "
                "tool once at a time and put up to 20 seed keywords in each "
                "call instead of issuing them in parallel."
            )
        if remaining > 0:
            _pacer_sleep(remaining)
        else:
            remaining = 0.0
        if len(_last_call_monotonic) > _MAX_TRACKED_CUSTOMERS:
            _last_call_monotonic.clear()
        _last_call_monotonic[customer_id] = time.monotonic()
        return remaining


def _quota_error(ex: GoogleAdsException) -> Optional[Dict[str, Any]]:
    """Returns the quota facts from a GoogleAdsException, or None when the
    failure is not a QuotaError.

    Keys: code (enum name), retry_delay_s (float or None), rate_scope
    (enum name or None), rate_name (str or None), message (str),
    shape ("google-ads-failure")."""
    return _quota_error_from_failure(getattr(ex, "failure", None))


def _quota_error_from_failure(failure) -> Optional[Dict[str, Any]]:
    """Shared parser: pulls quota facts out of a GoogleAdsFailure, whether it
    arrived on a GoogleAdsException or was recovered from trailing metadata."""
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
        details = getattr(
            getattr(error, "details", None), "quota_error_details", None
        )
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


def _quota_from_trailing_metadata(ex) -> Optional[Dict[str, Any]]:
    """Recovers the QuotaError details Google sent but the client library
    discarded.

    `Interceptor._get_error_from_response` short-circuits gRPC
    RESOURCE_EXHAUSTED (it is in `_RETRY_STATUS_CODES`) and returns the raw
    RpcError WITHOUT building a GoogleAdsException, so a quota rejection
    never reaches the `except GoogleAdsException` path even when Google
    attached a perfectly good GoogleAdsFailure. `from_grpc_error` keeps the
    original error on `ex.response`, so the trailing metadata survives and
    the failure can be deserialized back out of it.

    Returns the same dict shape as `_quota_error`, or None when no failure
    rides along (in which case the caller falls back to `_bare_429`)."""
    response = getattr(ex, "response", None)
    trailing = getattr(response, "trailing_metadata", None)
    if not callable(trailing):
        return None
    try:
        entries = list(trailing() or [])
    except Exception:  # defensive: metadata access must never mask the 429
        return None
    keys = [k for k, _ in entries]
    utils.logger.warning(
        "ads_mcp.generate_keyword_ideas quota rejection trailing-metadata "
        f"keys={keys}"
    )
    for key, value in entries:
        if not key.endswith("googleadsfailure-bin"):
            continue
        try:
            failure_type = utils.get_googleads_type("GoogleAdsFailure")
            failure = type(failure_type).deserialize(value)
        except Exception:
            return None
        info = _quota_error_from_failure(failure)
        if info is not None:
            info["shape"] = "http-429+failure"
            return info
    return None


def _bare_429(ex: api_exceptions.ResourceExhausted) -> Dict[str, Any]:
    """Fallback shape for a quota rejection that carries no GoogleAdsFailure.

    Google answers BOTH the Keyword Planning per-second limit and the
    developer token's daily operations cap with RESOURCE_EXHAUSTED, so when
    no failure detail rides along there is genuinely nothing in the response
    that distinguishes them and the caller must say so rather than guess."""
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
    the developer token's daily operations quota, so the response has to be
    read carefully.

    `rate_scope` is the field that actually separates them: the proto
    documents ACCOUNT as "Per customer account quota" and DEVELOPER as "Per
    project or DevToken quota" — DEVELOPER being the daily bucket. It is
    checked first. `rate_name` is free text whose documented examples
    ("Requests per account", "Get requests for standard access") contain
    none of the words a naive substring match would look for, so it is only
    a fallback and only on an explicit day/daily token; the bare word
    "operations" appears in per-minute buckets too and is not sufficient."""
    scope = (info.get("rate_scope") or "").upper()
    if scope == "DEVELOPER":
        return True
    if scope == "ACCOUNT":
        return False
    rate_name = (info.get("rate_name") or "").lower()
    if info.get("retry_delay_s"):
        return False
    return any(word in rate_name for word in ("day", "daily", "per-day"))


def _is_ambiguous_quota(info: Dict[str, Any]) -> bool:
    """True when Google sent nothing that separates the two limits, so the
    error must report the ambiguity rather than assert either one."""
    return info.get("shape") == "http-429" and not info.get("rate_scope")


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
            "on the token, over a sliding 24 hour window). Do not retry now; "
            "report the quota facts above to the user."
        )
    elif _is_ambiguous_quota(info):
        guidance = (
            "Google answers BOTH the Keyword Planning rate limit (1 request "
            "per second per customer account) and the developer token's daily "
            "operations quota with this same code, and sent no rate name or "
            "scope here, so which one it was cannot be read off this response. "
            "The rate limit is far likelier: it is the one a burst of parallel "
            "calls trips, and it clears in seconds. Stop issuing keyword-idea "
            "calls in parallel, put up to 20 seeds in one call, and try once "
            "more after a pause. If a single call still fails after 60 seconds "
            "of quiet, treat the daily quota (15,000 operations per sliding "
            "24 hours) as the likely cause and report that to the user."
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
        page_size: rows per PAGE, not a cap on the result (the API's
            documented maximum is 10,000). The tool walks every page and
            merges them, so a broad seed set can return a lot; results are
            truncated at 2,000 rows and a warning is logged when that fires.
            Each page is its own request, so it costs its own operation and
            its own slot against the per-second limit — a smaller page_size
            means MORE requests, not fewer.

    Returns: list of dicts, one per keyword idea. Each dict contains:
      - keyword_text:        str — the suggested keyword
      - avg_monthly_searches: int — historical avg monthly search volume
      - competition_level:    "LOW" | "MEDIUM" | "HIGH" | "UNKNOWN"
      - competition_index:    int 0-100, or None if KP didn't supply one
      - low_cpc:              float — top-of-page bid range low (currency
                              unit matches the customer_id's account currency)
      - high_cpc:             float — top-of-page bid range high

    Quota note: each GenerateKeywordIdeas REQUEST counts as 1 operation
    against the developer token's daily quota (15,000 per sliding 24 hours
    on Basic Access, shared by everyone using the token), and a paged
    response costs one operation per page. Typical keyword research uses
    well under 100 operations a day; the limit that actually bites is the
    per-second rate limit described above. The returned data is
    read-only — this tool never modifies any ad account.

    Errors: any Google Ads API error (e.g. invalid geo ID, missing
    permission on customer_id) is surfaced as a ToolError with the
    request ID and underlying error messages. Quota errors additionally
    carry Google's rate name, scope and retry delay when Google sends them.
    """
    seeds = [
        s.strip() for s in seed_keywords if isinstance(s, str) and s.strip()
    ]
    if not seeds:
        raise ToolError(
            "seed_keywords must contain at least one non-empty keyword."
        )

    kp_service = utils.get_googleads_service("KeywordPlanIdeaService")
    googleads_service = utils.get_googleads_service("GoogleAdsService")
    client = utils.get_googleads_client()

    clean_customer_id = customer_id.replace("-", "")
    merged: List[Dict[str, Any]] = []
    seen: set = set()

    request_type = type(utils.get_googleads_type("GenerateKeywordIdeasRequest"))
    seed_type = type(utils.get_googleads_type("KeywordSeed"))

    for batch in _chunks(seeds, _MAX_SEEDS_PER_REQUEST):
        request = request_type()
        request.customer_id = clean_customer_id
        request.language = googleads_service.language_constant_path(language_id)
        request.geo_target_constants.extend(
            [
                googleads_service.geo_target_constant_path(g)
                for g in geo_target_ids
            ]
        )
        request.include_adult_keywords = False
        request.keyword_plan_network = client.enums.KeywordPlanNetworkEnum[
            network
        ]
        request.page_size = page_size

        keyword_seed = seed_type()
        keyword_seed.keywords.extend(batch)
        request.keyword_seed = keyword_seed

        utils.logger.info(
            f"ads_mcp.generate_keyword_ideas customer={clean_customer_id} "
            f"seed={batch} geo={geo_target_ids} lang={language_id} "
            f"network={network} page_size={page_size}"
        )

        ideas = _call_with_pacing_and_retry(
            kp_service, request, clean_customer_id
        )
        for idea in ideas:
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
                        int(m.competition_index)
                        if m.competition_index
                        else None
                    ),
                    "low_cpc": (m.low_top_of_page_bid_micros or 0) / 1_000_000,
                    "high_cpc": (m.high_top_of_page_bid_micros or 0)
                    / 1_000_000,
                }
            )
            if len(merged) >= _MAX_RESULTS:
                utils.logger.warning(
                    "ads_mcp.generate_keyword_ideas truncated at "
                    f"{_MAX_RESULTS} rows"
                )
                return merged
    return merged


def _call_with_pacing_and_retry(kp_service, request, customer_id: str):
    """Returns every keyword idea for `request`, with the pacer and the quota
    retry applied to EACH underlying RPC.

    `generate_keyword_ideas` hands back a lazy pager whose iteration issues a
    fresh RPC per `next_page_token`, back-to-back. Returning that pager would
    leave every page after the first outside the pacer, outside the retry and
    outside the ToolError conversion — firing inside the same Google second
    the pacer exists to protect, and letting a bare 429 escape raw.

    Driving the pager's own generator is not enough either: once a page fetch
    raises, that generator is finished, so a retry would silently resume at
    "no more pages" and truncate the result. So paging is done explicitly with
    `page_token`, which makes every page an independent, retryable RPC."""
    ideas: List[Any] = []
    while True:
        response = _one_rpc_with_retry(
            lambda: kp_service.generate_keyword_ideas(request=request),
            customer_id,
        )
        results = getattr(response, "results", None)
        if results is None:
            # Not a paged response (a plain iterable, or a test double).
            return list(response)
        ideas.extend(list(results))
        token = getattr(response, "next_page_token", "") or ""
        if not token or len(ideas) >= _MAX_RESULTS:
            return ideas
        request.page_token = token


def _one_rpc_with_retry(call, customer_id: str, skip_first_wait: bool = False):
    """Runs a single Keyword Planning RPC through the pacer, retrying a quota
    rejection (either error shape) after Google's stated retry_delay when
    present, else a short backoff, within a bounded budget."""
    waited_total = 0.0
    last_quota: Optional[Dict[str, Any]] = None
    last_request_id = ""
    attempt = 0
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        waited_total += _wait_for_slot(customer_id)
        try:
            return call()
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
        except api_exceptions.TooManyRequests as ex:
            # ResourceExhausted subclasses TooManyRequests; catch the wider
            # class so an HTTP-flavoured 429 is handled the same way.
            quota = _quota_from_trailing_metadata(ex) or _bare_429(ex)
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
        _retry_sleep(delay)
        waited_total += delay
    if last_quota is None:  # pragma: no cover - only if _MAX_ATTEMPTS < 1
        raise ToolError(
            "generate_keyword_ideas made no attempt; _MAX_ATTEMPTS is "
            f"{_MAX_ATTEMPTS}, which must be at least 1."
        )
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
