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

"""Module declaring the singleton MCP instance.

The singleton allows other modules to register their tools with the same MCP
server using `@mcp.tool` annotations, thereby 'coordinating' the bootstrapping
of the server.
"""

import logging
import os
from typing import Any
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.dependencies import extract_version_spec
from fastmcp.server.middleware import Middleware
from fastmcp.server.providers.fastmcp_provider import FastMCPProviderTool
from fastmcp_tasks import TasksExtension
from mcp import types as mcp_types
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler
from ads_mcp.audit import AuditMiddleware
from ads_mcp.auth_storage import (
    create_audit_storage,
    create_client_storage,
    get_audit_storage_ttl,
)

logger = logging.getLogger(__name__)

_CLIENT_ID = os.environ.get("GOOGLE_ADS_MCP_OAUTH_CLIENT_ID")
_CLIENT_SECRET = os.environ.get("GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET")
_BASE_URL = os.environ.get("GOOGLE_ADS_MCP_BASE_URL", "http://localhost:8080")
_JWT_SIGNING_KEY = os.environ.get("GOOGLE_ADS_MCP_JWT_SIGNING_KEY")

if _CLIENT_ID and _CLIENT_SECRET:
    client_storage = create_client_storage()
    provider_kwargs: dict[str, Any] = {
        "client_id": _CLIENT_ID,
        "client_secret": _CLIENT_SECRET,
        "base_url": _BASE_URL,
        "required_scopes": [
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
            "https://www.googleapis.com/auth/adwords",
        ],
    }
    if _JWT_SIGNING_KEY:
        provider_kwargs["jwt_signing_key"] = _JWT_SIGNING_KEY
    if client_storage is not None:
        provider_kwargs["client_storage"] = client_storage

    auth = GoogleProvider(**provider_kwargs)
    mcp = FastMCP("Google Ads Server", auth=auth)
else:
    mcp = FastMCP("Google Ads Server")

mcp.add_extension(TasksExtension())
mcp.add_middleware(
    AuditMiddleware(
        storage=create_audit_storage(),
        ttl=get_audit_storage_ttl(),
        server=mcp,
    )
)


def ensure_subscriptions_listen(server: FastMCP) -> bool:
    """Register the MCP 2026 subscription stream when FastMCP omits it.

    FastMCP 4 (including 4.0.3) builds its low-level ``Server`` without the SDK's
    ``on_subscriptions_listen`` handler. Some modern clients open that stream
    while loading tools; without it, the SDK returns HTTP 404 / method not
    found, which mcp-go reports misleadingly as a missing session.

    The guard preserves a native FastMCP implementation once one is shipped.

    Returns:
        True when the compatibility handler was installed, otherwise False.
    """
    low_level_server = server._mcp_server
    if "subscriptions/listen" in low_level_server._request_handlers:
        return False

    subscription_bus = InMemorySubscriptionBus()
    low_level_server.add_request_handler(
        "subscriptions/listen",
        mcp_types.SubscriptionsListenRequestParams,
        ListenHandler(subscription_bus),
    )
    return True


ensure_subscriptions_listen(mcp)


def _sub_server_tools(sub_mcp: FastMCP) -> list[Any]:
    """Every registered tool object on a sub-server, all versions (GrowME fork)."""
    return [
        val
        for key, val in list(sub_mcp.local_provider._components.items())
        if key.startswith("tool:")
    ]


def _hint(annotations: Any, new_name: str, old_name: str) -> Any:
    """Reads an MCP tool hint under its SDK v2 name, falling back to v1's."""
    fields = getattr(type(annotations), "model_fields", {}) or {}
    if new_name in fields:
        return getattr(annotations, new_name, None)
    return getattr(annotations, old_name, None)


def _is_read_only(tool: Any) -> bool:
    """True only for a tool that declares readOnlyHint=True and is not
    declared destructive (GrowME fork)."""
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return False
    if _hint(annotations, "read_only_hint", "readOnlyHint") is not True:
        return False
    return _hint(annotations, "destructive_hint", "destructiveHint") is not True


class ReadOnlyGate(Middleware):
    """GrowME fork: the server-wide half of the read-only guarantee.

    The mount-time filter in initialize_and_mount_tools only sees tools that
    sub-servers registered before mounting. This middleware binds every name
    it exposes to the exact tool object that was mounted for it: the
    sub-server, the tool's own name there, and the object itself. tools/list
    shows, and tools/call runs, a tool only when what FastMCP resolves for that
    name is FastMCP's own mounted-tool wrapper around that same read-only
    object. Calls that select a tool version are refused, because this fork
    serves no versioned tools and FastMCP runs the version a request names.
    A FastMCP transform applied to a bundled tool (even a description change
    or a namespace) changes what FastMCP resolves, so that tool then refuses
    calls: the gate fails closed rather than trusting a rewritten tool.

    Scope: this protects against MCP clients (any request, name, version,
    method or task path) and against stray registrations (tools added on the
    parent, on a sub-server after mounting, through nested servers, providers,
    transforms or look-alike subclasses). It does not defend against
    malicious code running inside this server process (middleware that
    rewrites requests after the gate, edits to a tool object or monkeypatched
    lookups): code in the process is trusted, as with any installed package.
    """

    def __init__(self, bound: dict[str, tuple[FastMCP, str, Any]]):
        # exposed name -> (sub-server, name on the sub-server, tool object)
        self.bound = dict(bound)

    async def _is_bound(self, name: str, tool: Any) -> bool:
        entry = self.bound.get(name)
        if entry is None or tool is None or not _is_read_only(tool):
            return False
        sub_mcp, original_name, original_tool = entry
        # Exact type, not isinstance: a subclass can copy the two attributes
        # below and carry its own body.
        if type(tool) is not FastMCPProviderTool:
            return False
        if getattr(tool, "_server", None) is not sub_mcp:
            return False
        if getattr(tool, "_original_name", None) != original_name:
            return False
        current = await sub_mcp.get_tool(original_name)
        return current is original_tool and _is_read_only(current)

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        return [t for t in tools if await self._is_bound(t.name, t)]

    async def on_call_tool(self, context, call_next):
        name = context.message.name
        if name not in self.bound:
            raise ToolError(f"Unknown tool: {name!r}")
        if extract_version_spec(getattr(context.message, "meta", None)):
            raise ToolError(
                "Tool versions are not served by this read-only server."
            )
        server = getattr(context.fastmcp_context, "fastmcp", None)
        tool = await server.get_tool(name) if server is not None else None
        if not await self._is_bound(name, tool):
            raise ToolError(
                f"Tool {name!r} is not served by this read-only server."
            )
        return await call_next(context)


def initialize_and_mount_tools(parent_mcp: FastMCP) -> None:
    """Loads the tools configuration and dynamically mounts the tools sub-servers."""
    from ads_mcp.config import ToolsConfig
    import importlib
    import pkgutil
    import ads_mcp.tools as tools_pkg

    # Map of category name -> FastMCP sub-server
    sub_servers = {}

    # Discover and dynamically load all tool modules
    for _, module_name, _ in pkgutil.iter_modules(tools_pkg.__path__):
        full_module_name = f"ads_mcp.tools.{module_name}"
        module = importlib.import_module(full_module_name)

        # Find any FastMCP instances defined in the module
        for attr_name in dir(module):
            attr_val = getattr(module, attr_name)
            if isinstance(attr_val, FastMCP):
                category = attr_val.name
                if (
                    category in sub_servers
                    and sub_servers[category] is not attr_val
                ):
                    raise ValueError(
                        f"Conflicting FastMCP sub-servers registered for category '{category}'."
                    )
                sub_servers[category] = attr_val

    config = ToolsConfig.load()
    bound: dict[str, tuple[FastMCP, str, Any]] = {}

    for category, sub_mcp in sub_servers.items():
        if not config.is_namespace_enabled(category):
            continue

        # Filter disabled tools inside the sub-server before mounting
        tool_names = []
        for key, val in sub_mcp.local_provider._components.items():
            if key.startswith("tool:"):
                tool_names.append(val.name)

        for name in tool_names:
            if not config.is_tool_enabled(category, name):
                sub_mcp.local_provider.remove_tool(name)

        # GrowME fork: read-only guarantee. Whatever the tools config says,
        # a tool that does not declare readOnlyHint=True (or is declared
        # destructive) is never mounted, so upstream's write tools (labels,
        # assets) cannot be switched on by a config file or
        # GOOGLE_ADS_MCP_TOOLS_CONFIG. Every version is checked; a name with
        # any writable version is dropped whole (fails closed).
        unsafe = sorted(
            {t.name for t in _sub_server_tools(sub_mcp) if not _is_read_only(t)}
        )
        for name in unsafe:
            logger.warning(
                "Not mounting tool '%s' (category '%s'): it is not read-only, "
                "and this fork serves read-only tools only.",
                name,
                category,
            )
            sub_mcp.local_provider.remove_tool(name)

        # Determine prefix/namespace
        namespace_prefix = config.get_namespace_prefix(category)

        # Instrument background-task tools on this sub-server for worker auditing
        for mw in parent_mcp.middleware:
            if isinstance(mw, AuditMiddleware):
                mw.instrument_server(sub_mcp, namespace=namespace_prefix)

        # Mount the sub-server
        parent_mcp.mount(sub_mcp, namespace=namespace_prefix or None)
        for tool in _sub_server_tools(sub_mcp):
            exposed = (
                f"{namespace_prefix}_{tool.name}"
                if namespace_prefix
                else tool.name
            )
            bound[exposed] = (sub_mcp, tool.name, tool)

    # GrowME fork: freeze what was mounted and gate list/call on it.
    parent_mcp.add_middleware(ReadOnlyGate(bound))


# Automatically initialize and mount tools upon import
initialize_and_mount_tools(mcp)
