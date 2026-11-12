"""OTP service - orchestrasi lengkap flow OTP resmi Telegram.

Layer ini menggabungkan:
- Validasi nomor HP (phonenumbers library)
- Rate limiting (RateLimiter + Storage)
- Pengiriman OTP resmi (Telethon)
- Penyimpanan state (Redis via Storage)
- Cooldown kirim ulang (Storage)
- Verifikasi kode (Telethon sign_in)
- Cleanup sesi (Telethon logout + Redis purge)

Bot handlers hanya berinteraksi dengan service ini, tidak langsung
ke storage/telethon. Memudahkan testing & maintenance.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import phonenumbers
from phonenumbers import NumberParseException

from .config import settings
from .logger import get_logger
from .rate_limiter import RateLimiter, RateLimitResult
from .storage import OtpRecord, storage
from .telethon_manager import get_telethon_manager

log = get_logger(__name__)


# ============================================================
# Result Types (enum-based untuk readability)
# ============================================================


class SendOtpStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PHONE_INVALID = "PHONE_INVALID"
    RATE_LIMITED = "RATE_LIMITED"
    LOCKED = "LOCKED"
    TELETHON_FAILED = "TELETHON_FAILED"
    FLOOD_WAIT = "FLOOD_WAIT"


class VerifyOtpStatus(str, Enum):
    SUCCESS = "SUCCESS"
    NO_ACTIVE_OTP = "NO_ACTIVE_OTP"
    EXPIRED = "EXPIRED"
    WRONG_CODE = "WRONG_CODE"
    MAX_ATTEMPTS_REACHED = "MAX_ATTEMPTS_REACHED"
    LOCKED = "LOCKED"
    TELETHON_FAILED = "TELETHON_FAILED"
    FLOOD_WAIT = "FLOOD_WAIT"


@dataclass
class SendOtpOutcome:
    status: SendOtpStatus
    phone: str = ""
    expiry_seconds: int = 0
    retry_after_seconds: int = 0  # untuk RATE_LIMITED / FLOOD_WAIT
    error_message: str = ""


@dataclass
class VerifyOtpOutcome:
    status: VerifyOtpStatus
    phone: str = ""
    attempts_remaining: int = 0
    needs_2fa: bool = False
    error_message: str = ""


# ============================================================
# Service
# ============================================================


class OtpService:
    """Service layer untuk semua operasi OTP."""

    def __init__(self):
        self.rate_limiter = RateLimiter(
            max_requests=settings.rate_limit_max_requests,
            window_seconds=settings.rate_limit_window,
        )

    # ============================================================
    # Phone Validation
    # ============================================================

    @staticmethod
    def validate_phone(raw_phone: str) -> Optional[str]:
        """Validasi nomor HP dan kembalikan format E.164.

        Args:
            raw_phone: Nomor HP dari kontak Telegram, biasanya sudah ada kode negara.

        Returns:
            Nomor dalam format E.164 (contoh: +628123456789) atau None jika invalid.
        """
        if not raw_phone:
            return None
        # Pastikan ada tanda + di depan untuk parsing
        phone = raw_phone.strip()
        if not phone.startswith("+"):
            # Telegram contact kadang memberi nomor tanpa +
            phone = "+" + phone.lstrip("+")
        try:
            parsed = phonenumbers.parse(phone, None)
            if not phonenumbers.is_valid_number(parsed):
                return None
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        except NumberParseException:
            # Fallback: coba dengan region ID
            try:
                parsed = phonenumbers.parse(phone, "ID")
                if phonenumbers.is_valid_number(parsed):
                    return phonenumbers.format_number(
                        parsed, phonenumbers.PhoneNumberFormat.E164
                    )
            except NumberParseException:
                pass
            return None

    # ============================================================
    # Send OTP (initial)
    # ============================================================

    async def send_otp(self, user_id: int, raw_phone: str) -> SendOtpOutcome:
        """Kirim OTP resmi Telegram ke nomor user.

        Steps:
            1. Validasi format nomor HP
            2. Cek apakah user sedang di-lockout
            3. Cek rate limit
            4. Call Telethon send_code_request
            5. Simpan state ke Redis (dengan TTL = expiry OTP)
            6. Set cooldown kirim ulang

        Args:
            user_id: ID Telegram user
            raw_phone: Nomor HP dari kontak Telegram

        Returns:
            SendOtpOutcome dengan status dan metadata.
        """
        # 1. Validasi nomor HP
        phone = self.validate_phone(raw_phone)
        if phone is None:
            return SendOtpOutcome(
                status=SendOtpStatus.PHONE_INVALID,
                error_message="Format nomor HP tidak valid.",
            )

        # 2. Cek lockout
        if await storage.is_locked(user_id):
            return SendOtpOutcome(
                status=SendOtpStatus.LOCKED,
                error_message="User sedang di-lockout karena terlalu banyak percobaan salah.",
            )

        # 3. Cek rate limit (increment counter)
        rate: RateLimitResult = await self.rate_limiter.check(user_id)
        if not rate.allowed:
            log.warning(
                "rate_limited",
                extra={
                    "event": "rate_limited",
                    "user_id": user_id,
                    "phone": phone,
                },
            )
            return SendOtpOutcome(
                status=SendOtpStatus.RATE_LIMITED,
                phone=phone,
                retry_after_seconds=rate.retry_after_seconds,
                error_message=f"Rate limit tercapai. Coba lagi dalam {rate.retry_after_seconds} detik.",
            )

        # 4. Bersihkan state lama jika ada (OTP sebelumnya yang belum selesai)
        await self._cleanup_previous_flow(user_id)

        # 5. Kirim OTP via Telethon
        manager = get_telethon_manager()
        result = await manager.send_otp(user_id, phone)
        if not result.success:
            log.error(
                "send_otp_failed",
                extra={
                    "event": "send_otp_fail",
                    "user_id": user_id,
                    "phone": phone,
                    "error_code": result.error_code,
                },
            )
            status = (
                SendOtpStatus.FLOOD_WAIT
                if result.error_code == "FLOOD_WAIT"
                else SendOtpStatus.TELETHON_FAILED
            )
            return SendOtpOutcome(
                status=status,
                phone=phone,
                retry_after_seconds=result.flood_wait_seconds,
                error_message=result.error_message,
            )

        # 6. Simpan state ke Redis dengan TTL = OTP expiry
        record = OtpRecord(
            user_id=user_id,
            phone=phone,
            phone_code_hash=result.phone_code_hash,
            expires_at=time.time() + settings.otp_expiry_seconds,
            attempts_used=0,
            ts_sent=time.time(),
        )
        # TTL Redis sedikit lebih panjang dari OTP expiry agar kita bisa
        # deteksi "expired" dengan jelas (Redis belum auto-delete saat OTP expire)
        await storage.save_otp(record, ttl_seconds=settings.otp_expiry_seconds + 60)

        # 7. Set cooldown kirim ulang
        await storage.set_cooldown(user_id, settings.otp_resend_cooldown)

        # 8. Reset input keypad
        await storage.clear_input(user_id)

        return SendOtpOutcome(
            status=SendOtpStatus.SUCCESS,
            phone=phone,
            expiry_seconds=settings.otp_expiry_seconds,
        )

    # ============================================================
    # Resend OTP
    # ============================================================

    async def resend_otp(self, user_id: int) -> SendOtpOutcome:
        """Kirim ulang OTP ke nomor yang sama.

        Flow:
            1. Cek apakah ada OTP aktif (record di Redis)
            2. Cek cooldown kirim ulang
            3. Call Telethon send_code_request lagi (Telegram akan kirim OTP baru)
            4. Update record Redis (phone_code_hash baru, expiry baru)
            5. Reset cooldown
        """
        # 1. Ambil record OTP aktif
        record = await storage.get_otp(user_id)
        if record is None or record.is_expired:
            return SendOtpOutcome(
                status=SendOtpStatus.NO_ACTIVE_OTP if record is None else SendOtpStatus.EXPIRED,
                error_message="Tidak ada OTP aktif untuk dikirim ulang.",
            )

        # 2. Cek cooldown
        remaining_cooldown = await storage.get_cooldown_remaining(user_id)
        if remaining_cooldown > 0:
            return SendOtpOutcome(
                status=SendOtpStatus.RATE_LIMITED,
                phone=record.phone,
                retry_after_seconds=remaining_cooldown,
                error_message=f"Tunggu {remaining_cooldown} detik sebelum kirim ulang.",
            )

        # 3. Kirim OTP baru via Telethon (Telegram akan kirim kode baru)
        manager = get_telethon_manager()
        result = await manager.send_otp(user_id, record.phone)
        if not result.success:
            log.error(
                "resend_otp_failed",
                extra={
                    "event": "resend_otp_fail",
                    "user_id": user_id,
                    "phone": record.phone,
                    "error_code": result.error_code,
                },
            )
            return SendOtpOutcome(
                status=(
                    SendOtpStatus.FLOOD_WAIT
                    if result.error_code == "FLOOD_WAIT"
                    else SendOtpStatus.TELETHON_FAILED
                ),
                phone=record.phone,
                retry_after_seconds=result.flood_wait_seconds,
                error_message=result.error_message,
            )

        # 4. Update record dengan hash & expiry baru, reset attempts
        record.phone_code_hash = result.phone_code_hash
        record.expires_at = time.time() + settings.otp_expiry_seconds
        record.attempts_used = 0
        record.ts_sent = time.time()
        await storage.save_otp(record, ttl_seconds=settings.otp_expiry_seconds + 60)

        # 5. Reset cooldown
        await storage.set_cooldown(user_id, settings.otp_resend_cooldown)

        # 6. Reset input keypad
        await storage.clear_input(user_id)

        return SendOtpOutcome(
            status=SendOtpStatus.SUCCESS,
            phone=record.phone,
            expiry_seconds=settings.otp_expiry_seconds,
        )

    # ============================================================
    # Verify OTP
    # ============================================================

    async def verify_otp(self, user_id: int, code: str) -> VerifyOtpOutcome:
        """Verifikasi kode OTP yang dimasukkan user.

        Flow:
            1. Ambil record OTP aktif
            2. Cek expiry
            3. Cek lockout
            4. Call Telethon sign_in
            5. Jika sukses: cleanup & return SUCCESS
            6. Jika gagal: increment attempts, cek max attempts, lock jika perlu
        """
        # 1. Ambil record
        record = await storage.get_otp(user_id)
        if record is None:
            return VerifyOtpOutcome(
                status=VerifyOtpStatus.NO_ACTIVE_OTP,
                error_message="Tidak ada OTP aktif. Ketik /start untuk memulai.",
            )

        # 2. Cek expiry
        if record.is_expired:
            await self._cleanup_previous_flow(user_id)
            return VerifyOtpOutcome(
                status=VerifyOtpStatus.EXPIRED,
                error_message="Kode OTP sudah kedaluwarsa. Ketik /start untuk minta baru.",
            )

        # 3. Cek lockout
        if await storage.is_locked(user_id):
            return VerifyOtpOutcome(
                status=VerifyOtpStatus.LOCKED,
                error_message="Akun di-lock karena terlalu banyak percobaan salah.",
            )

        # 4. Cek apakah sudah melebihi max attempts
        if record.attempts_used >= settings.otp_max_attempts:
            await storage.lock_user(user_id, lock_seconds=1800)
            await self._cleanup_previous_flow(user_id)
            return VerifyOtpOutcome(
                status=VerifyOtpStatus.MAX_ATTEMPTS_REACHED,
                error_message=f"Maksimal {settings.otp_max_attempts} percobaan tercapai.",
            )

        # 5. Verifikasi via Telethon
        manager = get_telethon_manager()
        result = await manager.verify_otp(
            user_id=user_id,
            phone=record.phone,
            code=code,
            phone_code_hash=record.phone_code_hash,
        )

        if result.success:
            # 6. Sukses - cleanup
            phone = record.phone
            await self._cleanup_after_success(user_id)
            return VerifyOtpOutcome(
                status=VerifyOtpStatus.SUCCESS,
                phone=phone,
                needs_2fa=result.needs_2fa,
            )

        # 7. Gagal - increment attempts
        new_attempts = await storage.increment_attempts(user_id)
        remaining = max(0, settings.otp_max_attempts - new_attempts)

        # Handle FLOOD_WAIT khusus
        if result.error_code == "FLOOD_WAIT":
            return VerifyOtpOutcome(
                status=VerifyOtpStatus.FLOOD_WAIT,
                phone=record.phone,
                attempts_remaining=remaining,
                error_message=result.error_message,
            )

        if remaining <= 0:
            # Lock user setelah max attempts tercapai
            await storage.lock_user(user_id, lock_seconds=1800)
            await self._cleanup_previous_flow(user_id)
            return VerifyOtpOutcome(
                status=VerifyOtpStatus.MAX_ATTEMPTS_REACHED,
                error_message=f"Maksimal {settings.otp_max_attempts} percobaan tercapai.",
            )

        # Wrong code - masih ada sisa percobaan
        if result.error_code in ("CODE_INVALID", "CODE_UNOCCUPIED"):
            return VerifyOtpOutcome(
                status=VerifyOtpStatus.WRONG_CODE,
                phone=record.phone,
                attempts_remaining=remaining,
                error_message="Kode OTP salah.",
            )
        elif result.error_code == "CODE_EXPIRED":
            await self._cleanup_previous_flow(user_id)
            return VerifyOtpOutcome(
                status=VerifyOtpStatus.EXPIRED,
                error_message="Kode OTP sudah kedaluwarsa.",
            )

        # Telethon error lain
        return VerifyOtpOutcome(
            status=VerifyOtpStatus.TELETHON_FAILED,
            phone=record.phone,
            attempts_remaining=remaining,
            error_message=result.error_message,
        )

    # ============================================================
    # Cancel & Cleanup helpers
    # ============================================================

    async def cancel_flow(self, user_id: int) -> None:
        """Batalkan verifikasi yang sedang berjalan."""
        await self._cleanup_previous_flow(user_id)
        log.info("otp_cancelled", extra={"event": "otp_cancel", "user_id": user_id})

    async def _cleanup_previous_flow(self, user_id: int) -> None:
        """Cleanup state Redis + Telethon session untuk flow sebelumnya."""
        # Logout Telethon (hapus session file)
        try:
            manager = get_telethon_manager()
            await manager.logout_and_cleanup(user_id)
        except Exception as e:
            log.warning("cleanup_telethon_failed: %s", e)

        # Purge Redis state
        await storage.purge_user(user_id)

    async def _cleanup_after_success(self, user_id: int) -> None:
        """Cleanup khusus setelah verifikasi sukses (lebih thorough)."""
        await self._cleanup_previous_flow(user_id)
        log.info(
            "otp_success_cleaned",
            extra={"event": "otp_success_clean", "user_id": user_id},
        )

    # ============================================================
    # State queries (untuk handlers)
    # ============================================================

    async def has_active_otp(self, user_id: int) -> bool:
        """Cek apakah user punya OTP aktif yang belum expire."""
        record = await storage.get_otp(user_id)
        return record is not None and not record.is_expired

    async def get_cooldown_remaining(self, user_id: int) -> int:
        """Sisa detik cooldown kirim ulang."""
        return await storage.get_cooldown_remaining(user_id)
    
    async def fetch_incoming_otp(self, user_id: int) -> Optional[str]:
        """Menunggu dan mengambil kode OTP masuk otomatis via Telethon."""
        manager = get_telethon_manager()
        return await manager.wait_for_otp_code(user_id, timeout=60)


# ============================================================
# Lazy Initialization (Proxy Pattern) untuk Mencegah Crash Saat Import
# ============================================================
_otp_service_instance: Optional[OtpService] = None

def get_otp_service() -> OtpService:
    global _otp_service_instance
    if _otp_service_instance is None:
        _otp_service_instance = OtpService()
    return _otp_service_instance

class _OtpServiceProxy:
    def __getattr__(self, name):
        return getattr(get_otp_service(), name)

# `otp_service` sekarang aman di-import kapan saja tanpa langsung crash
otp_service = _OtpServiceProxy()