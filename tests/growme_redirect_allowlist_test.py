# Copyright 2026 GrowME Marketing Inc.
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

"""GrowME fork: GOOGLE_ADS_MCP_ALLOWED_CLIENT_REDIRECT_URIS limits where the
hosted server's OAuth proxy will send sign-ins (dynamic client registration
and /authorize). Runs the real server in HTTP mode with a dummy Google client;
no Google call is made."""

import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

_ALLOWLIST = ",".join(
    [
        "https://chatgpt.com/connector/oauth/*",
        "https://chatgpt.com/connector_platform_oauth_redirect",
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
        "http://localhost:*",
        "http://127.0.0.1:*",
    ]
)

_ALLOWED = [
    "https://chatgpt.com/connector/oauth/AbC123",
    "https://chatgpt.com/connector_platform_oauth_redirect",
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost:53682/callback",
    "http://127.0.0.1:8085/",
]

_REFUSED = [
    "https://evil.example/cb",
    "https://chatgpt.com.evil.example/connector/oauth/x",
    "https://evil.example/connector/oauth/x",
    "https://chatgpt.com@evil.example/connector/oauth/x",
    "https://evil.example/?next=https://chatgpt.com/connector/oauth/x",
    "http://chatgpt.com/connector/oauth/x",
    "https://claude.ai/api/mcp/auth_callback/../../evil",
    "https://claude.ai.evil.example/api/mcp/auth_callback",
    "http://localhost.evil.example:8080/callback",
]


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    def __init__(self, allowlist):
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env = dict(os.environ)
        env.update(
            GOOGLE_ADS_MCP_OAUTH_CLIENT_ID="dummy.apps.googleusercontent.com",
            GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET="dummy-secret",
            GOOGLE_ADS_MCP_BASE_URL=self.base,
            GOOGLE_ADS_MCP_STORAGE_TYPE="memory",
            GOOGLE_ADS_MCP_JWT_SIGNING_KEY="test-only-signing-key",
            PORT=str(self.port),
        )
        env.pop("GOOGLE_ADS_MCP_TOOLS_CONFIG", None)
        env.pop("GOOGLE_ADS_MCP_ALLOWED_CLIENT_REDIRECT_URIS", None)
        if allowlist is not None:
            env["GOOGLE_ADS_MCP_ALLOWED_CLIENT_REDIRECT_URIS"] = allowlist
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from ads_mcp.server import run_server; run_server()",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                urllib.request.urlopen(
                    self.base + "/.well-known/oauth-authorization-server",
                    timeout=2,
                )
                return
            except Exception:
                time.sleep(0.3)
        self.stop()
        raise AssertionError("server did not start")

    def register(self, redirect_uri):
        body = json.dumps(
            {
                "redirect_uris": [redirect_uri],
                "client_name": "test",
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            }
        ).encode()
        req = urllib.request.Request(
            self.base + "/register",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, None

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


class RedirectAllowlistTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = _Server(_ALLOWLIST)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def test_chatgpt_claude_and_loopback_redirects_register(self):
        for uri in _ALLOWED:
            status, _ = self.server.register(uri)
            self.assertEqual(status, 201, uri)

    def test_outside_and_look_alike_redirects_are_refused(self):
        for uri in _REFUSED:
            status, _ = self.server.register(uri)
            self.assertNotEqual(status, 201, uri)

    def test_authorize_refuses_an_unlisted_redirect_for_a_listed_client(self):
        status, client = self.server.register(_ALLOWED[0])
        self.assertEqual(status, 201)
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": client["client_id"],
                "redirect_uri": "https://evil.example/cb",
                "code_challenge": "x" * 43,
                "code_challenge_method": "S256",
                "state": "s",
            }
        )

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        try:
            resp = opener.open(
                self.server.base + "/authorize?" + query, timeout=10
            )
            status, location = resp.status, resp.headers.get("location", "")
        except urllib.error.HTTPError as e:
            status, location = e.code, e.headers.get("location", "")
        self.assertNotIn("evil.example", location or "")
        self.assertNotIn("/consent", location or "")
        self.assertGreaterEqual(status, 400)


class NoAllowlistControlTest(unittest.TestCase):
    """Without the setting, FastMCP's default accepts any redirect (control)."""

    def test_unset_accepts_an_outside_redirect(self):
        server = _Server(None)
        try:
            status, _ = server.register("https://evil.example/cb")
            self.assertEqual(status, 201)
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
