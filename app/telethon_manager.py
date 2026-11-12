"""Manager untuk TelegramClient Telethon yang dipakai untuk OTP resmi.

Alur:
  1. create_client(user_id) -> TelegramClient baru dengan session file per user
  2. send_otp(phone) -> trigger send_code_request, return phone_code_hash
  3. verify_otp(code, phone, phone_code_hash) -> sign_in untuk verifikasi
  4. logout_and_cleanup() -> log_out + hapus session file
"""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Optional

from telethon import TelegramClient, events
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

SESSION_DIR = "sessions"


def _ensure_session_dir() -> None:
    os.makedirs(SESSION_DIR, exist_ok=True)


def _session_path(user_id: int) -> str:
    _ensure_session_dir()
    return os.path.join(SESSION_DIR, f"verify_{user_id}")


@dataclass
class SendOtpResult:
    success: bool
    phone_code_hash: str = ""
    error_code: str = ""
    error_message: str = ""
    flood_wait_seconds: int = 0


@dataclass
class VerifyOtpResult:
    success: bool
    error_code: str = ""
    error_message: str = ""
    needs_2fa: bool = False


class TelethonManager:
    """Manager lifecycle TelegramClient Telethon per user."""

    def __init__(self, api_id: int, api_hash: str):
        self.api_id = api_id
        self.api_hash = api_hash
        self._clients: dict[int, TelegramClient] = {}
        self._lock = asyncio.Lock()

    async def _get_or_create_client(self, user_id: int) -> TelegramClient:
        async with self._lock:
            client = self._clients.get(user_id)
            if client is not None and client.is_connected():
                return client
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

    async def wait_for_otp_code(self, user_id: int, timeout: int = 60) -> Optional[str]:
        """Menunggu pesan masuk dari Telegram secara otomatis dan mengekstrak kode OTP-nya."""
        client = await self._get_or_create_client(user_id)
        event_future = asyncio.get_running_loop().create_future()

        @client.on(events.NewMessage(incoming=True))
        async def handler(event):
            msg_text = event.raw_text or ""
            # Pola regex untuk mencari 5 atau 6 digit kode login Telegram
            match = re.search(r'\b(\d{5,6})\b', msg_text)
            if match and not event_future.done():
                code = match.group(1)
                event_future.set_result(code)

        try:
            code = await asyncio.wait_for(event_future, timeout=timeout)
            return code
        except asyncio.TimeoutError:
            return None
        finally:
            client.remove_event_handler(handler)

    async def verify_otp(
        self, user_id: int, phone: str, code: str, phone_code_hash: str
    ) -> VerifyOtpResult:
        try:
            client = await self._get_or_create_client(user_id)
            await client.sign_in(
                phone=phone,
                code=code,
                phone_code_hash=phone_code_hash,
            )
            return VerifyOtpResult(success=True)
        except PhoneCodeInvalidError:
            return VerifyOtpResult(success=False, error_code="CODE_INVALID", error_message="Kode OTP salah.")
        except PhoneCodeExpiredError:
            return VerifyOtpResult(success=False, error_code="CODE_EXPIRED", error_message="Kode OTP kedaluwarsa.")
        except PhoneNumberUnoccupiedError:
            return VerifyOtpResult(success=False, error_code="CODE_UNOCCUPIED", error_message="Sesi tidak cocok.")
        except SessionPasswordNeededError:
            return VerifyOtpResult(success=True, needs_2fa=True)
        except FloodWaitError as e:
            return VerifyOtpResult(success=False, error_code="FLOOD_WAIT", error_message=f"Tunggu {e.seconds} detik.")
        except Exception as e:
            return VerifyOtpResult(success=False, error_code="UNKNOWN", error_message=str(e))

    async def logout_and_cleanup(self, user_id: int) -> None:
        async with self._lock:
            client = self._clients.pop(user_id, None)

        if client is not None:
            try:
                if client.is_connected():
                    await client.log_out()
                else:
                    await client.disconnect()
                    self._delete_session_file(user_id)
            except Exception:
                self._delete_session_file(user_id)
        else:
            self._delete_session_file(user_id)

    def _delete_session_file(self, user_id: int) -> None:
        session_base = _session_path(user_id)
        session_file = f"{session_base}.session"
        if os.path.exists(session_file):
            try:
                os.remove(session_file)
            except OSError:
                pass

    async def close_all(self) -> None:
        async with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            try:
                await client.disconnect()
            except Exception:
                pass


telethon_manager: Optional[TelethonManager] = None


def init_telethon_manager() -> TelethonManager:
    global telethon_manager
    if telethon_manager is None:
        telethon_manager = TelethonManager(
            api_id=settings.api_id, api_hash=settings.api_hash
        )
    return telethon_manager


def get_telethon_manager() -> TelethonManager:
    if telethon_manager is None:
        raise RuntimeError("TelethonManager belum diinisialisasi.")
    return telethon_manager