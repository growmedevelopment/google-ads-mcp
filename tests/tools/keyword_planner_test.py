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

"""Test cases for the generate_keyword_ideas tool (pacing + quota retry)."""

import unittest
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from google.api_core import exceptions as api_exceptions

from ads_mcp.tools import keyword_planner as kp


def _idea(
    text, searches=100, competition="LOW", index=10, low=1_000_000, high=2_000_000
):
    idea = MagicMock()
    idea.text = text
    m = idea.keyword_idea_metrics
    m.avg_monthly_searches = searches
    m.competition.name = competition
    m.competition_index = index
    m.low_top_of_page_bid_micros = low
    m.high_top_of_page_bid_micros = high
    return idea


def _quota_exception(
    code="RESOURCE_EXHAUSTED",
    retry_delay_s=None,
    rate_scope="ACCOUNT",
    rate_name=None,
    request_id="req-1",
):
    """Builds a GoogleAdsException carrying one QuotaError with optional details."""
    error = MagicMock()
    error.message = "Too many requests. Retry in 3 seconds."
    error.error_code.quota_error.name = code
    details = error.details.quota_error_details
    if retry_delay_s is None:
        details.retry_delay = None
    else:
        details.retry_delay.seconds = int(retry_delay_s)
        details.retry_delay.nanos = int((retry_delay_s - int(retry_delay_s)) * 1e9)
    details.rate_scope.name = rate_scope
    details.rate_name = rate_name
    failure = MagicMock()
    failure.errors = [error]
    return GoogleAdsException(None, None, failure, request_id)


def _other_exception(request_id="req-x"):
    error = MagicMock()
    error.message = "Invalid geo target constant."
    error.error_code.quota_error.name = "UNSPECIFIED"
    failure = MagicMock()
    failure.errors = [error]
    return GoogleAdsException(None, None, failure, request_id)


class TestGenerateKeywordIdeas(unittest.TestCase):
    """Behavioural tests with the Google Ads client fully mocked."""

    def setUp(self):
        kp._last_call_monotonic = None
        self.get_service = patch("ads_mcp.utils.get_googleads_service").start()
        self.get_client = patch("ads_mcp.utils.get_googleads_client").start()
        self.get_type = patch("ads_mcp.utils.get_googleads_type").start()
        self.sleep = patch("ads_mcp.tools.keyword_planner.time.sleep").start()
        self.addCleanup(patch.stopall)
        self.kp_service = MagicMock()
        self.gas = MagicMock()
        self.get_service.side_effect = lambda name: (
            self.kp_service if name == "KeywordPlanIdeaService" else self.gas
        )
        self.requests = []

        def make_type(name):
            obj = MagicMock(name=name)
            if name == "GenerateKeywordIdeasRequest":
                self.requests.append(obj)
            return obj

        self.get_type.side_effect = make_type

    def _call(self, seeds=("basement renovations calgary",)):
        return kp.generate_keyword_ideas(
            customer_id="975-512-9455",
            seed_keywords=list(seeds),
            geo_target_ids=["2124"],
        )

    def test_basic_success_shape(self):
        self.kp_service.generate_keyword_ideas.return_value = [
            _idea(
                "basement builders calgary", searches=480, competition="HIGH", index=77
            )
        ]
        rows = self._call()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["keyword_text"], "basement builders calgary")
        self.assertEqual(rows[0]["avg_monthly_searches"], 480)
        self.assertEqual(rows[0]["competition_level"], "HIGH")
        self.assertEqual(rows[0]["competition_index"], 77)
        self.assertEqual(rows[0]["low_cpc"], 1.0)
        self.assertEqual(rows[0]["high_cpc"], 2.0)
        self.assertEqual(self.requests[0].customer_id, "9755129455")
        self.assertEqual(self.kp_service.generate_keyword_ideas.call_count, 1)
        self.sleep.assert_not_called()

    def test_second_call_is_paced_one_second_apart(self):
        self.kp_service.generate_keyword_ideas.return_value = []
        with patch("ads_mcp.tools.keyword_planner.time.monotonic") as mono:
            # first call at t=100.0, second call arrives 0.2 s later
            mono.side_effect = [100.0, 100.0, 100.2, 101.3]
            self._call()
            self._call()
        self.sleep.assert_called_once()
        waited = self.sleep.call_args[0][0]
        self.assertAlmostEqual(waited, kp._MIN_INTERVAL_S - 0.2, places=6)

    def test_no_pacing_wait_when_enough_time_passed(self):
        self.kp_service.generate_keyword_ideas.return_value = []
        with patch("ads_mcp.tools.keyword_planner.time.monotonic") as mono:
            mono.side_effect = [100.0, 100.0, 105.0, 105.0]
            self._call()
            self._call()
        self.sleep.assert_not_called()

    def test_rate_limit_retries_after_google_retry_delay_then_succeeds(self):
        self.kp_service.generate_keyword_ideas.side_effect = [
            _quota_exception(retry_delay_s=3.0, rate_name="Requests per account"),
            [_idea("legal basement suites calgary")],
        ]
        rows = self._call()
        self.assertEqual(
            [r["keyword_text"] for r in rows], ["legal basement suites calgary"]
        )
        self.assertEqual(self.kp_service.generate_keyword_ideas.call_count, 2)
        self.assertEqual(self.sleep.call_args_list[0][0][0], 3.0)

    def test_rate_limit_without_delay_uses_default_backoff(self):
        self.kp_service.generate_keyword_ideas.side_effect = [
            _quota_exception(code="RESOURCE_TEMPORARILY_EXHAUSTED"),
            [],
        ]
        self._call()
        self.assertEqual(self.sleep.call_args_list[0][0][0], kp._DEFAULT_RETRY_WAIT_S)

    def test_persistent_rate_limit_reports_rate_not_daily(self):
        self.kp_service.generate_keyword_ideas.side_effect = [
            _quota_exception(
                retry_delay_s=2.0, rate_name="Requests per account", request_id="req-7"
            )
        ] * kp._MAX_ATTEMPTS
        with self.assertRaises(ToolError) as ctx:
            self._call()
        msg = str(ctx.exception)
        self.assertEqual(
            self.kp_service.generate_keyword_ideas.call_count, kp._MAX_ATTEMPTS
        )
        self.assertIn("RATE limit", msg)
        self.assertIn("1 request per second", msg)
        self.assertIn("not the daily quota", msg)
        self.assertIn("Requests per account", msg)
        self.assertIn("req-7", msg)
        self.assertIn(f"attempts: {kp._MAX_ATTEMPTS}", msg)

    def test_daily_quota_is_not_retried_and_says_so(self):
        self.kp_service.generate_keyword_ideas.side_effect = [
            _quota_exception(
                code="RESOURCE_EXHAUSTED",
                retry_delay_s=None,
                rate_scope="DEVELOPER",
                rate_name="Operations per day for basic access",
            )
        ]
        with self.assertRaises(ToolError) as ctx:
            self._call()
        msg = str(ctx.exception)
        self.assertEqual(self.kp_service.generate_keyword_ideas.call_count, 1)
        self.assertIn("DAILY operations quota", msg)
        self.assertIn("DEVELOPER", msg)
        self.sleep.assert_not_called()

    def test_long_retry_delay_is_reported_not_slept(self):
        self.kp_service.generate_keyword_ideas.side_effect = [
            _quota_exception(retry_delay_s=kp._MAX_RETRY_WAIT_S + 1)
        ]
        with self.assertRaises(ToolError):
            self._call()
        self.assertEqual(self.kp_service.generate_keyword_ideas.call_count, 1)
        self.sleep.assert_not_called()

    def test_bare_http_429_is_retried_with_backoff_then_succeeds(self):
        # The shape the planning rate limit actually arrives in (reproduced
        # 2026-09-01): api_core ResourceExhausted, no GoogleAdsFailure.
        self.kp_service.generate_keyword_ideas.side_effect = [
            api_exceptions.ResourceExhausted(
                "Resource has been exhausted (e.g. check quota)."
            ),
            [_idea("basement developments calgary")],
        ]
        rows = self._call()
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.kp_service.generate_keyword_ideas.call_count, 2)
        self.assertEqual(self.sleep.call_args_list[0][0][0], kp._DEFAULT_RETRY_WAIT_S)

    def test_persistent_bare_429_names_the_rate_limit_and_how_to_tell(self):
        self.kp_service.generate_keyword_ideas.side_effect = [
            api_exceptions.ResourceExhausted(
                "Resource has been exhausted (e.g. check quota)."
            )
        ] * kp._MAX_ATTEMPTS
        with self.assertRaises(ToolError) as ctx:
            self._call()
        msg = str(ctx.exception)
        self.assertEqual(
            self.kp_service.generate_keyword_ideas.call_count, kp._MAX_ATTEMPTS
        )
        self.assertIn("http-429", msg)
        self.assertIn("RATE limit", msg)
        self.assertIn("60 second pause", msg)
        # backoff grows: 2s then 4s (the pacer's own sub-1.1s sleeps are
        # filtered out; with time.sleep mocked the clock never advances)
        backoffs = [c[0][0] for c in self.sleep.call_args_list if c[0][0] >= 1.5]
        self.assertEqual(
            backoffs, [kp._DEFAULT_RETRY_WAIT_S, kp._DEFAULT_RETRY_WAIT_S * 2]
        )

    def test_non_quota_error_is_not_retried(self):
        self.kp_service.generate_keyword_ideas.side_effect = [_other_exception("req-x")]
        with self.assertRaises(ToolError) as ctx:
            self._call()
        self.assertIn("Invalid geo target constant", str(ctx.exception))
        self.assertIn("req-x", str(ctx.exception))
        self.assertEqual(self.kp_service.generate_keyword_ideas.call_count, 1)

    def test_more_than_twenty_seeds_are_batched_and_merged(self):
        seeds = [f"seed {i}" for i in range(45)]
        self.kp_service.generate_keyword_ideas.side_effect = [
            [_idea("a"), _idea("b")],
            [_idea("b"), _idea("c")],
            [_idea("d")],
        ]
        rows = self._call(seeds)
        self.assertEqual(self.kp_service.generate_keyword_ideas.call_count, 3)
        sent = [
            list(r.keyword_seed.keywords.extend.call_args[0][0]) for r in self.requests
        ]
        self.assertEqual([len(s) for s in sent], [20, 20, 5])
        self.assertEqual([r["keyword_text"] for r in rows], ["a", "b", "c", "d"])

    def test_empty_seeds_rejected_before_any_api_call(self):
        with self.assertRaises(ToolError):
            self._call(seeds=("", "   "))
        self.kp_service.generate_keyword_ideas.assert_not_called()


class TestQuotaErrorParsing(unittest.TestCase):
    def test_extracts_delay_scope_and_rate_name(self):
        ex = _quota_exception(
            retry_delay_s=4.5, rate_scope="ACCOUNT", rate_name="Requests per account"
        )
        info = kp._quota_error(ex)
        self.assertEqual(info["code"], "RESOURCE_EXHAUSTED")
        self.assertAlmostEqual(info["retry_delay_s"], 4.5, places=6)
        self.assertEqual(info["rate_scope"], "ACCOUNT")
        self.assertEqual(info["rate_name"], "Requests per account")

    def test_non_quota_returns_none(self):
        self.assertIsNone(kp._quota_error(_other_exception()))

    def test_daily_heuristic(self):
        self.assertTrue(
            kp._looks_like_daily_quota(
                {"retry_delay_s": None, "rate_name": "Operations per day"}
            )
        )
        self.assertFalse(
            kp._looks_like_daily_quota(
                {"retry_delay_s": 3.0, "rate_name": "Operations per day"}
            )
        )
        self.assertFalse(
            kp._looks_like_daily_quota(
                {"retry_delay_s": None, "rate_name": "Requests per account"}
            )
        )
        self.assertFalse(
            kp._looks_like_daily_quota({"retry_delay_s": None, "rate_name": None})
        )


if __name__ == "__main__":
    unittest.main()
