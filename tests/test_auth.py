"""Passwords, usernames, sessions and rate limiting.

Everything here is the part of auth.py that does not need a database. The SQL
side is covered by tests/test_multi_user.py, which drives it through the app.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import auth  # noqa: E402

# Real hashing at 600k rounds costs about a third of a second a call, and these
# tests call it a lot. The cost factor is what is being kept out of the test, not
# the algorithm: every assertion below still runs the genuine PBKDF2 path.
FAST = 1_000


class TestPasswordHashing:
    def test_a_password_verifies_against_its_own_hash(self):
        stored = auth.hash_password("correct horse battery", FAST)
        assert auth.verify_password("correct horse battery", stored)

    def test_a_wrong_password_does_not(self):
        stored = auth.hash_password("correct horse battery", FAST)
        assert not auth.verify_password("correct horse batteru", stored)

    def test_the_password_is_not_recoverable_from_the_hash(self):
        assert "hunter2hunter2" not in auth.hash_password("hunter2hunter2", FAST)

    def test_two_hashes_of_one_password_differ(self):
        """Per-hash salt: identical passwords must not produce identical rows,
        or the table itself would show which accounts share a password."""
        first = auth.hash_password("same password", FAST)
        second = auth.hash_password("same password", FAST)
        assert first != second
        assert auth.verify_password("same password", first)
        assert auth.verify_password("same password", second)

    def test_the_cost_factor_travels_with_the_hash(self):
        stored = auth.hash_password("a password here", 2_000)
        assert stored.split("$")[1] == "2000"
        # Verifying does not need to be told the count - which is what makes it
        # safe to raise the default later without locking anyone out.
        assert auth.verify_password("a password here", stored)

    @pytest.mark.parametrize(
        "stored",
        [
            "",
            "not-a-hash",
            "pbkdf2_sha256$notanumber$c2FsdA$aGFzaA",
            "pbkdf2_sha256$0$c2FsdA$aGFzaA",
            "bcrypt$12$c2FsdA$aGFzaA",
            "pbkdf2_sha256$1000$!!!$aGFzaA",
        ],
    )
    def test_a_malformed_hash_never_authenticates(self, stored):
        """A row this function cannot read is a failed login, not an exception -
        and never an accidental success."""
        assert not auth.verify_password("anything at all", stored)

    def test_rehash_is_asked_for_when_the_cost_factor_has_risen(self):
        assert auth.needs_rehash(auth.hash_password("password!", FAST))
        assert not auth.needs_rehash(
            auth.hash_password("password!", auth.PBKDF2_ITERATIONS)
        )

    def test_rehash_is_asked_for_on_an_unreadable_hash(self):
        assert auth.needs_rehash("bcrypt$12$whatever")


class TestUsernames:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("alice", "alice"),
            ("  Alice  ", "alice"),
            ("SOHAIB", "sohaib"),
            ("a.b_c-d", "a.b_c-d"),
            ("lifter99", "lifter99"),
        ],
    )
    def test_accepted_names_fold_to_lowercase(self, raw, expected):
        assert auth.normalize_username(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "  ",
            "ab",                       # too short
            "x" * 33,                   # too long
            "has space",
            "has@sign",
            ".leading",
            "trailing-",
            "admin",                    # reserved
            "LOGIN",                    # reserved, whatever the case
        ],
    )
    def test_rejected_names_explain_themselves(self, raw):
        with pytest.raises(auth.AuthError):
            auth.normalize_username(raw)

    def test_case_folding_is_what_stops_a_lookalike_account(self):
        """'Alice' and 'alice' normalize to one stored name, so the unique index
        rejects the second - the impersonation this is really guarding against."""
        assert auth.normalize_username("Alice") == auth.normalize_username("alice")


class TestPasswordRules:
    def test_a_long_enough_password_is_returned_unchanged(self):
        assert auth.validate_password("longenough") == "longenough"

    @pytest.mark.parametrize("raw", ["", "short", "1234567"])
    def test_a_short_password_is_rejected(self, raw):
        with pytest.raises(auth.AuthError):
            auth.validate_password(raw)

    def test_an_absurdly_long_password_is_rejected(self):
        """Not a security rule - a bound on how much text we will run 600k
        rounds of PBKDF2 over before the caller has authenticated."""
        with pytest.raises(auth.AuthError):
            auth.validate_password("x" * (auth.PASSWORD_MAX + 1))


class TestTimezones:
    def test_a_real_zone_is_kept(self):
        assert auth.validate_timezone("Asia/Karachi") == "Asia/Karachi"

    @pytest.mark.parametrize("raw", ["", "   ", None])
    def test_blank_means_use_the_app_default(self, raw):
        assert auth.validate_timezone(raw) is None

    @pytest.mark.parametrize("raw", ["Mars/Olympus", "GMT+5", "not a zone"])
    def test_an_unloadable_zone_is_rejected(self, raw):
        with pytest.raises(auth.AuthError):
            auth.validate_timezone(raw)


class TestSessionTokens:
    def test_the_raw_token_is_never_what_gets_stored(self):
        token = "a-token-value"
        assert auth._token_hash(token) != token
        assert len(auth._token_hash(token)) == 64

    def test_hashing_is_stable(self):
        assert auth._token_hash("abc") == auth._token_hash("abc")

    def test_different_tokens_hash_differently(self):
        assert auth._token_hash("abc") != auth._token_hash("abd")

    def test_cookie_lifetime_matches_the_session_lifetime(self):
        assert auth.session_cookie_max_age(30) == 30 * 24 * 3600


class TestSessionRefreshWindow:
    """`resolve_session` only writes once a session is half spent, so an active
    user is never logged out and an idle one still expires on time."""

    def _window(self, age_days: float, ttl_days: int = 30):
        now = datetime.now(timezone.utc)
        created = now - timedelta(days=age_days)
        return created, created + timedelta(days=ttl_days)

    def test_a_fresh_session_is_not_rewritten(self):
        created, expires = self._window(age_days=1)
        assert not auth._past_halfway(created, expires)

    def test_a_session_past_the_midpoint_is_extended(self):
        created, expires = self._window(age_days=20)
        assert auth._past_halfway(created, expires)

    def test_missing_timestamps_extend_rather_than_crash(self):
        assert auth._past_halfway(None, None)

    def test_naive_timestamps_are_read_as_utc(self):
        """Some drivers hand back naive datetimes; comparing one against an
        aware `now` would raise, and this path runs on every request."""
        created = datetime.utcnow() - timedelta(days=20)
        assert auth._past_halfway(created, created + timedelta(days=30))


class TestRateLimiter:
    def test_attempts_up_to_the_limit_are_allowed(self):
        limiter = auth.RateLimiter(limit=3, window_seconds=60)
        assert [limiter.check("ip", now=100.0) for _ in range(3)] == [True] * 3

    def test_the_next_attempt_is_refused(self):
        limiter = auth.RateLimiter(limit=3, window_seconds=60)
        for _ in range(3):
            limiter.check("ip", now=100.0)
        assert limiter.check("ip", now=100.0) is False

    def test_the_window_moves_on(self):
        limiter = auth.RateLimiter(limit=2, window_seconds=60)
        limiter.check("ip", now=100.0)
        limiter.check("ip", now=100.0)
        assert limiter.check("ip", now=100.0) is False
        assert limiter.check("ip", now=200.0) is True

    def test_one_caller_being_limited_does_not_limit_another(self):
        """The whole point of keying by client: an attacker hammering the login
        form must not lock everyone else out of their own account."""
        limiter = auth.RateLimiter(limit=1, window_seconds=60)
        assert limiter.check("attacker", now=100.0) is True
        assert limiter.check("attacker", now=100.0) is False
        assert limiter.check("someone-else", now=100.0) is True

    def test_a_successful_login_can_clear_the_count(self):
        limiter = auth.RateLimiter(limit=1, window_seconds=60)
        limiter.check("ip", now=100.0)
        limiter.reset("ip")
        assert limiter.check("ip", now=100.0) is True

    def test_old_keys_do_not_accumulate_forever(self):
        limiter = auth.RateLimiter(limit=1, window_seconds=1)
        for index in range(5000):
            limiter.check(f"ip-{index}", now=float(index))
        assert len(limiter._hits) < 5000
