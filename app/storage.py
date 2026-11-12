"""Storage layer async untuk OTP state menggunakan Redis.

Skema key (semua dengan prefix `otpbot:`):
  - otpbot:otp:{user_id}        -> hash {phone, phone_code_hash, expires_at, attempts, ts_sent}
  - otpbot:ratelimit:{user_id}  -> counter (TTL = window rate limit)
  - otpbot:cooldown:{user_id}   -> "1" (TTL = cooldown kirim ulang)
  - otpbot:input:{user_id}     -> string digit yang sudah diketik user pada keypad
  - otpbot:locked:{user_id}    -> "1" (TTL = lockout duration)

Semua operasi async, menggunakan redis.asyncio.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional

import redis.asyncio as redis_asyncio

from .logger import get_logger

log = get_logger(__name__)

# Prefix untuk semua key agar tidak konflik dengan app lain di Redis bersama
KEY_PREFIX = "otpbot"

KEY_OTP = f"{KEY_PREFIX}:otp"          # hash
KEY_RATELIMIT = f"{KEY_PREFIX}:ratelimit"
KEY_COOLDOWN = f"{KEY_PREFIX}:cooldown"
KEY_INPUT = f"{KEY_PREFIX}:input"
KEY_LOCKED = f"{KEY_PREFIX}:locked"


@dataclass
class OtpRecord:
    """Record OTP yang sedang berjalan untuk seorang user."""

    user_id: int
    phone: str
    phone_code_hash: str
    expires_at: float  # unix timestamp
    attempts_used: int
    ts_sent: float

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    @property
    def remaining_seconds(self) -> int:
        return max(0, int(self.expires_at - time.time()))

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "phone": self.phone,
            "phone_code_hash": self.phone_code_hash,
            "expires_at": self.expires_at,
            "attempts_used": self.attempts_used,
            "ts_sent": self.ts_sent,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OtpRecord":
        return cls(
            user_id=int(d["user_id"]),
            phone=d["phone"],
            phone_code_hash=d["phone_code_hash"],
            expires_at=float(d["expires_at"]),
            attempts_used=int(d.get("attempts_used", 0)),
            ts_sent=float(d.get("ts_sent", 0)),
        )


class Storage:
    """Wrapper async untuk Redis dengan operasi khusus OTP bot."""

    def __init__(self, redis_url: str = ""):
        self._redis_url = redis_url
        self._client: Optional[redis_asyncio.Redis] = None

    def configure(self, redis_url: str) -> None:
        """Set Redis URL sebelum memanggil connect(). Dipakai dari main.py."""
        self._redis_url = redis_url

    async def connect(self) -> None:
        """Inisialisasi koneksi Redis. Health check dengan PING."""
        if not self._redis_url:
            raise RuntimeError("REDIS_URL belum dikonfigurasi. Panggil configure() dulu.")
        self._client = redis_asyncio.from_url(
            self._redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=10,
            retry_on_timeout=True,
            health_check_interval=30,
        )
        # Test koneksi
        await self._client.ping()
        log.info("redis_connected", extra={"event": "redis_connect"})

    async def close(self) -> None:
        """Tutup koneksi Redis dengan aman."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            log.info("redis_closed", extra={"event": "redis_close"})

    @property
    def client(self) -> redis_asyncio.Redis:
        if self._client is None:
            raise RuntimeError("Redis client belum diinisialisasi. Panggil connect() dulu.")
        return self._client

    # ============================================================
    # OTP Record
    # ============================================================

    async def save_otp(self, record: OtpRecord, ttl_seconds: int) -> None:
        """Simpan record OTP dengan TTL otomatis (auto-expire)."""
        key = f"{KEY_OTP}:{record.user_id}"
        value = json.dumps(record.to_dict(), ensure_ascii=False)
        await self.client.set(key, value, ex=ttl_seconds)
        log.info(
            "otp_saved",
            extra={
                "event": "otp_save",
                "user_id": record.user_id,
                "phone": record.phone,
            },
        )

    async def get_otp(self, user_id: int) -> Optional[OtpRecord]:
        """Ambil record OTP aktif. Return None jika tidak ada / sudah expire di Redis."""
        key = f"{KEY_OTP}:{user_id}"
        raw = await self.client.get(key)
        if raw is None:
            return None
        try:
            return OtpRecord.from_dict(json.loads(raw))
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            log.warning(
                "otp_record_corrupt",
                extra={"event": "otp_corrupt", "user_id": user_id},
            )
            log.exception("Corrupt OTP record for user %s: %s", user_id, e)
            await self.client.delete(key)
            return None

    async def delete_otp(self, user_id: int) -> None:
        """Hapus record OTP (setelah verifikasi sukses atau abort)."""
        key = f"{KEY_OTP}:{user_id}"
        await self.client.delete(key)

    async def increment_attempts(self, user_id: int) -> int:
        """Tambah counter percobaan OTP salah. Return nilai baru."""
        record = await self.get_otp(user_id)
        if record is None:
            return 0
        record.attempts_used += 1
        # Pertahankan TTL asli - baca TTL lalu simpan ulang
        key = f"{KEY_OTP}:{user_id}"
        ttl = await self.client.ttl(key)
        if ttl is None or ttl < 0:
            ttl = 60  # fallback 1 menit
        await self.client.set(key, json.dumps(record.to_dict()), ex=ttl)
        return record.attempts_used

    # ============================================================
    # Rate Limiting (sliding window via INCR + EXPIRE)
    # ============================================================

    async def check_rate_limit(
        self, user_id: int, max_requests: int, window_seconds: int
    ) -> tuple[bool, int, int]:
        """Cek apakah user masih dalam batas rate limit.

        Returns:
            (allowed, current_count, retry_after_seconds)
            - allowed=True jika user boleh request lagi
            - current_count = jumlah request dalam window saat ini
            - retry_after_seconds = berapa detik lagi user boleh request (0 jika allowed)
        """
        key = f"{KEY_RATELIMIT}:{user_id}"
        # INCR atomic, lalu set EXPIRE hanya jika key baru
        current = await self.client.incr(key)
        if current == 1:
            # First request in window - set TTL
            await self.client.expire(key, window_seconds)
        if current <= max_requests:
            return True, current, 0
        # Sudah lewat batas - beri tahu user berapa lama harus nunggu
        ttl = await self.client.ttl(key)
        retry_after = max(1, ttl if ttl and ttl > 0 else window_seconds)
        return False, current, retry_after

    # ============================================================
    # Resend Cooldown
    # ============================================================

    async def set_cooldown(self, user_id: int, cooldown_seconds: int) -> None:
        """Set cooldown untuk kirim ulang OTP."""
        key = f"{KEY_COOLDOWN}:{user_id}"
        await self.client.set(key, "1", ex=cooldown_seconds)

    async def get_cooldown_remaining(self, user_id: int) -> int:
        """Sisa detik cooldown. 0 jika tidak ada cooldown aktif."""
        key = f"{KEY_COOLDOWN}:{user_id}"
        ttl = await self.client.ttl(key)
        if ttl is None or ttl < 0:
            return 0
        return ttl

    async def is_in_cooldown(self, user_id: int) -> bool:
        return await self.get_cooldown_remaining(user_id) > 0

    async def clear_cooldown(self, user_id: int) -> None:
        """Hapus cooldown (untuk resend yang sukses, agar cooldown baru bisa dipasang)."""
        await self.client.delete(f"{KEY_COOLDOWN}:{user_id}")

    # ============================================================
    # Keypad Input Buffer
    # ============================================================

    async def set_input(self, user_id: int, digits: str, ttl_seconds: int = 600) -> None:
        """Simpan string digit yang sudah diketik user pada keypad."""
        key = f"{KEY_INPUT}:{user_id}"
        await self.client.set(key, digits, ex=ttl_seconds)

    async def get_input(self, user_id: int) -> str:
        """Ambil digit yang sudah diketik. Return string kosong jika belum ada."""
        key = f"{KEY_INPUT}:{user_id}"
        raw = await self.client.get(key)
        return raw if raw else ""

    async def append_digit(self, user_id: int, digit: str, max_digits: int) -> str:
        """Tambah satu digit ke buffer. Kembalikan string baru."""
        current = await self.get_input(user_id)
        if len(current) >= max_digits:
            return current  # Sudah penuh, abaikan
        new_value = current + digit
        # Pertahankan TTL lama
        key = f"{KEY_INPUT}:{user_id}"
        ttl = await self.client.ttl(key)
        if ttl is None or ttl < 0:
            ttl = 600
        await self.client.set(key, new_value, ex=ttl)
        return new_value

    async def clear_input(self, user_id: int) -> None:
        """Kosongkan buffer input."""
        await self.client.delete(f"{KEY_INPUT}:{user_id}")

    # ============================================================
    # Lockout (setelah melebihi max attempts)
    # ============================================================

    async def lock_user(self, user_id: int, lock_seconds: int = 1800) -> None:
        """Kunci user selama lock_seconds (default 30 menit)."""
        await self.client.set(f"{KEY_LOCKED}:{user_id}", "1", ex=lock_seconds)

    async def is_locked(self, user_id: int) -> bool:
        ttl = await self.client.ttl(f"{KEY_LOCKED}:{user_id}")
        return ttl is not None and ttl > 0

    # ============================================================
    # Cleanup seluruh state untuk user (verifikasi sukses / cancel)
    # ============================================================

    async def purge_user(self, user_id: int) -> None:
        """Hapus SEMUA state Redis milik user (OTP, input, cooldown, lock)."""
        # Note: rate limit TIDAK dihapus agar abuse tetap terbatasi
        keys = [
            f"{KEY_OTP}:{user_id}",
            f"{KEY_INPUT}:{user_id}",
            f"{KEY_COOLDOWN}:{user_id}",
        ]
        if keys:
            await self.client.delete(*keys)


# Singleton storage - diinisialisasi di main.py saat startup
storage = Storage("")  # URL di-set saat connect() dipanggil dari main
