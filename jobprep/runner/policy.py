"""Bounded, restart-safe task retry policy; tool-local retries are separate."""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class RunnerPolicy:
    max_attempts: int = 3
    automatic_retry: bool = True
    base_delay_seconds: float = 5
    max_delay_seconds: float = 60
    wait_for_retries: bool = True
    max_wait_seconds: float = 60

    @classmethod
    def from_config(cls, values=None):
        policy = cls(**(values or {}))
        if isinstance(policy.max_attempts, bool) or not isinstance(policy.max_attempts, int) or not 1 <= policy.max_attempts <= 20:
            raise ValueError('runner.max_attempts must be an integer from 1 to 20')
        if not isinstance(policy.automatic_retry, bool) or not isinstance(policy.wait_for_retries, bool):
            raise ValueError('runner retry flags must be boolean')
        for value in (policy.base_delay_seconds, policy.max_delay_seconds, policy.max_wait_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError('runner delays must be finite nonnegative seconds')
        return policy

    def delay(self, attempt):
        return min(self.max_delay_seconds, self.base_delay_seconds * 2 ** max(0, attempt - 1))

    def retryable(self, result):
        if not self.automatic_retry or result.get('status') != 'error' or result.get('retryable') is False:
            return False
        return (result.get('retryable') is True or
                result.get('error_kind') in {'timeout', 'transport', 'rate_limited', 'upstream_unavailable'} or
                result.get('http_status') in {408, 425, 429, 500, 502, 503, 504})
