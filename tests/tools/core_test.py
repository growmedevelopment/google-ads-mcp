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

"""Test cases for core tools."""

import unittest
from unittest.mock import MagicMock, patch

from ads_mcp.tools import core


class TestCoreTools(unittest.TestCase):
    """Test cases for the core tools module."""

    @patch("ads_mcp.utils.get_googleads_service")
    def test_list_accessible_customers_success(self, mock_get_service):
        """Tests that list_accessible_customers returns customer IDs with prefix stripped."""
        mock_service = MagicMock()
        mock_response = MagicMock()
        mock_response.resource_names = [
            "customers/1234567890",
            "customers/9876543210",
        ]
        mock_service.list_accessible_customers.return_value = mock_response
        mock_get_service.return_value = mock_service

        result = core.list_accessible_customers()

        self.assertEqual(result, ["1234567890", "9876543210"])
        mock_get_service.assert_called_once_with("CustomerService")
        mock_service.list_accessible_customers.assert_called_once()


if __name__ == "__main__":
    unittest.main()
