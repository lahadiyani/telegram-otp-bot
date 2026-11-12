"""Rate limiter untuk request OTP berbasis Redis sliding window.

Implementasi sederhana: counter dengan TTL window. Cocok untuk rate limit
per-user. Untuk produksi dengan traffic tinggi, pertimbangkan sliding window
log atau token bucket, tapi untuk bot OTP ini cukup memadai.
"""
from __future__ import annotations

from dataclasses import dataclass

from .storage import storage


@dataclass
class RateLimitResult:
    """Hasil pengecekan rate limit."""

    allowed: bool
    current_count: int
    max_requests: int
    retry_after_seconds: int

    @property
    def remaining_requests(self) -> int:
        return max(0, self.max_requests - self.current_count)


class RateLimiter:
    """Rate limiter untuk OTP request per user."""

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds

    async def check(self, user_id: int) -> RateLimitResult:
        """Cek dan INCREMENT counter. Return hasil rate limit.

        Side effect: counter di Redis di-increment ATOMIK. Jika user
        belum pernah request dalam window ini, TTL diset ke window_seconds.
        """
        allowed, count, retry_after = await storage.check_rate_limit(
            user_id=user_id,
            max_requests=self.max_requests,
            window_seconds=self.window_seconds,
        )
        return RateLimitResult(
            allowed=allowed,
            current_count=count,
            max_requests=self.max_requests,
            retry_after_seconds=retry_after,
        )

    async def peek(self, user_id: int) -> RateLimitResult:
        """Cek tanpa increment (preview). Dipakai untuk notifikasi state."""
        # Baca counter tanpa increment - pakai GET langsung
        # NOTE: ini tidak atomic, tapi cukup untuk preview
        from .storage import KEY_RATELIMIT

        key = f"{KEY_RATELIMIT}:{user_id}"
        raw = await storage.client.get(key)
        count = int(raw) if raw else 0
        ttl = await storage.client.ttl(key)
        retry_after = max(0, ttl if ttl and ttl > 0 else 0)

        return RateLimitResult(
            allowed=count < self.max_requests,
            current_count=count,
            max_requests=self.max_requests,
            retry_after_seconds=retry_after,
        )
