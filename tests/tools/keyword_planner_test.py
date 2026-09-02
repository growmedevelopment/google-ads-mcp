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

"""Test cases for generate_keyword_ideas: pacing, quota retry, paging."""

import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from google.api_core import exceptions as api_exceptions

from ads_mcp.tools import keyword_planner as kp


class _RepeatedField(list):
    """Stands in for a proto repeated field (list + .extend recorded)."""


class _FakeRequest:
    """Records every request the tool builds, so request shape is assertable.

    The tool constructs requests as `type(utils.get_googleads_type(...))()`,
    so instantiating this class is what the code under test actually does."""

    instances = []

    def __init__(self):
        self.customer_id = None
        self.language = None
        self.geo_target_constants = _RepeatedField()
        self.include_adult_keywords = None
        self.keyword_plan_network = None
        self.page_size = None
        self.page_token = ""
        self.keyword_seed = None
        _FakeRequest.instances.append(self)


class _FakeSeed:
    def __init__(self):
        self.keywords = _RepeatedField()


class _Page:
    """One GenerateKeywordIdeasResponse page (what a pager proxies to)."""

    def __init__(self, results, next_page_token=""):
        self.results = results
        self.next_page_token = next_page_token


class _PagedService:
    """Serves pages in response to page_token, the way the API does.

    `raise_on_page` makes that page's RPC fail, so a retry re-issues a real
    request rather than resuming a dead generator."""

    def __init__(self, page_results, raise_on_page=None):
        self.page_results = list(page_results)
        self.raise_on_page = raise_on_page
        self.rpc_count = 0
        self.tokens_seen = []

    def __call__(self, request=None):
        token = getattr(request, "page_token", "") or ""
        index = int(token) if token else 0
        self.rpc_count += 1
        self.tokens_seen.append(token)
        if self.raise_on_page == index:
            raise _bare_429_exception()
        nxt = str(index + 1) if index + 1 < len(self.page_results) else ""
        return _Page(self.page_results[index], nxt)


def _idea(
    text,
    searches=100,
    competition="LOW",
    index=10,
    low=1_000_000,
    high=2_000_000,
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
    """A GoogleAdsException carrying one QuotaError with optional details."""
    error = MagicMock()
    error.message = "Too many requests. Retry in 3 seconds."
    error.error_code.quota_error.name = code
    details = error.details.quota_error_details
    if retry_delay_s is None:
        details.retry_delay = None
    else:
        details.retry_delay.seconds = int(retry_delay_s)
        details.retry_delay.nanos = int(
            (retry_delay_s - int(retry_delay_s)) * 1e9
        )
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


def _bare_429_exception(
    message="Resource has been exhausted (e.g. check quota).",
):
    return api_exceptions.ResourceExhausted(message)


class _KeywordPlannerTestBase(unittest.TestCase):
    def setUp(self):
        kp._last_call_monotonic.clear()
        _FakeRequest.instances = []
        self.get_service = patch("ads_mcp.utils.get_googleads_service").start()
        self.get_client = patch("ads_mcp.utils.get_googleads_client").start()
        self.get_type = patch("ads_mcp.utils.get_googleads_type").start()
        self.pacer_sleep = patch(
            "ads_mcp.tools.keyword_planner._pacer_sleep"
        ).start()
        self.retry_sleep = patch(
            "ads_mcp.tools.keyword_planner._retry_sleep"
        ).start()
        self.addCleanup(patch.stopall)

        self.kp_service = MagicMock()
        self.gas = MagicMock()
        self.gas.language_constant_path.side_effect = (
            lambda v: f"languageConstants/{v}"
        )
        self.gas.geo_target_constant_path.side_effect = (
            lambda v: f"geoTargetConstants/{v}"
        )
        self.get_service.side_effect = lambda name: (
            self.kp_service if name == "KeywordPlanIdeaService" else self.gas
        )
        self.get_type.side_effect = lambda name: (
            _FakeRequest()
            if name == "GenerateKeywordIdeasRequest"
            else _FakeSeed()
        )

    @property
    def requests(self):
        # The tool fetches each proto type once (outside the batch loop) to
        # get its class; that probe instance is never populated. Only the
        # requests the tool actually filled in are interesting here.
        return [r for r in _FakeRequest.instances if r.customer_id is not None]

    def call(self, seeds=("basement renovations calgary",), **kw):
        params = dict(
            customer_id="975-512-9455",
            seed_keywords=list(seeds),
            geo_target_ids=["2124"],
        )
        params.update(kw)
        return kp.generate_keyword_ideas(**params)


class TestRequestShape(_KeywordPlannerTestBase):
    """The request-construction block moved into the batch loop, so assert it."""

    def test_result_mapping_and_customer_id(self):
        self.kp_service.generate_keyword_ideas.return_value = [
            _idea(
                "basement builders calgary",
                searches=480,
                competition="HIGH",
                index=77,
            )
        ]
        rows = self.call()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["keyword_text"], "basement builders calgary")
        self.assertEqual(rows[0]["avg_monthly_searches"], 480)
        self.assertEqual(rows[0]["competition_level"], "HIGH")
        self.assertEqual(rows[0]["competition_index"], 77)
        self.assertEqual(rows[0]["low_cpc"], 1.0)
        self.assertEqual(rows[0]["high_cpc"], 2.0)
        self.assertEqual(self.requests[0].customer_id, "9755129455")

    def test_every_request_field_is_set(self):
        self.kp_service.generate_keyword_ideas.return_value = []
        self.call(
            geo_target_ids=["2124", "2840"], language_id="1002", page_size=250
        )
        req = self.requests[0]
        self.assertEqual(req.language, "languageConstants/1002")
        self.assertEqual(
            list(req.geo_target_constants),
            ["geoTargetConstants/2124", "geoTargetConstants/2840"],
        )
        self.assertIs(req.include_adult_keywords, False)
        self.assertEqual(req.page_size, 250)
        self.get_client.return_value.enums.KeywordPlanNetworkEnum.__getitem__.assert_called_with(
            "GOOGLE_SEARCH"
        )
        self.assertEqual(
            list(req.keyword_seed.keywords), ["basement renovations calgary"]
        )

    def test_more_than_twenty_seeds_batched_and_merged(self):
        seeds = [f"seed {i}" for i in range(45)]
        self.kp_service.generate_keyword_ideas.side_effect = [
            [_idea("a"), _idea("b")],
            [_idea("b"), _idea("c")],
            [_idea("d")],
        ]
        rows = self.call(seeds)
        self.assertEqual(self.kp_service.generate_keyword_ideas.call_count, 3)
        self.assertEqual(
            [len(r.keyword_seed.keywords) for r in self.requests], [20, 20, 5]
        )
        # every batch carries the full request shape, not just the first
        for req in self.requests:
            self.assertIs(req.include_adult_keywords, False)
            self.assertEqual(req.language, "languageConstants/1000")
        self.assertEqual(
            [r["keyword_text"] for r in rows], ["a", "b", "c", "d"]
        )

    def test_empty_seeds_rejected_before_any_api_call(self):
        with self.assertRaises(ToolError):
            self.call(seeds=("", "   "))
        self.kp_service.generate_keyword_ideas.assert_not_called()


class TestPaging(_KeywordPlannerTestBase):
    """Pages after the first are real RPCs; they must be paced and protected."""

    def test_all_pages_merged(self):
        svc = _PagedService([[_idea("a")], [_idea("b")], [_idea("c")]])
        self.kp_service.generate_keyword_ideas.side_effect = svc
        rows = self.call()
        self.assertEqual([r["keyword_text"] for r in rows], ["a", "b", "c"])
        self.assertEqual(svc.rpc_count, 3)
        self.assertEqual(svc.tokens_seen, ["", "1", "2"])

    def test_every_page_goes_through_the_pacer(self):
        svc = _PagedService([[_idea("a")], [_idea("b")], [_idea("c")]])
        self.kp_service.generate_keyword_ideas.side_effect = svc
        # clock: each _wait_for_slot reads monotonic twice; keep the gap small
        # so every call after the first has to wait.
        clock = iter([100.0 + 0.01 * i for i in range(40)])
        with patch(
            "ads_mcp.tools.keyword_planner.time.monotonic",
            side_effect=lambda: next(clock),
        ):
            self.call()
        # page 1 finds the slot free; pages 2 and 3 each wait
        self.assertEqual(self.pacer_sleep.call_count, 2)

    def test_429_on_a_later_page_is_retried_not_swallowed(self):
        # page 2 (index 1) fails every time -> the tool must raise, never
        # silently return page 1 only.
        svc = _PagedService([[_idea("a")], [_idea("b")]], raise_on_page=1)
        self.kp_service.generate_keyword_ideas.side_effect = svc
        with self.assertRaises(ToolError) as ctx:
            self.call()
        self.assertIn("quota error", str(ctx.exception))
        # 1 successful first page + _MAX_ATTEMPTS attempts at page 2
        self.assertEqual(svc.rpc_count, 1 + kp._MAX_ATTEMPTS)

    def test_results_are_capped(self):
        pages = [[_idea(f"k{i}-{j}") for j in range(500)] for i in range(10)]
        svc = _PagedService(pages)
        self.kp_service.generate_keyword_ideas.side_effect = svc
        rows = self.call()
        self.assertLessEqual(len(rows), kp._MAX_RESULTS)
        self.assertGreaterEqual(len(rows), 500)
        self.assertLess(svc.rpc_count, 10)


class TestPacer(_KeywordPlannerTestBase):
    def test_second_call_to_same_customer_waits(self):
        self.kp_service.generate_keyword_ideas.return_value = []
        clock = iter([100.0, 100.0, 100.2, 101.3])
        with patch(
            "ads_mcp.tools.keyword_planner.time.monotonic",
            side_effect=lambda: next(clock),
        ):
            self.call()
            self.call()
        self.pacer_sleep.assert_called_once()
        self.assertAlmostEqual(
            self.pacer_sleep.call_args[0][0], kp._MIN_INTERVAL_S - 0.2, places=6
        )

    def test_no_wait_when_enough_time_passed(self):
        self.kp_service.generate_keyword_ideas.return_value = []
        clock = iter([100.0, 100.0, 105.0, 105.0])
        with patch(
            "ads_mcp.tools.keyword_planner.time.monotonic",
            side_effect=lambda: next(clock),
        ):
            self.call()
            self.call()
        self.pacer_sleep.assert_not_called()

    def test_different_customers_do_not_throttle_each_other(self):
        self.kp_service.generate_keyword_ideas.return_value = []
        clock = iter([100.0, 100.0, 100.1, 100.1])
        with patch(
            "ads_mcp.tools.keyword_planner.time.monotonic",
            side_effect=lambda: next(clock),
        ):
            self.call(customer_id="1111111111")
            self.call(customer_id="2222222222")
        self.pacer_sleep.assert_not_called()

    def test_concurrent_callers_are_serialized_by_the_lock(self):
        """Threads that arrive together must be granted slots one interval
        apart. Without the lock they all read the same empty slot and are
        granted at once, so this fails if the lock is ever dropped."""
        interval = 0.15
        grants = []
        record_lock = threading.Lock()
        start = threading.Barrier(4)

        # a real sleep, so callers genuinely overlap inside the pacer
        with (
            patch.object(kp, "_MIN_INTERVAL_S", interval),
            patch.object(kp, "_pacer_sleep", time.sleep),
        ):

            def worker():
                start.wait()
                kp._wait_for_slot("9755129455")
                with record_lock:
                    grants.append(time.monotonic())

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(len(grants), 4)
        grants.sort()
        gaps = [b - a for a, b in zip(grants, grants[1:])]
        for gap in gaps:
            self.assertGreater(
                gap,
                interval * 0.6,
                f"slots were granted {gap:.3f}s apart, expected >= {interval}s "
                "— the pacer is not serializing concurrent callers",
            )

    def test_queue_longer_than_the_ceiling_is_refused(self):
        kp._last_call_monotonic["9755129455"] = 100.0
        with (
            patch.object(kp, "_MIN_INTERVAL_S", kp._MAX_PACER_QUEUE_S + 5),
            patch(
                "ads_mcp.tools.keyword_planner.time.monotonic",
                return_value=100.0,
            ),
        ):
            with self.assertRaises(ToolError) as ctx:
                kp._wait_for_slot("9755129455")
        self.assertIn("once at a time", str(ctx.exception))
        self.pacer_sleep.assert_not_called()


class TestQuotaRetry(_KeywordPlannerTestBase):
    def test_retries_after_googles_retry_delay_then_succeeds(self):
        self.kp_service.generate_keyword_ideas.side_effect = [
            _quota_exception(
                retry_delay_s=3.0, rate_name="Requests per account"
            ),
            [_idea("legal basement suites calgary")],
        ]
        rows = self.call()
        self.assertEqual(
            [r["keyword_text"] for r in rows], ["legal basement suites calgary"]
        )
        self.assertEqual(self.kp_service.generate_keyword_ideas.call_count, 2)
        self.retry_sleep.assert_called_once_with(3.0)

    def test_default_backoff_when_no_delay_supplied(self):
        self.kp_service.generate_keyword_ideas.side_effect = [
            _quota_exception(code="RESOURCE_TEMPORARILY_EXHAUSTED"),
            [],
        ]
        self.call()
        self.retry_sleep.assert_called_once_with(kp._DEFAULT_RETRY_WAIT_S)

    def test_bare_429_is_retried_then_succeeds(self):
        self.kp_service.generate_keyword_ideas.side_effect = [
            _bare_429_exception(),
            [_idea("basement developments calgary")],
        ]
        rows = self.call()
        self.assertEqual(len(rows), 1)
        self.retry_sleep.assert_called_once_with(kp._DEFAULT_RETRY_WAIT_S)

    def test_persistent_bare_429_reports_the_ambiguity_honestly(self):
        self.kp_service.generate_keyword_ideas.side_effect = [
            _bare_429_exception()
        ] * kp._MAX_ATTEMPTS
        with self.assertRaises(ToolError) as ctx:
            self.call()
        msg = str(ctx.exception)
        self.assertIn("http-429", msg)
        self.assertIn("cannot be read off this response", msg)
        self.assertIn("rate limit is far likelier", msg)
        self.assertEqual(
            [c[0][0] for c in self.retry_sleep.call_args_list],
            [kp._DEFAULT_RETRY_WAIT_S, kp._DEFAULT_RETRY_WAIT_S * 2],
        )

    def test_persistent_account_scoped_limit_names_the_rate_limit(self):
        self.kp_service.generate_keyword_ideas.side_effect = [
            _quota_exception(
                retry_delay_s=2.0,
                rate_scope="ACCOUNT",
                rate_name="Requests per account",
            )
        ] * kp._MAX_ATTEMPTS
        with self.assertRaises(ToolError) as ctx:
            self.call()
        msg = str(ctx.exception)
        self.assertIn("RATE limit", msg)
        self.assertIn("not the daily quota", msg)
        self.assertIn("Requests per account", msg)

    def test_developer_scoped_quota_is_daily_and_not_retried(self):
        self.kp_service.generate_keyword_ideas.side_effect = [
            _quota_exception(
                retry_delay_s=None,
                rate_scope="DEVELOPER",
                rate_name="Get requests for standard access",
            )
        ]
        with self.assertRaises(ToolError) as ctx:
            self.call()
        msg = str(ctx.exception)
        self.assertEqual(self.kp_service.generate_keyword_ideas.call_count, 1)
        self.assertIn("DAILY operations quota", msg)
        self.retry_sleep.assert_not_called()

    def test_long_retry_delay_is_reported_not_slept(self):
        self.kp_service.generate_keyword_ideas.side_effect = [
            _quota_exception(retry_delay_s=120.0)
        ]
        with self.assertRaises(ToolError):
            self.call()
        self.assertEqual(self.kp_service.generate_keyword_ideas.call_count, 1)
        self.retry_sleep.assert_not_called()
        self.assertLessEqual(kp._MAX_RETRY_WAIT_S, 60.0)

    def test_non_quota_error_is_not_retried(self):
        self.kp_service.generate_keyword_ideas.side_effect = [
            _other_exception("req-x")
        ]
        with self.assertRaises(ToolError) as ctx:
            self.call()
        self.assertIn("Invalid geo target constant", str(ctx.exception))
        self.assertIn("req-x", str(ctx.exception))
        self.assertEqual(self.kp_service.generate_keyword_ideas.call_count, 1)


class TestQuotaClassification(unittest.TestCase):
    def test_scope_decides_before_rate_name(self):
        # DEVELOPER is the developer-token bucket, whatever the name says
        self.assertTrue(
            kp._looks_like_daily_quota(
                {
                    "rate_scope": "DEVELOPER",
                    "rate_name": "Get requests for standard access",
                    "retry_delay_s": 3.0,
                }
            )
        )
        # ACCOUNT is the per-customer bucket, even when the name says operations
        self.assertFalse(
            kp._looks_like_daily_quota(
                {
                    "rate_scope": "ACCOUNT",
                    "rate_name": "Operations per minute",
                    "retry_delay_s": None,
                }
            )
        )

    def test_bare_word_operations_is_not_enough(self):
        self.assertFalse(
            kp._looks_like_daily_quota(
                {
                    "rate_scope": None,
                    "rate_name": "Operations per minute",
                    "retry_delay_s": None,
                }
            )
        )
        self.assertTrue(
            kp._looks_like_daily_quota(
                {
                    "rate_scope": None,
                    "rate_name": "Operations per day",
                    "retry_delay_s": None,
                }
            )
        )

    def test_unnamed_bare_429_is_ambiguous_not_asserted(self):
        info = kp._bare_429(_bare_429_exception())
        self.assertFalse(kp._looks_like_daily_quota(info))
        self.assertTrue(kp._is_ambiguous_quota(info))

    def test_recovered_failure_is_not_ambiguous(self):
        self.assertFalse(
            kp._is_ambiguous_quota(
                {"shape": "http-429+failure", "rate_scope": "ACCOUNT"}
            )
        )


class TestQuotaErrorParsing(unittest.TestCase):
    def test_extracts_delay_scope_and_rate_name(self):
        info = kp._quota_error(
            _quota_exception(
                retry_delay_s=4.5,
                rate_scope="ACCOUNT",
                rate_name="Requests per account",
            )
        )
        self.assertEqual(info["code"], "RESOURCE_EXHAUSTED")
        self.assertAlmostEqual(info["retry_delay_s"], 4.5, places=6)
        self.assertEqual(info["rate_scope"], "ACCOUNT")
        self.assertEqual(info["rate_name"], "Requests per account")

    def test_non_quota_returns_none(self):
        self.assertIsNone(kp._quota_error(_other_exception()))

    def test_trailing_metadata_without_a_failure_returns_none(self):
        response = MagicMock()
        response.trailing_metadata.return_value = [("request-id", b"abc")]
        ex = api_exceptions.ResourceExhausted("exhausted", response=response)
        self.assertIsNone(kp._quota_from_trailing_metadata(ex))

    def test_trailing_metadata_failure_is_recovered(self):
        failure = MagicMock()
        error = MagicMock()
        error.message = "Too many requests."
        error.error_code.quota_error.name = "RESOURCE_EXHAUSTED"
        details = error.details.quota_error_details
        details.retry_delay = None
        details.rate_scope.name = "DEVELOPER"
        details.rate_name = "Operations per day"
        failure.errors = [error]
        response = MagicMock()
        response.trailing_metadata.return_value = [
            ("google.ads.googleads.v25.errors.googleadsfailure-bin", b"payload")
        ]
        ex = api_exceptions.ResourceExhausted("exhausted", response=response)
        with patch("ads_mcp.utils.get_googleads_type") as gt:
            gt.return_value.__class__.deserialize = MagicMock(
                return_value=failure
            )
            with patch.object(
                type(gt.return_value),
                "deserialize",
                create=True,
                return_value=failure,
            ):
                info = kp._quota_from_trailing_metadata(ex)
        self.assertIsNotNone(info)
        self.assertEqual(info["rate_scope"], "DEVELOPER")
        self.assertEqual(info["shape"], "http-429+failure")
        self.assertTrue(kp._looks_like_daily_quota(info))

    def test_no_response_attribute_is_handled(self):
        self.assertIsNone(
            kp._quota_from_trailing_metadata(_bare_429_exception())
        )


if __name__ == "__main__":
    unittest.main()
