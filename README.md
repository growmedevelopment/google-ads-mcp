# Google Ads MCP Server

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server that enables LLMs and AI agents to query and manage [Google Ads](https://developers.google.com/google-ads/api) accounts.

> **This is GrowME's fork of [googleads/google-ads-mcp](https://github.com/googleads/google-ads-mcp), and it is read-only.**
> Differences from upstream:
> - Two extra read-only tools: `list_customer_clients` (every account under a manager/MCC account, via the `customer_client` resource) and `generate_keyword_ideas` (Keyword Planner ideas with historical metrics, paced and quota-aware).
> - The bundled `tools_config.yaml` mounts the read namespaces **without a prefix** (`search`, `get_resource_metadata`, `list_accessible_customers`, `list_customer_clients`, `generate_keyword_ideas`) and turns upstream's mutate namespaces (`labels`, `assets`) off.
> - `ads_mcp/coordinator.py` refuses to mount any tool that does not declare `readOnlyHint=True`, whatever the config file or `GOOGLE_ADS_MCP_TOOLS_CONFIG` says, and a `ReadOnlyGate` middleware serves only the exact tools mounted at startup (no tool versions). The mutate tools and the Propose & Confirm workflow described below therefore never reach a client of this fork. Code running inside the server process is trusted and out of scope.
> - Hosted (Streamable HTTP) deployments should set `GOOGLE_ADS_MCP_ALLOWED_CLIENT_REDIRECT_URIS` to the MCP clients they serve (see the environment variables below); otherwise any website can register as a client.
> - A `tools_config.yaml` in the directory the server is launched from replaces the bundled one (upstream behaviour). It cannot enable a write tool here, but it can change which of the read-only tools appear, so launch the server from a directory without one.
> - Install it once (`pipx install git+https://github.com/growmedevelopment/google-ads-mcp.git`, or `uv tool install`) and point your client at the installed executable; the `git+https` source needs the `git` command-line tool. Do not launch it with `pipx run --spec git+…` or `uvx --from git+…`: on a cache miss those rebuild from GitHub before the server answers `initialize` (about 30 s), and some clients time out.
> - Report problems with this fork on [its issue tracker](https://github.com/growmedevelopment/google-ads-mcp/issues); bugs in upstream's own code belong [upstream](https://github.com/googleads/google-ads-mcp/issues).

## Table of Contents

- [Overview](#overview)
- [Quickstart & Setup](#quickstart--setup)
  - [1. Prerequisites](#1-prerequisites)
  - [2. Choose an Authentication Mode](#2-choose-an-authentication-mode)
  - [3. Configure Your MCP Client](#3-configure-your-mcp-client)
- [Tools & Resources](#tools--resources)
  - [Available Tools](#available-tools)
  - [Available Resources](#available-resources)
- [Configuration & Security](#configuration--security)
  - [Propose & Confirm Safety Workflow](#propose--confirm-safety-workflow)
  - [Configuring and Namespacing Tools](#configuring-and-namespacing-tools)
  - [Tool Call Auditability](#tool-call-auditability)
  - [Environment Variables Reference](#environment-variables-reference)
- [Deployment](#deployment)
  - [Local Container Deployment (Docker / Podman)](#local-container-deployment-docker--podman)
  - [Google Cloud Run Deployment](#google-cloud-run-deployment)
- [Try It Out](#try-it-out)
- [Contributing](#contributing)

## Overview

The Google Ads MCP Server exposes [MCP Tools](https://modelcontextprotocol.io/docs/concepts/tools) and [MCP Resources](https://modelcontextprotocol.io/docs/concepts/resources) backed by the [Google Ads API](https://developers.google.com/google-ads/api/reference/rpc/latest/overview). It supports:

- **Read & reporting workflows**: GAQL queries, accessible customer discovery, resource field metadata, and API reference resources.
- **Guarded account mutations**: Asset creation, video uploads, and label management protected by a two-step **Propose & Confirm** workflow.
- **Flexible deployment**: Run locally over `stdio` with Application Default Credentials, or over **Streamable HTTP** with FastMCP's OAuth proxy and structured audit logging.

> **Notes:**
> - Connecting an LLM or agent to this server exposes your accessible Google Ads account data to that client.
> - API calls made by this server include an MCP telemetry header used by Google to collect usage statistics and improve the product.
> - Report bugs or feature requests for this fork on [its GitHub issue tracker](https://github.com/growmedevelopment/google-ads-mcp/issues).

---

## Quickstart & Setup

### 1. Prerequisites

1. **Python & `pipx`** (for local non-container setups): Python 3.11+, [`pipx`](https://pipx.pypa.io/stable/#install-pipx) (or `uv`) and the `git` command-line tool (Windows: [Git for Windows](https://git-scm.com/download/win); macOS: `xcode-select --install`). Install this fork once:
   ```shell
   pipx install git+https://github.com/growmedevelopment/google-ads-mcp.git
   # or: uv tool install --python 3.12 git+https://github.com/growmedevelopment/google-ads-mcp.git
   ```
   Then find the installed executable for your client config: `which google-ads-mcp` (macOS / Linux), `where google-ads-mcp` (Windows cmd) or `(Get-Command google-ads-mcp).Source` (PowerShell); on Windows write the path with forward slashes. To upgrade, uninstall first (`pipx uninstall google-ads-mcp`), then install again: `--force` has left a half-broken environment. The PyPI package named `google-ads-mcp` is upstream's, not this fork, and serves upstream's write tools.
2. **Google Cloud Project**: Enable the [Google Ads API](https://console.cloud.google.com/apis/library/googleads.googleapis.com) in your Google Cloud project ([how to enable APIs](https://support.google.com/googleapi/answer/6158841)).

### 2. Choose an Authentication Mode

The server supports two authentication and transport modes out of the box:

| Mode | Transport | Best For |
| :--- | :--- | :--- |
| **Mode A: Application Default Credentials (ADC)** | `stdio` | Single-user local CLI/IDE agents where the MCP client spawns the server directly. |
| **Mode B: FastMCP OAuth Proxy** | `streamable-http` | Shared/remote deployments or local containers where server credentials stay isolated from MCP client configs. |

#### Mode A: Local `stdio` with Application Default Credentials (ADC)

Configure [Application Default Credentials (ADC)](https://cloud.google.com/docs/authentication/provide-credentials-adc) for a user or service account with access to your Google Ads accounts. Credentials must include the Google Ads API scope (`https://www.googleapis.com/auth/adwords`). See [Manage OAuth Clients](https://support.google.com/cloud/answer/15549257) if you need to create an OAuth desktop or web client first.

- **Option 1 — User credentials with an OAuth client JSON file:**
  ```shell
  gcloud auth application-default login \
    --scopes=https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform \
    --client-id-file=YOUR_CLIENT_JSON_FILE
  ```

- **Option 2 — Service account impersonation:**
  ```shell
  gcloud auth application-default login \
    --impersonate-service-account=SERVICE_ACCOUNT_EMAIL \
    --scopes=https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform
  ```

When the command finishes, note the credentials path printed to the console (`Credentials saved to file: [PATH_TO_CREDENTIALS_JSON]`) for your MCP client configuration.

#### Mode B: Streamable HTTP with FastMCP OAuth Proxy

Setting `GOOGLE_ADS_MCP_OAUTH_CLIENT_ID` and `GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET` enables FastMCP's [OAuth proxy](https://gofastmcp.com/servers/auth/oauth-proxy) for dynamic per-user Google OAuth login and automatically switches the server transport from `stdio` to `streamable-http` (default endpoint: `http://localhost:8080/mcp`).

Run the server as a standalone process or container (see [Deployment](#deployment) and [Environment Variables Reference](#environment-variables-reference)), then point your MCP client at the HTTP endpoint—no server secrets are stored in client configs.

### 3. Configure Your MCP Client

#### Connecting via Streamable HTTP (Mode B — Recommended for Shared or Container Setups)

- **Antigravity / Antigravity IDE** ([MCP setup docs](https://antigravity.google/docs/mcp)):
  Use `serverUrl` (required for Streamable HTTP in current Antigravity releases; do not use `httpUrl`):
  ```json
  {
    "mcpServers": {
      "google-ads-mcp": {
        "serverUrl": "http://localhost:8080/mcp"
      }
    }
  }
  ```

- **Codex CLI** ([Codex MCP docs](https://developers.openai.com/codex/mcp/)):
  ```shell
  codex mcp add google_ads --url http://localhost:8080/mcp
  codex mcp login google_ads
  ```

#### Spawning Locally via `stdio` + ADC (Mode A)

Add the following `mcpServers` block to your client configuration, replacing `PATH_TO_CREDENTIALS_JSON` and `YOUR_PROJECT_ID` ([how to find your project ID](https://support.google.com/googleapi/answer/7014113)):

```json
{
  "mcpServers": {
    "google-ads-mcp": {
      "command": "/ABSOLUTE/PATH/TO/google-ads-mcp",
      "args": [],
      "env": {
        "GOOGLE_APPLICATION_CREDENTIALS": "PATH_TO_CREDENTIALS_JSON",
        "GOOGLE_CLOUD_PROJECT": "YOUR_PROJECT_ID",
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "YOUR_MANAGER_CUSTOMER_ID"
      }
    }
  }
}
```

> **Do not** launch the server with `pipx run --spec git+…` or `uvx --from git+…`: on a cache miss those resolve and build the package from GitHub before the server starts (about 30 s against about 2 s for the installed executable), and a client that allows only a few seconds for `initialize` reports the server as down. `GOOGLE_ADS_LOGIN_CUSTOMER_ID` is needed when your access to client accounts comes through a manager (MCC) account. `GOOGLE_ADS_DEVELOPER_TOKEN` is optional: Google stopped requiring developer tokens on 2026-09-09, and access levels now attach to the Cloud project behind your OAuth client. Note that `stdio` mode stores the ADC file path in the client's config; prefer [Local Container Deployment](#local-container-deployment-docker--podman) if client configs are shared or synced across tools.

#### Manager Accounts (`login_customer_id`)

If you access client accounts through a Google Ads Manager Account (MCC), you can either:
1. Pass `login_customer_id` per tool call (takes precedence when provided), or
2. Set `GOOGLE_ADS_LOGIN_CUSTOMER_ID` in the server's `env` configuration as a default:
   ```json
   "env": {
     "GOOGLE_APPLICATION_CREDENTIALS": "PATH_TO_CREDENTIALS_JSON",
     "GOOGLE_CLOUD_PROJECT": "YOUR_PROJECT_ID",
     "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "YOUR_MANAGER_CUSTOMER_ID"
   }
   ```

See [Google Ads API Call Structure (`login-customer-id`)](https://developers.google.com/google-ads/api/docs/concepts/call-structure#cid) for details.

#### Other MCP Clients (Claude Code, Cursor, VS Code)

The `mcpServers` JSON format works across standard MCP clients—place it in the client's configuration file (for example, `~/.claude/settings.json` for Claude Code, `.cursor/mcp.json` for Cursor, or `.vscode/mcp.json` for VS Code).

---

## Tools & Resources

### Available Tools

Upstream mounts tools with their category namespace prefix (`<namespace>_<tool>`); this fork's bundled config mounts the read namespaces without one (names in brackets below), adds the `keywords` namespace, and never mounts the Mutate rows. Namespaces and individual tools can be customized via `tools_config.yaml` (see [Configuring and Namespacing Tools](#configuring-and-namespacing-tools)).

| Default Tool Name | Namespace | Type | Description |
| :--- | :--- | :--- | :--- |
| `search_search` (fork: `search`) | `search` | Read | Queries Google Ads account data using GAQL (Google Ads Query Language). |
| `metadata_get_resource_metadata` (fork: `get_resource_metadata`) | `metadata` | Read | Retrieves selectable, filterable, and sortable field metadata for a Google Ads API resource (e.g., `campaign`). |
| `customers_list_accessible_customers` (fork: `list_accessible_customers`) | `customers` | Read | Lists customer IDs directly accessible to the authenticated user. |
| `customers_list_customer_clients` (fork: `list_customer_clients`) | `customers` | Read | GrowME fork. Lists every client account linked under a manager (MCC) account, not only direct grants. |
| `keywords_generate_keyword_ideas` (fork: `generate_keyword_ideas`) | `keywords` | Read | GrowME fork. Keyword Planner ideas with average monthly searches, competition and bid ranges. |
| `assets_create_asset` | `assets` | Mutate (not mounted in this fork) | Creates Google Ads assets (`TEXT`, `IMAGE`, `YOUTUBE_VIDEO`, `SITELINK`, `CALLOUT`, `STRUCTURED_SNIPPET`, plus additional types via `extra_params`). Note: `image_asset_path` is local `stdio` only; use base64 `image_asset_data` over HTTP. |
| `assets_upload_video_asset` | `assets` | Mutate (not mounted in this fork) | Uploads a local video file to YouTube via a background task so it can be referenced by video assets (local `stdio` mode only). |
| `labels_manage_labels` | `labels` | Mutate (not mounted in this fork) | Creates, updates, and removes account labels. |
| `labels_manage_label_associations` | `labels` | Mutate (not mounted in this fork) | Creates and removes label associations on `CAMPAIGN`, `CUSTOMER`, `AD_GROUP`, and `AD_GROUP_CRITERION` resources. |

### Available Resources

| Resource URI | Description |
| :--- | :--- |
| `resource://discovery-document` | Google Ads API REST discovery document describing the API surface, resources, methods, and schemas. (Prefer `metadata_get_resource_metadata` when looking up specific queryable GAQL fields.) |
| `resource://metrics` | Reference documentation for reporting metrics in the Google Ads API. |
| `resource://segments` | Reference documentation for reporting segments in the Google Ads API. |
| `resource://release-notes` | Release notes for the latest version of the Google Ads API. |

---

## Configuration & Security

### Propose & Confirm Safety Workflow

All mutate tools (`assets_create_asset`, `assets_upload_video_asset`, `labels_manage_labels`, and `labels_manage_label_associations`) enforce a mandatory two-step safety workflow:

1. **Propose (`validate_only=True`, default)**: Validates the operation (including a Google Ads API `validate_only` dry run where supported) without committing changes. Returns human-readable `change_items` and a cryptographic `checksum` bound to the `customer_id` and parameters.
2. **Confirm (`validate_only=False`)**: After explicit user approval, pass the exact same parameters and `customer_id` along with the `checksum` from step 1. Missing or mismatched checksums are rejected before mutating the account.

### Configuring and Namespacing Tools

Use `tools_config.yaml` to selectively enable or disable tool categories/tools and customize namespace prefixes. A default configuration with all categories enabled is bundled with the package.

The server resolves `tools_config.yaml` in the following order:
1. Path specified by `GOOGLE_ADS_MCP_TOOLS_CONFIG`.
2. `./tools_config.yaml` in the current working directory.
3. The default `tools_config.yaml` bundled with the package.

If an explicitly configured file is missing or any resolved file is invalid, startup fails immediately.

```yaml
namespaces:
  # Enable category with default prefix -> "customers_list_accessible_customers"
  customers: true

  # Enable category with custom prefix -> "query_search"
  search: "query"

  # Fine-grained control over individual tools in a category
  metadata:
    enabled: true
    prefix: "metadata"
    enabled_tools:
      - get_resource_metadata: true

  # Disable mutate categories for a strictly read-only deployment
  labels: false
  assets: false
```

### Tool Call Auditability

Every tool invocation—including background worker runs via `TasksExtension`—is recorded by `AuditMiddleware`, which emits structured JSON events (`"tool_call"` and `"task_execution"`) to the `ads_mcp.audit` logger:

- **Actor (`actor`)**: Authentication mode (`oauth` or `adc`), opaque OAuth identifiers (`sub`, `client_id`, `scopes`), MCP `session_id` (per-request on sessionless MCP `2026-07-28` connections), and MCP `client_info`. Raw PII such as `email` and `name` is omitted.
- **Execution context**: Unique `audit_id`, tool name, MCP `call_id` (and `task_id` for background tasks), `received_at` UTC timestamp, `customer_id`, `login_customer_id`, `mutation_phase` (`READ`, `PROPOSE`, or `COMMIT`), `checksum`, `schema_hash`, `status` (`completed`, `empty`, `enqueued`, `error`, or `failed`), and `duration_ms`.
- **Redacted arguments**: `arguments` with binary/base64 media payloads and Google Ads sensitive/PII fields (in both `snake_case` and protobuf JSON `camelCase`) replaced with `REDACTED`.

Audit records can also be persisted to `filetree`, `redis`, `firestore`, or `memory` via the `GOOGLE_ADS_MCP_AUDIT_STORAGE_*` variables below. Stored records remain unencrypted redacted JSON by default so backends like Firestore stay queryable across OAuth secret rotations; set `GOOGLE_ADS_MCP_AUDIT_STORAGE_ENCRYPTION_KEY` to enable Fernet encryption with a dedicated HKDF salt isolated from OAuth token storage.

### Environment Variables Reference

#### Core & OAuth Proxy

| Variable | Description |
| :--- | :--- |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | Optional default Manager Account (MCC) customer ID when accessing client accounts through a manager. |
| `GOOGLE_ADS_MCP_TOOLS_CONFIG` | Optional path to a custom `tools_config.yaml` file. |
| `PORT` | HTTP listen port when running in Streamable HTTP mode (default: `8080`). |
| `GOOGLE_ADS_MCP_OAUTH_CLIENT_ID` | Google Cloud OAuth 2.0 Client ID. Setting both ID and Secret enables OAuth Proxy & Streamable HTTP mode. |
| `GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET` | Google Cloud OAuth 2.0 Client Secret. |
| `GOOGLE_ADS_MCP_BASE_URL` | Public base URL of the HTTP server (default: `http://localhost:8080`). |
| `GOOGLE_ADS_MCP_JWT_SIGNING_KEY` | Recommended for multi-instance or persistent HTTP deployments to sign FastMCP JWTs consistently. |
| `GOOGLE_ADS_MCP_ALLOWED_CLIENT_REDIRECT_URIS` | GrowME fork. Comma-separated redirect URI patterns (FastMCP wildcards, e.g. `https://chatgpt.com/connector/oauth/*`, `http://localhost:*`) that MCP clients may register and be sent back to. Unset accepts any redirect URI, so on a public deployment anyone could register a client that returns sign-ins to their own site: set it. |

#### OAuth State & Audit Persistence

Both OAuth state storage (`GOOGLE_ADS_MCP_STORAGE_*`) and optional audit record storage (`GOOGLE_ADS_MCP_AUDIT_STORAGE_*`) support `filetree`, `redis`, `firestore`, and `memory` backends.

> **Optional backend extras:** Install `pip install "py-key-value-aio[redis]"` for Redis or install this fork with its Firestore extra (`pipx install "google-ads-mcp[firestore] @ git+https://github.com/growmedevelopment/google-ads-mcp.git"`, or `pip install ".[firestore]"` from a checkout) for Firestore. The PyPI package `google-ads-mcp` is upstream's, not this fork.

| OAuth Storage Variable | Audit Storage Equivalent | Description |
| :--- | :--- | :--- |
| `GOOGLE_ADS_MCP_STORAGE_TYPE` | `GOOGLE_ADS_MCP_AUDIT_STORAGE_TYPE` | Backend type: `filetree`, `redis`, `firestore`, or `memory`. |
| `GOOGLE_ADS_MCP_STORAGE_PATH` | `GOOGLE_ADS_MCP_AUDIT_STORAGE_PATH` | Directory path when using the `filetree` backend. |
| `GOOGLE_ADS_MCP_STORAGE_REDIS_URL` | `GOOGLE_ADS_MCP_AUDIT_STORAGE_REDIS_URL` | Redis connection URL (defaults to `redis://localhost:6379/0`). |
| `GOOGLE_ADS_MCP_STORAGE_FIRESTORE_PROJECT` | `GOOGLE_ADS_MCP_AUDIT_STORAGE_FIRESTORE_PROJECT` | GCP project for `firestore` (defaults to ADC project; setting this implicitly selects `firestore`). |
| `GOOGLE_ADS_MCP_STORAGE_FIRESTORE_DATABASE` | `GOOGLE_ADS_MCP_AUDIT_STORAGE_FIRESTORE_DATABASE` | Firestore database name (defaults to `(default)`). |
| `GOOGLE_ADS_MCP_STORAGE_ENCRYPTION_KEY` | `GOOGLE_ADS_MCP_AUDIT_STORAGE_ENCRYPTION_KEY` | Fernet key (or secret material) for encrypting stored records. OAuth storage falls back to JWT/OAuth secret; audit storage is unencrypted unless explicitly set. |
| `GOOGLE_ADS_MCP_STORAGE_DISABLE_ENCRYPTION` | `GOOGLE_ADS_MCP_AUDIT_STORAGE_DISABLE_ENCRYPTION` | Set to `true` to disable storage encryption. |
| — | `GOOGLE_ADS_MCP_AUDIT_STORAGE_TTL_SECONDS` | Optional retention TTL (in positive seconds) for persisted audit records. |

---

## Deployment

Remote and containerized deployments run in Streamable HTTP mode using the FastMCP OAuth Proxy (`GOOGLE_ADS_MCP_OAUTH_CLIENT_ID` and `GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET`).

### Local Container Deployment (Docker / Podman)

Running the server in a local rootless container isolates OAuth secrets and storage encryption keys from MCP client configurations.

1. **Build the image:**
   ```shell
   docker build --tag google-ads-mcp:latest .
   # or with Podman:
   podman build --tag localhost/google-ads-mcp:latest --file Dockerfile .
   ```

2. **Run with isolated secrets and persistent OAuth state:**
   Keep your `.env` file outside the repository with owner-only permissions, bind port `8080` strictly to `127.0.0.1` for local agents, and mount a dedicated volume for `filetree` OAuth state (`GOOGLE_ADS_MCP_STORAGE_PATH=/var/lib/google-ads-mcp`):

   ```shell
   docker run -d --name google-ads-mcp \
     -p 127.0.0.1:8080:8080 \
     --env-file /absolute/host/path/google-ads-mcp.env \
     -v google-ads-mcp-oauth:/var/lib/google-ads-mcp:rw \
     --read-only --security-opt no-new-privileges --cap-drop ALL \
     google-ads-mcp:latest
   ```

   For systemd/Podman Quadlet setups, the equivalent `.container` unit is:
   ```ini
   [Container]
   Image=localhost/google-ads-mcp:latest
   PublishPort=127.0.0.1:8080:8080
   EnvironmentFile=/absolute/host/path/google-ads-mcp.env
   Volume=google-ads-mcp-oauth.volume:/var/lib/google-ads-mcp:rw
   ReadOnly=true
   NoNewPrivileges=true
   DropCapability=all
   ```

#### HTTP Interoperability Notes

- **Stateful Streamable HTTP**: Keep stateful Streamable HTTP enabled (the default). It serves both MCP 2025 clients (`Mcp-Session-Id` + `GET` SSE) and MCP 2026 clients (sessionless `POST` + `subscriptions/listen`). Do not enable FastMCP's `stateless_http` option on a shared endpoint, as it removes the legacy `GET` SSE channel.
- **FastMCP 4 & OAuth callback compatibility**: The server targets `fastmcp>=4.0.6,<5` and `mcp[cli]>=2.0.0,<3`. The `Dockerfile` applies a version-guarded patch that stops advertising RFC 9207 `authorization_response_iss_parameter_supported` as mandatory for compatibility with Codex CLI OAuth callbacks, failing the build loudly if the upstream FastMCP target changes.

### Google Cloud Run Deployment

#### Prerequisites

1. A Google Cloud project with billing enabled.
2. The `gcloud` CLI authenticated and configured:
   ```shell
   gcloud config set project YOUR_PROJECT_ID
   ```

#### Step 1: Build and Push the Container Image

Create an Artifact Registry repository and build with Cloud Build (no local Docker required):

```shell
gcloud artifacts repositories create mcp-servers \
  --repository-format=docker \
  --location=us-central1

gcloud builds submit \
  --tag us-central1-docker.pkg.dev/YOUR_PROJECT_ID/mcp-servers/google-ads-mcp:latest .
```

> **Using Firestore for persistent OAuth/audit storage on Cloud Run:**
> 1. Install the Firestore extra in your `Dockerfile` (`RUN uv pip install --system ".[firestore]"`).
> 2. Provision a Firestore database in your GCP project and grant the Cloud Run service account `roles/datastore.user`.
> 3. Note that `py-key-value-aio`'s Firestore store filters expired entries on read without deleting them (and writes `expires_at` as a string rather than a native timestamp), so plan a periodic cleanup job for long-running deployments—or use Redis (`GOOGLE_ADS_MCP_STORAGE_TYPE=redis`), which expires keys natively.

#### Step 2: Deploy to Cloud Run

Deploy the service with your OAuth credentials, persistent JWT signing key, and storage backend. On first deploy, you can omit `GOOGLE_ADS_MCP_BASE_URL` until Cloud Run assigns your service URL, then update it:

```shell
gcloud run deploy google-ads-mcp \
  --image us-central1-docker.pkg.dev/YOUR_PROJECT_ID/mcp-servers/google-ads-mcp:latest \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID,GOOGLE_ADS_MCP_OAUTH_CLIENT_ID=YOUR_CLIENT_ID,GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET=YOUR_CLIENT_SECRET,GOOGLE_ADS_MCP_BASE_URL=https://your-cloud-run-url.a.run.app,GOOGLE_ADS_MCP_JWT_SIGNING_KEY=YOUR_JWT_SIGNING_KEY,GOOGLE_ADS_MCP_STORAGE_TYPE=firestore"
```

Add `GOOGLE_ADS_LOGIN_CUSTOMER_ID=YOUR_MANAGER_CUSTOMER_ID` to `--set-env-vars` if your accounts are accessed via a manager account.

#### Step 3: Connect Your MCP Client

Point your MCP client at the Cloud Run `/mcp` endpoint:

```json
{
  "mcpServers": {
    "google-ads-mcp": {
      "serverUrl": "https://your-cloud-run-url.a.run.app/mcp"
    }
  }
}
```

---

## Try It Out

Launch your MCP client and verify that `google-ads-mcp` appears in the connected servers list. Try prompts such as:

- `What can the google-ads-mcp server do?`
- `What customers do I have access to?`
- `How many active campaigns do I have for customer ID 1234567890?`
- `How is my campaign performance this week for customer ID 1234567890?`

> **Tip:** Most tools require a `customer_id`. Including the customer ID directly in your prompt makes switching across multiple accounts faster.

---

## Contributing

Contributions are welcome! See the [Contributing Guide](CONTRIBUTING.md) to get started.
This fork is not published to PyPI: install it from its GitHub source as above. (Upstream's release guide and PyPI workflow were removed from the fork.)
