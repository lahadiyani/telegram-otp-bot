"""Manager untuk TelegramClient Telethon yang dipakai untuk OTP resmi.

Alur:
  1. create_client(user_id) -> TelegramClient baru dengan session file per user
  2. send_otp(phone) -> trigger send_code_request, return phone_code_hash
  3. verify_otp(code, phone, phone_code_hash) -> sign_in untuk verifikasi
  4. logout_and_cleanup() -> log_out + hapus session file

Catatan penting untuk hosting Heroku:
- File session Telethon disimpan di filesystem dyno (ephemeral).
  Artinya, jika dyno restart/sleep, session untuk verifikasi yang sedang
  berjalan akan hilang dan user harus minta OTP baru.
  Ini trade-off yang dapat diterima untuk bot OTP dengan window 3 menit.
- Untuk produksi dengan traffic tinggi, pertimbangkan custom RedisSession
  (Telethon memungkinkan implementasi Session interface sendiri).
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Optional

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    PhoneNumberUnoccupiedError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from .config import settings
from .logger import get_logger

log = get_logger(__name__)

# Direktori penyimpanan session file Telethon
SESSION_DIR = "sessions"


def _ensure_session_dir() -> None:
    """Pastikan direktori sessions/ ada."""
    os.makedirs(SESSION_DIR, exist_ok=True)


def _session_path(user_id: int) -> str:
    """Path file session SQLite untuk user tertentu.

    Telethon akan otomatis menambahkan ekstensi .session.
    """
    _ensure_session_dir()
    return os.path.join(SESSION_DIR, f"verify_{user_id}")


@dataclass
class SendOtpResult:
    """Hasil request pengiriman OTP."""

    success: bool
    phone_code_hash: str = ""
    error_code: str = ""
    error_message: str = ""
    flood_wait_seconds: int = 0


@dataclass
class VerifyOtpResult:
    """Hasil verifikasi OTP."""

    success: bool
    error_code: str = ""
    error_message: str = ""
    needs_2fa: bool = False  # True jika akun user butuh password 2FA


class TelethonManager:
    """Manager lifecycle TelegramClient Telethon per user."""

    def __init__(self, api_id: int, api_hash: str):
        self.api_id = api_id
        self.api_hash = api_hash
        # Cache client yang sedang aktif dalam memori (per user)
        # Kunci: user_id, Nilai: TelegramClient
        self._clients: dict[int, TelegramClient] = {}
        self._lock = asyncio.Lock()

    async def _get_or_create_client(self, user_id: int) -> TelegramClient:
        """Ambil client yang sudah ada di memori, atau buat baru.

        TelegramClient di-cache per user selama flow verifikasi berjalan
        untuk menghindari overhead koneksi berulang.
        """
        async with self._lock:
            client = self._clients.get(user_id)
            if client is not None and client.is_connected():
                return client
            # Buat client baru - session file akan di-load jika sudah ada
            client = TelegramClient(
                session=_session_path(user_id),
                api_id=self.api_id,
                api_hash=self.api_hash,
                connection_retries=3,
                retry_delay=1,
                request_retries=3,
            )
            await client.connect()
            self._clients[user_id] = client
            log.info(
                "telethon_client_created",
                extra={"event": "telethon_create", "user_id": user_id},
            )
            return client

    async def send_otp(self, user_id: int, phone: str) -> SendOtpResult:
        """Trigger pengiriman kode OTP resmi Telegram ke nomor user.

        Args:
            user_id: ID Telegram user (untuk isolasi session)
            phone: Nomor HP format E.164 (contoh: +628123456789)

        Returns:
            SendOtpResult dengan phone_code_hash jika sukses.
        """
        try:
            client = await self._get_or_create_client(user_id)
            sent = await client.send_code_request(phone, force_sms=False)
            log.info(
                "otp_sent_telethon",
                extra={
                    "event": "otp_sent",
                    "user_id": user_id,
                    "phone": phone,
                },
            )
            return SendOtpResult(
                success=True,
                phone_code_hash=sent.phone_code_hash,
            )
        except FloodWaitError as e:
            # Telegram membatasi jumlah request - harus tunggu
            log.warning(
                "telethon_flood_wait",
                extra={
                    "event": "flood_wait",
                    "user_id": user_id,
                    "phone": phone,
                },
            )
            return SendOtpResult(
                success=False,
                error_code="FLOOD_WAIT",
                error_message=f"Telegram meminta tunggu {e.seconds} detik.",
                flood_wait_seconds=e.seconds,
            )
        except PhoneNumberBannedError:
            log.error(
                "telethon_phone_banned",
                extra={"event": "phone_banned", "user_id": user_id, "phone": phone},
            )
            return SendOtpResult(
                success=False,
                error_code="PHONE_BANNED",
                error_message="Nomor ini dilarang oleh Telegram.",
            )
        except PhoneNumberInvalidError:
            return SendOtpResult(
                success=False,
                error_code="PHONE_INVALID",
                error_message="Nomor tidak terdaftar di Telegram.",
            )
        except Exception as e:
            log.exception(
                "telethon_send_otp_error",
                extra={"event": "send_otp_error", "user_id": user_id, "phone": phone},
            )
            return SendOtpResult(
                success=False,
                error_code="UNKNOWN",
                error_message=str(e),
            )

    async def verify_otp(
        self, user_id: int, phone: str, code: str, phone_code_hash: str
    ) -> VerifyOtpResult:
        """Verifikasi kode OTP yang dimasukkan user dengan sign_in.

        Args:
            user_id: ID Telegram user
            phone: Nomor HP yang dipakai saat send_otp
            code: Kode 5-digit dari user
            phone_code_hash: Hash yang dikembalikan saat send_otp

        Returns:
            VerifyOtpResult
        """
        try:
            client = await self._get_or_create_client(user_id)
            await client.sign_in(
                phone=phone,
                code=code,
                phone_code_hash=phone_code_hash,
            )
            log.info(
                "otp_verified_telethon",
                extra={
                    "event": "otp_verified",
                    "user_id": user_id,
                    "phone": phone,
                },
            )
            return VerifyOtpResult(success=True)
        except PhoneCodeInvalidError:
            return VerifyOtpResult(
                success=False,
                error_code="CODE_INVALID",
                error_message="Kode OTP salah.",
            )
        except PhoneCodeExpiredError:
            return VerifyOtpResult(
                success=False,
                error_code="CODE_EXPIRED",
                error_message="Kode OTP sudah kedaluwarsa.",
            )
        except PhoneNumberUnoccupiedError:
            return VerifyOtpResult(
                success=False,
                error_code="CODE_UNOCCUPIED",
                error_message="Kode tidak cocok dengan sesi yang aktif.",
            )
        except SessionPasswordNeededError:
            # Akun user butuh 2FA password - kita tidak handle password
            # Tapi kode OTP-nya BENAR, jadi phone terverifikasi
            log.info(
                "otp_verified_with_2fa",
                extra={
                    "event": "otp_verified_2fa",
                    "user_id": user_id,
                    "phone": phone,
                },
            )
            return VerifyOtpResult(success=True, needs_2fa=True)
        except FloodWaitError as e:
            return VerifyOtpResult(
                success=False,
                error_code="FLOOD_WAIT",
                error_message=f"Telegram meminta tunggu {e.seconds} detik.",
            )
        except Exception as e:
            log.exception(
                "telethon_verify_error",
                extra={"event": "verify_error", "user_id": user_id, "phone": phone},
            )
            return VerifyOtpResult(
                success=False,
                error_code="UNKNOWN",
                error_message=str(e),
            )

    async def logout_and_cleanup(self, user_id: int) -> None:
        """Logout user dari Telethon dan hapus session file.

        WAJIB dipanggil setelah verifikasi sukses agar tidak menumpuk
        session file di dyno.
        """
        async with self._lock:
            client = self._clients.pop(user_id, None)

        if client is not None:
            try:
                if client.is_connected():
                    # Log out menghapus auth_key di server Telegram sekaligus file session
                    await client.log_out()
                else:
                    # Jika tidak terkoneksi, hapus file manual
                    await client.disconnect()
                    self._delete_session_file(user_id)
                log.info(
                    "telethon_logged_out",
                    extra={"event": "telethon_logout", "user_id": user_id},
                )
            except Exception as e:
                log.warning(
                    "telethon_logout_failed",
                    extra={"event": "telethon_logout_fail", "user_id": user_id},
                )
                log.exception("Logout gagal: %s", e)
                # Fallback: hapus file session manual
                self._delete_session_file(user_id)
        else:
            # Client tidak ada di memori, tapi mungkin file session masih ada
            self._delete_session_file(user_id)

    def _delete_session_file(self, user_id: int) -> None:
        """Hapus file session SQLite dari filesystem."""
        session_base = _session_path(user_id)
        # Telethon membuat file .session
        session_file = f"{session_base}.session"
        if os.path.exists(session_file):
            try:
                os.remove(session_file)
            except OSError:
                pass

    async def close_all(self) -> None:
        """Tutup SEMUA client aktif - dipanggil saat shutdown aplikasi."""
        async with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()

        for client in clients:
            try:
                await client.disconnect()
            except Exception:
                pass


# Singleton manager - diinisialisasi di main.py
telethon_manager: Optional[TelethonManager] = None


def init_telethon_manager() -> TelethonManager:
    """Inisialisasi TelethonManager global. Dipanggil dari main.py."""
    global telethon_manager
    if telethon_manager is None:
        telethon_manager = TelethonManager(
            api_id=settings.api_id, api_hash=settings.api_hash
        )
    return telethon_manager


def get_telethon_manager() -> TelethonManager:
    """Ambil instance TelethonManager global."""
    if telethon_manager is None:
        raise RuntimeError("TelethonManager belum diinisialisasi. Panggil init_telethon_manager() dulu.")
    return telethon_manager
