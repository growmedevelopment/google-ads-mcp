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

Read-only — never mutates an ad account.
"""

from typing import Any, Dict, List

from ads_mcp.coordinator import mcp
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations
import ads_mcp.utils as utils

from google.ads.googleads.errors import GoogleAdsException
from fastmcp.exceptions import ToolError


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

    Args:
        customer_id: 10-digit Google Ads customer ID without dashes
            (e.g. "1234567890"). Must be a customer the OAuth user
            has access to — typically the MCC, or any child account.
        seed_keywords: list of seed terms to expand from. Example:
            ["criminal defense lawyer Calgary", "DUI lawyer Calgary"].
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

    Quota note: GenerateKeywordIdeas counts as 1 operation against the
    developer-token's daily quota. Basic Access provides 15,000
    ops/day, so even heavy keyword research workflows stay well under
    cap. The returned data is read-only — this tool never modifies any
    ad account.

    Errors: any Google Ads API error (e.g. invalid geo ID, missing
    permission on customer_id) is surfaced as a ToolError with the
    request ID and underlying error messages.
    """
    kp_service = utils.get_googleads_service("KeywordPlanIdeaService")
    googleads_service = utils.get_googleads_service("GoogleAdsService")
    client = utils.get_googleads_client()

    request = utils.get_googleads_type("GenerateKeywordIdeasRequest")
    request.customer_id = customer_id.replace("-", "")
    request.language = googleads_service.language_constant_path(language_id)
    request.geo_target_constants.extend(
        [
            googleads_service.geo_target_constant_path(g)
            for g in geo_target_ids
        ]
    )
    request.include_adult_keywords = False
    request.keyword_plan_network = client.enums.KeywordPlanNetworkEnum[network]
    request.page_size = page_size

    keyword_seed = utils.get_googleads_type("KeywordSeed")
    keyword_seed.keywords.extend(seed_keywords)
    request.keyword_seed = keyword_seed

    utils.logger.info(
        f"ads_mcp.generate_keyword_ideas customer={request.customer_id} "
        f"seed={seed_keywords} geo={geo_target_ids} lang={language_id} "
        f"network={network} page_size={page_size}"
    )

    try:
        response = kp_service.generate_keyword_ideas(request=request)
        out: List[Dict[str, Any]] = []
        for idea in response:
            m = idea.keyword_idea_metrics
            out.append(
                {
                    "keyword_text": idea.text,
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
                    "high_cpc": (
                        m.high_top_of_page_bid_micros or 0
                    ) / 1_000_000,
                }
            )
        return out
    except GoogleAdsException as ex:
        error_msgs = [
            f"Google Ads API Error: {error.message}"
            for error in ex.failure.errors
        ]
        raise ToolError(
            f"Request ID: {ex.request_id}\n" + "\n".join(error_msgs)
        )


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
