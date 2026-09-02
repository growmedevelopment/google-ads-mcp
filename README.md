# Google Ads MCP Server

This repo contains the source code for running an
[MCP](https://modelcontextprotocol.io) server that interacts with the
[Google Ads API](https://developers.google.com/google-ads/api).

## tools

The server uses the
[Google Ads API](https://developers.google.com/google-ads/api/reference/rpc/latest/overview)
to provide several
[tools](https://modelcontextprotocol.io/docs/concepts/tools) and [Resources](https://modelcontextprotocol.io/docs/concepts/tools) for use with LLMs and AI agents.

### tools available

- `search`: Retrieves information about the Google Ads account.
- `get_resource_metadata`: Retrieves metadata about a Google Ads API resource type, for example "campaign". This is useful to understand the structure of the data and what fields are available for querying.
- `list_accessible_customers`: Returns ids of customers where the authenticating user has **direct** access. Does NOT include customers inherited via MCC access — for that, use `list_customer_clients`.
- `list_customer_clients` *(GrowME fork)*: Lists every customer account linked under a Manager (MCC) account via the `customer_client` resource. Takes an optional `manager_customer_id`; defaults to the `GOOGLE_ADS_LOGIN_CUSTOMER_ID` env var. Use this when the user asks "show me all our managed accounts."
- `generate_keyword_ideas` *(GrowME fork)*: Wraps `KeywordPlanIdeaService.GenerateKeywordIdeas`. Returns keyword text + historical metrics (avg monthly searches, competition, top-of-page CPC range) for a list of seed keywords in the requested geo + language. Google rate-limits this method to **1 request per second per customer ID**, so the tool paces calls 1.1 s apart inside the server, retries a quota rejection after Google's stated delay, batches more than 20 seeds automatically, and reports in its error whether a rejection was the per-second rate limit or the daily operations quota. Call it once at a time with up to 20 seeds per call; never in parallel.

### Resources available

- `discovery-document`: Retrieve the Google Ads API discovery document. Provides the discovery document for the latest version of the Google Ads API, which describes the API surface, including resources, methods, and schemas. Host LLMs should access this resource to understand the structure of the Google Ads API and discover available features.
- `metrics`: Retrieve information about the metrics available for reporting in the Google Ads API.
- `segments`: Retrieve information about the segments available for reporting in the Google Ads API.
- `release-notes`: Retrieve the release notes for the latest version of the Google Ads API.

## Notes

1.  The MCP Server will expose your data to the Agent or LLM that you connect to it.
1.  If you have technical issues, please use the [GitHub issue tracker](https://github.com/googleads/google-ads-mcp/issues).
1.  To help us collect usage data, you will notice an extra header has been added to your API calls: this data is used to improve the product.

## Setup instructions

Setup involves the following steps:

1.  Configure Python.
1.  Configure Developer Token.
1.  Enable APIs in your project
1.  Configure Credentials.
1.  Configure your AI agent.

### Configure Python

[Install pipx](https://pipx.pypa.io/stable/#install-pipx).

### Configure Developer Token

Follow the instructions for [Obtaining a Developer Token](https://developers.google.com/google-ads/api/docs/get-started/dev-token).

Make sure your developer token has at least [Explorer access](https://developers.google.com/google-ads/api/docs/api-policy/access-levels).

Record your developer token, you will need this for the the 'Configure your AI agent' step below

### Enable APIs in your project

[Follow the instructions](https://support.google.com/googleapi/answer/6158841)
to enable the following APIs in your Google Cloud project:

* [Google Ads API](https://console.cloud.google.com/apis/library/googleads.googleapis.com)

### Configure Credentials
#### Option 1: Using FastMCP OAuth Proxy

The server supports FastMCP's [OAuth proxy](https://gofastmcp.com/servers/auth/oauth-proxy) feature for dynamic user authentication. This is useful when running the server as a web service.

To enable it, set the following environment variables:

- `GOOGLE_ADS_MCP_OAUTH_CLIENT_ID`: Your Google Cloud OAuth 2.0 Client ID.
- `GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET`: Your Google Cloud OAuth 2.0 Client Secret.
- `GOOGLE_ADS_MCP_BASE_URL`: (Optional) The base URL where the server is accessible (defaults to `http://localhost:8000`).

Once this is enabled, you can authenticate to the API through your MCP client: for example, in Gemini CLI, the command `/mcp auth google-ads-mcp` triggers the authentication flow.

When these variables are set, the server automatically switches to the `streamable-http` transport (SSE/HTTP) instead of `stdio`.

You will need to run the server as a separate process and configure your MCP client to connect to the SSE endpoint (e.g., `http://localhost:8000/mcp`).

#### Option 2: Configure credentials using Application Default Credentials

Configure your [Application Default Credentials
(ADC)](https://cloud.google.com/docs/authentication/provide-credentials-adc).
Make sure the credentials are for a user with access to your Google Ads
accounts or properties.

Credentials must include the Google Ads API scope:

```
https://www.googleapis.com/auth/adwords
```

Check out
[Manage OAuth Clients](https://support.google.com/cloud/answer/15549257)
for how to create an OAuth client.

Here are some sample `gcloud` commands you might find useful:


- Set up ADC using user credentials and an OAuth desktop or web client after
  downloading the client JSON to `YOUR_CLIENT_JSON_FILE`.

  ```shell
  gcloud auth application-default login \
    --scopes https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform \
    --client-id-file=YOUR_CLIENT_JSON_FILE
  ```

- Set up ADC using service account impersonation.

  ```shell
  gcloud auth application-default login \
    --impersonate-service-account=SERVICE_ACCOUNT_EMAIL \
    --scopes=https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform
  ```

When the `gcloud auth application-default` command completes, copy the
`PATH_TO_CREDENTIALS_JSON` file location printed to the console in the
following message. You will need this for a later step!

```
Credentials saved to file: [PATH_TO_CREDENTIALS_JSON]
```

#### Option 3: Configure credentials using the Google Ads API Python client library.

[Follow the instructions](https://developers.google.com/google-ads/api/docs/client-libs/python/)
to setup and configure the Google Ads API Python client library

If you have already done this and have a working `google-ads.yaml` , you can reuse this file!

In the utils.py file, change get_googleads_client() to use the load_from_storage() method.

### Configure your AI agent

These instructions describe the process to configure the MCP server on either 
[Gemini CLI](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/index.md) or [Gemini Code Assist](https://marketplace.visualstudio.com/items?itemName=Google.geminicodeassist).

Create or edit the file at `~/.gemini/settings.json`, adding your server
    to the `mcpServers` list.

- Option 1: Using FastMCP OAuth Proxy (Streamable HTTP)

  You can run the server as a separate process and configure your MCP client to connect to the SSE endpoint (e.g., `http://localhost:8000/mcp`).
  This also allows using FastMCP's [OAuth proxy](https://gofastmcp.com/servers/auth/oauth-proxy) feature for dynamic user authentication.

    ```json
    {
      "mcpServers": {
        "google-ads-mcp": {
          "httpUrl":"http://localhost:8000/mcp",
          "env": {
            "GOOGLE_PROJECT_ID": "YOUR_PROJECT_ID",
            "GOOGLE_ADS_DEVELOPER_TOKEN": "YOUR_DEVELOPER_TOKEN"                        
          }
        }
      }
    }
    ```

- Option 2: the Application Default Credentials method

    Replace `PATH_TO_CREDENTIALS_JSON` with the path you copied in the previous
    step.

    We also recommend that you add a `GOOGLE_CLOUD_PROJECT` attribute to the
    `env` object. Replace `YOUR_PROJECT_ID` in the following example with the
    [project ID](https://support.google.com/googleapi/answer/7014113) of your
    Google Cloud project.



    ```json
    {
      "mcpServers": {
        "google-ads-mcp": {
          "command": "pipx",
          "args": [
            "run",
            "--spec",
            "git+https://github.com/googleads/google-ads-mcp.git",
            "google-ads-mcp"
          ],
          "env": {
            "GOOGLE_APPLICATION_CREDENTIALS": "PATH_TO_CREDENTIALS_JSON",
            "GOOGLE_PROJECT_ID": "YOUR_PROJECT_ID",
            "GOOGLE_ADS_DEVELOPER_TOKEN": "YOUR_DEVELOPER_TOKEN"
          }
        }
      }
    }
    ```

- Option 3: the Python client library method

    ```json
    {
      "mcpServers": {
        "google-ads-mcp": {
          "command": "pipx",
          "args": [
            "run",
            "--spec",
            "git+https://github.com/googleads/google-ads-mcp.git",
            "google-ads-mcp"
          ],
          "env": {
            "GOOGLE_PROJECT_ID": "YOUR_PROJECT_ID",
            "GOOGLE_ADS_DEVELOPER_TOKEN": "YOUR_DEVELOPER_TOKEN"
          }
        }
      }
    }
    ```

#### Login Customer Id

If your access to the customer account is through a manager account, you will
need to add the customer ID of the manager account to the settings file.

See [here](https://developers.google.com/google-ads/api/docs/concepts/call-structure#cid) for details.

The final file will look like this:

  ```json
  {
    "mcpServers": {
      "google-ads-mcp": {
        "command": "pipx",
        "args": [
          "run",
          "--spec",
          "git+https://github.com/googleads/google-ads-mcp.git",
          "google-ads-mcp"
        ],
        "env": {
          "GOOGLE_APPLICATION_CREDENTIALS": "PATH_TO_CREDENTIALS_JSON",
          "GOOGLE_PROJECT_ID": "YOUR_PROJECT_ID",
          "GOOGLE_ADS_DEVELOPER_TOKEN": "YOUR_DEVELOPER_TOKEN",
          "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "YOUR_MANAGER_CUSTOMER_ID"
        }
      }
    }
  }
  ```

## Deployment to Google Cloud Platform

Instead of hosting this MCP server locally, you can host it on Google Cloud Run or on any other cloud-based infrastructure. This is useful if you want to share the server across different agents or run it as a web service.

Note that this only supports authentication with an OAuth Client ID and Client Secret pair through the OAuth proxy (Option #1 above).

### Prerequisites

1.  A Google Cloud project.
2.  The `gcloud` CLI installed, authenticated, and active project set.
    ```shell
    gcloud config set project YOUR_PROJECT_ID
    ```

### Step 1: Build and Push Docker Image

You can use Cloud Build to build and push the image to Artifact Registry without needing Docker installed locally.

1.  Create a repository in Artifact Registry:
    ```shell
    gcloud artifacts repositories create mcp-servers --repository-format=docker --location=us-central1
    ```
2.  Build and submit the image:
    ```shell
    gcloud builds submit --tag us-central1-docker.pkg.dev/YOUR_PROJECT_ID/mcp-servers/google-ads-mcp:latest .
    ```
    Replace `YOUR_PROJECT_ID` with your Google Cloud project ID.

### Step 2: Deploy to Google Cloud Run

Make sure to set the required environment variables:

- `GOOGLE_PROJECT_ID`: Your Google Cloud project ID.
- `GOOGLE_ADS_DEVELOPER_TOKEN`: The developer token you want the MCP server to use (see above).
- `GOOGLE_ADS_MCP_OAUTH_CLIENT_ID`: The OAuth Client ID you want the MCP server to use.
- `GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET`: The OAuth Client secret you want the MCP server to use.
- `GOOGLE_ADS_MCP_BASE_URL`: The base URL where your MCP server is accessible: this will be automatically assigned by Google Cloud Run after your first deployment. You can update the environment variables after deployment. 
- `FASTMCP_HOST`: Set this to `0.0.0.0` to allow FastMCP to accept connections from all IP addresses.

```shell
gcloud run deploy google-ads-mcp \
  --image us-central1-docker.pkg.dev/YOUR_PROJECT_ID/mcp-servers/google-ads-mcp:latest \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars="GOOGLE_PROJECT_ID=YOUR_PROJECT_ID,GOOGLE_ADS_DEVELOPER_TOKEN=YOUR_DEVELOPER_TOKEN,GOOGLE_ADS_MCP_OAUTH_CLIENT_ID=YOUR_CLIENT_ID,GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET=YOUR_CLIENT_SECRET,GOOGLE_ADS_MCP_BASE_URL=YOUR_BASE_URL,FASTMCP_HOST=0.0.0.0"
```

### Step 3: Configure MCP Client

Once deployed, update your MCP client configuration (e.g., `~/.gemini/settings.json`) to use the Cloud Run URL.

```json
{
  "mcpServers": {
    "google-ads-mcp": {
      "httpUrl": "https://your-cloud-run-url.a.run.app/mcp"
    }
  }
}
```

## Try it out

Launch Gemini Code Assist or Gemini CLI and type `/mcp`. You should see
`google-ads-mcp` listed in the results.

Here are some sample prompts to get you started:

- Ask what the server can do:

  ```
  what can the ads-mcp server do?
  ```

- Ask about customers:

  ```
  what customers do I have access to?
  ```

- Ask about campaigns 

  ```
  How many active campaigns do I have?
  ```

  ```
  How is my campaign performance this week?
  ```

### Note about Customer ID

Your agent will need and ask for a customer id for most prompts. If you are 
moving between multiple customers, including the customer ID in the prompt may
be simpler.

```
How many active campaigns do I have for customer id 1234567890
```


## Contributing

Contributions welcome! See the [Contributing Guide](CONTRIBUTING.md).