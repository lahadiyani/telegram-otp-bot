"""Konfigurasi aplikasi yang dibaca dari environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

# Muat .env jika ada (lokal).
load_dotenv()


def _get_int(key: str, default: int) -> int:
    """Baca env var sebagai int dengan fallback default."""
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Konfigurasi global aplikasi (immutable)."""

    # Telegram Bot
    bot_token: str
    api_id: int
    api_hash: str

    # Redis
    redis_url: str

    # Webhook
    webhook_url: str
    port: int
    webhook_path: str

    # OTP
    otp_expiry_seconds: int
    otp_resend_cooldown: int
    otp_max_attempts: int

    # Rate Limiting
    rate_limit_max_requests: int
    rate_limit_window: int

    @classmethod
    def from_env(cls) -> "Settings":
        """Baca semua konfigurasi dari environment variables."""
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        api_id_raw = os.getenv("API_ID", "").strip()
        api_hash = os.getenv("API_HASH", "").strip()

        if not bot_token:
            raise RuntimeError(
                "BOT_TOKEN belum diset. Dapatkan dari @BotFather di Telegram."
            )
        if not api_id_raw or not api_hash:
            raise RuntimeError(
                "API_ID dan API_HASH belum diset. Dapatkan dari https://my.telegram.org"
            )

        try:
            api_id = int(api_id_raw)
        except ValueError:
            raise RuntimeError(
                f"API_ID harus berupa angka, diterima: {api_id_raw!r}"
            )

        # Redis URL: Railway menyediakan REDIS_URL otomatis.
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0").strip()

        # URL publik untuk webhook
        webhook_url = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")

        # PORT: Railway menyediakan secara otomatis
        port = _get_int("PORT", 8443)
        webhook_path = os.getenv("WEBHOOK_PATH", "/webhook").strip()
        # Pastikan path diawali dengan /
        if not webhook_path.startswith("/"):
            webhook_path = "/" + webhook_path

        return cls(
            bot_token=bot_token,
            api_id=api_id,
            api_hash=api_hash,
            redis_url=redis_url,
            webhook_url=webhook_url,
            port=port,
            webhook_path=webhook_path,
            otp_expiry_seconds=_get_int("OTP_EXPIRY_SECONDS", 180),
            otp_resend_cooldown=_get_int("OTP_RESEND_COOLDOWN", 180),
            otp_max_attempts=_get_int("OTP_MAX_ATTEMPTS", 3),
            rate_limit_max_requests=_get_int("RATE_LIMIT_MAX_REQUESTS", 3),
            rate_limit_window=_get_int("RATE_LIMIT_WINDOW", 600),
        )

    @property
    def webhook_full_url(self) -> str:
        """URL lengkap endpoint webhook yang akan didaftarkan ke Telegram."""
        if not self.webhook_url:
            return ""
        return f"{self.webhook_url}{self.webhook_path}"

    def validate_for_webhook(self) -> None:
        """Memastikan konfigurasi cukup untuk menjalankan webhook mode."""
        if not self.webhook_url:
            raise RuntimeError(
                "WEBHOOK_URL belum diset. Set ke URL publik aplikasi Anda."
            )


# --- PERBAIKAN UTAMA (Lazy Proxy) ---
# Menggunakan objek proxy/fungsi agar tidak langsung crash saat modul di-import,
# melainkan dibaca saat pertama kali benar-benar diakses oleh aplikasi.
class _SettingsProxy:
    _instance: Optional[Settings] = None

    def __getattr__(self, name):
        if self._instance is None:
            self._instance = Settings.from_env()
        return getattr(self._instance, name)

# `settings` sekarang aman di-import kapan saja tanpa langsung crash
settings = _SettingsProxy()