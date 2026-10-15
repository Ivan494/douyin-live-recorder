"""Opt-in, process-local protection for read-only account lookups.

No I/O or dependencies: clocks, waits and randomness can all be replaced by
offline tests. A failure is one completed account check, not each retry.
"""

from dataclasses import dataclass
import hashlib
import logging
import math
import random
import threading
import time
from urllib.parse import urlsplit


LOOKUP_DEFAULTS = {
    "media_lookup_resilience_enabled": False,
    "media_lookup_max_attempts": 3,
    "media_lookup_backoff_base_seconds": 1.0,
    "media_lookup_backoff_max_seconds": 8.0,
    "media_lookup_failure_threshold": 3,
    "media_lookup_cooldown_seconds": 900.0,
}


def _number(settings, key, low, high, *, integer=False):
    default = LOOKUP_DEFAULTS[key]
    raw = settings.get(key, default)
    try:
        value = float(raw)
        if isinstance(raw, bool) or not math.isfinite(value):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        value = default
    value = max(low, min(high, value))
    return int(value) if integer else value


@dataclass(frozen=True)
class LookupPolicy:
    max_attempts: int
    base_seconds: float
    max_seconds: float
    failure_threshold: int
    cooldown_seconds: float

    @classmethod
    def from_settings(cls, settings):
        # Do not turn a malformed string such as "false" into an opt-in.
        if settings.get("media_lookup_resilience_enabled") is not True:
            return None
        cap = _number(settings, "media_lookup_backoff_max_seconds", 0.1, 60)
        return cls(
            _number(settings, "media_lookup_max_attempts", 1, 5, integer=True),
            min(cap, _number(settings, "media_lookup_backoff_base_seconds", 0.1, 60)),
            cap,
            _number(settings, "media_lookup_failure_threshold", 1, 20, integer=True),
            _number(settings, "media_lookup_cooldown_seconds", 1, 86400),
        )


def account_key(profile):
    """Never retain or log cookies, signed queries, proxy credentials or names."""
    identity = profile.get("id")
    if not identity:
        parsed = urlsplit(profile.get("original_profile_url") or profile.get("url") or "")
        identity = (parsed.hostname or "") + parsed.path
    return hashlib.sha256(str(identity).encode("utf-8")).hexdigest()


def retry_lookup(operation, policy, *, retryable, wait, check_cancelled,
                 uniform=None, label="lookup"):
    if policy is None:
        return operation()
    uniform = uniform or random.uniform
    for attempt in range(1, policy.max_attempts + 1):
        check_cancelled()
        try:
            return operation()
        except InterruptedError:
            raise
        except Exception as exc:
            if attempt == policy.max_attempts or not retryable(exc):
                raise
            cap = min(policy.max_seconds, policy.base_seconds * (2 ** (attempt - 1)))
            # Equal jitter: positive delay and exponential growth, capped even
            # at the upper edge of the random interval.
            delay = uniform(cap / 2, cap)
            logging.debug("Media %s retry %d/%d in %.2fs", label, attempt + 1,
                          policy.max_attempts, delay)
            wait(delay)
            check_cancelled()


class LookupDeferred(RuntimeError):
    def __init__(self, retry_after, reason="open"):
        self.retry_after = max(0.0, retry_after)
        self.reason = reason
        super().__init__(f"Media lookup circuit {reason}; check deferred")


@dataclass
class _AccountState:
    failures: int = 0
    opened_at: float | None = None
    in_flight: bool = False


class AccountCircuitBreaker:
    def __init__(self, *, clock=None):
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._states = {}

    def remaining(self, key, policy):
        with self._lock:
            state = self._states.get(key)
            if state is None or state.opened_at is None:
                return 0.0
            return max(0.0, policy.cooldown_seconds - (self._clock() - state.opened_at))

    def acquire(self, key, policy):
        with self._lock:
            state = self._states.setdefault(key, _AccountState())
            if state.in_flight:
                raise LookupDeferred(1, "busy")
            probe = state.opened_at is not None
            if probe:
                remaining = policy.cooldown_seconds - (self._clock() - state.opened_at)
                if remaining > 0:
                    raise LookupDeferred(remaining)
                logging.info("Media lookup circuit half-open (account %s)", key[:12])
            state.in_flight = True
            return state, probe

    def finish(self, key, state, policy, *, failed=None):
        """None means cancellation: release admission without counting failure."""
        with self._lock:
            state.in_flight = False
            if failed is None:
                return
            if not failed:
                if state.opened_at is not None:
                    logging.info("Media lookup circuit closed (account %s)", key[:12])
                self._states.pop(key, None)
                return
            state.failures = min(state.failures + 1, policy.failure_threshold)
            if state.opened_at is not None or state.failures >= policy.failure_threshold:
                state.opened_at = self._clock()
                logging.warning("Media lookup circuit open for %.0fs (account %s)",
                                policy.cooldown_seconds, key[:12])
