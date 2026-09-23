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

"""Test cases for the utils module."""

import unittest
from google.ads.googleads.v24.enums.types.campaign_status import (
    CampaignStatusEnum,
)
from google.ads.googleads.v24.common.types.metrics import Metrics

from ads_mcp import utils


class TestUtils(unittest.TestCase):
    """Test cases for the utils module."""

    def test_format_output_value(self):
        """Tests that output values are formatted correctly."""

        self.assertEqual(
            utils.format_output_value(
                CampaignStatusEnum.CampaignStatus.ENABLED
            ),
            "ENABLED",
        )

    def test_format_output_value_primitive(self):
        """Tests that primitive values are returned as is."""
        self.assertEqual(utils.format_output_value(123), 123)
        self.assertEqual(utils.format_output_value("abc"), "abc")

    def test_format_output_value_message(self):
        """Tests that proto messages are converted to dict."""
        metrics = Metrics(clicks=10, impressions=100)
        formatted = utils.format_output_value(metrics)
        self.assertIsInstance(formatted, dict)
        self.assertEqual(formatted.get("clicks"), "10")
        self.assertEqual(formatted.get("impressions"), "100")

    def test_format_output_value_repeated_primitive(self):
        """Tests that repeated primitive values are formatted."""
        self.assertEqual(
            utils.format_output_value([1, 2, 3]),
            [1, 2, 3],
        )

    def test_format_output_value_repeated_message(self):
        """Tests that repeated proto messages are formatted."""
        metrics1 = Metrics(clicks=10)
        metrics2 = Metrics(clicks=20)
        formatted = utils.format_output_value([metrics1, metrics2])
        self.assertIsInstance(formatted, list)
        self.assertEqual(len(formatted), 2)
        self.assertEqual(formatted[0].get("clicks"), "10")
        self.assertEqual(formatted[1].get("clicks"), "20")

    def test_get_developer_token(self):
        """Returns the env variable when set, None when unset (no raise)."""
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(utils._get_developer_token())

        with patch.dict(
            os.environ, {"GOOGLE_ADS_DEVELOPER_TOKEN": "test-dev-token"}
        ):
            self.assertEqual(utils._get_developer_token(), "test-dev-token")

    def test_get_googleads_client_without_developer_token(self):
        """Builds the client without a developer_token kwarg when unset."""
        import os
        from unittest.mock import MagicMock, patch

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(
                utils, "_create_credentials", return_value=MagicMock()
            ):
                with patch("ads_mcp.utils.GoogleAdsClient") as mock_client:
                    utils._get_googleads_client()
                    mock_client.assert_called_once()
                    _, kwargs = mock_client.call_args
                    self.assertNotIn("developer_token", kwargs)

    def test_get_googleads_client_with_developer_token(self):
        """Passes developer_token through when the variable is set."""
        import os
        from unittest.mock import MagicMock, patch

        with patch.dict(
            os.environ,
            {"GOOGLE_ADS_DEVELOPER_TOKEN": "test-dev-token"},
            clear=True,
        ):
            with patch.object(
                utils, "_create_credentials", return_value=MagicMock()
            ):
                with patch("ads_mcp.utils.GoogleAdsClient") as mock_client:
                    utils._get_googleads_client()
                    mock_client.assert_called_once()
                    _, kwargs = mock_client.call_args
                    self.assertEqual(
                        kwargs.get("developer_token"), "test-dev-token"
                    )

    def test_get_googleads_client_instantiation_without_developer_token(self):
        """The real GoogleAdsClient accepts a missing token (google-ads >= 32)."""
        import os
        from unittest.mock import patch
        from google.auth.credentials import AnonymousCredentials

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(
                utils,
                "_create_credentials",
                return_value=AnonymousCredentials(),
            ):
                client = utils._get_googleads_client()
                self.assertIsNone(client.developer_token)

    def test_quota_tool_error_message_names_the_project_quota(self):
        """The quota explanation blames the Cloud project, not a developer token."""
        msg = utils.quota_tool_error_message(Exception("429 boom"))
        self.assertIn("Cloud project's daily", msg)
        self.assertIn("15,000", msg)
        self.assertNotIn("developer token", msg)
