# Copyright 2026 Google LLC.
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

"""Tools for exposing simple, core API methods to the MCP server."""

from typing import Any, Dict, List, Optional
from ads_mcp.coordinator import mcp
from mcp.types import ToolAnnotations
from fastmcp.exceptions import ToolError

import ads_mcp.utils as utils

from google.ads.googleads.errors import GoogleAdsException
from google.api_core import exceptions as api_exceptions
from google.ads.googleads.v24.services.types.customer_service import (
    ListAccessibleCustomersResponse,
)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_accessible_customers() -> List[str]:
    """Returns customer IDs where the authenticating user has DIRECT access.

    IMPORTANT — what this does NOT return: customers linked through a Manager
    (MCC) account via the manager hierarchy. If the authenticating user is a
    member of an MCC but does not have a direct user grant on each child
    account, those child accounts will NOT appear here. The result count will
    be much smaller than "all accounts under the MCC."

    For "show me every account under our MCC" use `list_customer_clients`
    instead — that one walks the manager hierarchy via the `customer_client`
    resource and returns every linked account (including ones inherited
    through MCC access).

    Use this tool when you specifically need the list of customers the
    OAuth user has direct user-level access on (e.g., to confirm the
    refresh-token user is set up correctly).

    Returns:
        List[str]: A list of customer IDs (no `customers/` prefix).
    """
    ga_service = utils.get_googleads_service("CustomerService")
    accessible_customers: ListAccessibleCustomersResponse = (
        ga_service.list_accessible_customers()
    )
    # remove customer/ from the start of each resource
    return [
        cust_rn.removeprefix("customers/")
        for cust_rn in accessible_customers.resource_names
    ]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_customer_clients(
    manager_customer_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List every customer account linked under a Manager (MCC) account.

    Use this tool — NOT `list_accessible_customers` — whenever the user asks
    for "all client accounts under our MCC," "every account we manage," or
    similar. It walks the manager hierarchy via the `customer_client`
    resource so child accounts inherited through MCC access are included.

    Internally runs:
        SELECT customer_client.id,
               customer_client.descriptive_name,
               customer_client.status,
               customer_client.manager,
               customer_client.test_account,
               customer_client.level,
               customer_client.currency_code,
               customer_client.time_zone
        FROM customer_client
        WHERE customer_client.status = 'ENABLED'

    Args:
        manager_customer_id: 10-digit MCC ID, no dashes. If omitted, defaults
            to the GOOGLE_ADS_LOGIN_CUSTOMER_ID environment variable the
            server was launched with (the org's primary MCC).

    Returns:
        List[Dict]: one entry per child account with keys
            `customer_client.id`, `customer_client.descriptive_name`,
            `customer_client.status`, `customer_client.manager`,
            `customer_client.test_account`, `customer_client.level`,
            `customer_client.currency_code`, `customer_client.time_zone`.
            `manager=true` rows represent nested sub-MCCs.

    Raises:
        ToolError: if no manager_customer_id is provided AND
            GOOGLE_ADS_LOGIN_CUSTOMER_ID is unset, or if the Google Ads
            API call fails.
    """
    cid = manager_customer_id or utils._get_login_customer_id()
    if not cid:
        raise ToolError(
            "No manager_customer_id provided and GOOGLE_ADS_LOGIN_CUSTOMER_ID"
            " is not set in the environment. Provide the MCC ID explicitly"
            " (10 digits, no dashes)."
        )

    ga_service = utils.get_googleads_service("GoogleAdsService")
    query = (
        "SELECT customer_client.id, customer_client.descriptive_name, "
        "customer_client.status, customer_client.manager, "
        "customer_client.test_account, customer_client.level, "
        "customer_client.currency_code, customer_client.time_zone "
        "FROM customer_client "
        "WHERE customer_client.status = 'ENABLED'"
    )

    try:
        result_stream = ga_service.search_stream(customer_id=cid, query=query)
        output: List[Dict[str, Any]] = []
        for batch in result_stream:
            for row in batch.results:
                output.append(
                    utils.format_output_row(row, batch.field_mask.paths)
                )
        return output
    except GoogleAdsException as ex:
        error_msgs = [
            f"Google Ads API Error: {error.message}"
            for error in ex.failure.errors
        ]
        raise ToolError(
            f"Request ID: {ex.request_id}\n" + "\n".join(error_msgs)
        )
    except api_exceptions.TooManyRequests as ex:
        # A quota rejection never arrives as a GoogleAdsException; see
        # utils.quota_tool_error_message.
        raise ToolError(utils.quota_tool_error_message(ex))
