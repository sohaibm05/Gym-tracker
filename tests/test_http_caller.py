"""The HTTP caller's retry and circuit-breaker rules, without a network.

httpx.MockTransport stands in for the server and a fake clock for time, so the
breaker's recovery period can be stepped through instantly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients"))

from http_caller import CircuitBreaker, CircuitOpen, ReliableCaller  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def caller_for(responses, clock=None, retries=2, failures=3):
    """A caller whose server answers from `responses` in order.

    Each item is a status code, or an exception class to raise instead.
    """
    seen = []
    script = iter(responses)

    def handler(request):
        seen.append(request)
        item = next(script)
        if isinstance(item, type) and issubclass(item, Exception):
            raise item("simulated", request=request)
        return httpx.Response(item, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test",
                          timeout=2.0)
    breaker = CircuitBreaker(failures, 10.0, clock=clock or Clock())
    return ReliableCaller(client, breaker, retries=retries, sleep=lambda _s: None), seen


class TestRetry:
    def test_a_success_is_returned_at_once(self):
        caller, seen = caller_for([201])
        assert caller.request("t", "POST", "/x").status_code == 201
        assert len(seen) == 1

    def test_a_timeout_is_retried_with_the_same_key_and_request_id(self):
        caller, seen = caller_for([httpx.ReadTimeout, 200])
        response = caller.request("t", "POST", "/x", headers={"Idempotency-Key": "k1"})
        assert response.status_code == 200
        assert [r.headers["Idempotency-Key"] for r in seen] == ["k1", "k1"]
        assert seen[0].headers["X-Request-ID"] == seen[1].headers["X-Request-ID"]

    def test_a_5xx_is_retried(self):
        caller, seen = caller_for([503, 502, 201])
        assert caller.request("t", "POST", "/x").status_code == 201
        assert len(seen) == 3

    @pytest.mark.parametrize("status", [400, 404, 409, 429])
    def test_a_4xx_is_not_retried(self, status):
        caller, seen = caller_for([status])
        assert caller.request("t", "POST", "/x").status_code == status
        assert len(seen) == 1

    def test_the_retry_limit_is_respected(self):
        caller, seen = caller_for([httpx.ConnectError] * 3, failures=10)
        with pytest.raises(ConnectionError):
            caller.request("t", "POST", "/x")
        assert len(seen) == 3  # one attempt plus two retries


class TestCircuitBreaker:
    def test_opens_after_consecutive_failures_and_blocks_without_a_request(self):
        clock = Clock()
        caller, seen = caller_for([httpx.ConnectError] * 3, clock=clock)
        with pytest.raises(ConnectionError):
            # Three attempts fail; the third both spends the retries and opens
            # the breaker.
            caller.request("t", "POST", "/x")
        assert caller.breaker.state == CircuitBreaker.OPEN
        sent = len(seen)
        with pytest.raises(CircuitOpen):
            caller.request("t", "POST", "/x")
        assert len(seen) == sent

    def test_recovers_through_one_trial_call(self):
        clock = Clock()
        caller, seen = caller_for([httpx.ConnectError] * 3 + [201], clock=clock)
        with pytest.raises(ConnectionError):
            caller.request("t", "POST", "/x")
        with pytest.raises(CircuitOpen):
            caller.request("t", "POST", "/x")
        clock.now += 10.0
        assert caller.request("t", "POST", "/x").status_code == 201
        assert caller.breaker.state == CircuitBreaker.CLOSED
        assert caller.breaker.failures == 0

    def test_a_failed_trial_reopens_at_once(self):
        clock = Clock()
        breaker = CircuitBreaker(3, 10.0, clock=clock)
        for _ in range(3):
            breaker.record_failure()
        clock.now += 10.0
        breaker.before_call()
        assert breaker.state == CircuitBreaker.HALF_OPEN
        breaker.record_failure()
        assert breaker.state == CircuitBreaker.OPEN
        with pytest.raises(CircuitOpen):
            breaker.before_call()

    def test_a_success_resets_the_count(self):
        breaker = CircuitBreaker(3, 10.0, clock=Clock())
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        assert breaker.state == CircuitBreaker.CLOSED

