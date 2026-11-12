"""Entry point aplikasi Telegram OTP Bot untuk Heroku.

Menjalankan webhook mode dengan bind ke PORT yang disediakan Heroku.
Telegram akan mengirim update ke {WEBHOOK_URL}{WEBHOOK_PATH}.

Pola: menggunakan Application.run_webhook() (synchronous wrapper).
post_init dipakai untuk inisialisasi resource async (Redis & Telethon).
post_stop dipakai untuk cleanup.

Urutan lifecycle:
    1. Build Application
    2. Initialize Application (async)
    3. post_init: connect Redis + init Telethon manager
    4. set_webhook() otomatis (via run_webhook params)
    5. Start webhook HTTP server (bind 0.0.0.0:PORT)
    6. Block menerima update sampai SIGTERM/SIGINT
    7. post_stop: close Telethon + close Redis
    8. Shutdown
"""
from __future__ import annotations

import sys

from telegram.ext import Application

from .bot_handlers import register_handlers
from .config import settings
from .logger import get_logger, setup_logging
from .storage import storage
from .telethon_manager import init_telethon_manager, get_telethon_manager

log = get_logger(__name__)


# ============================================================
# Lifecycle hooks (async - dipanggil python-telegram-bot)
# ============================================================


async def post_init(application: Application) -> None:
    """Dipanggil setelah Application.initialize() tapi sebelum menerima update.

    Di sini kita koneksi Redis & init Telethon manager.
    """
    log.info("post_init_starting", extra={"event": "post_init_start"})

    # 1. Redis - WAJIB connect sebelum bisa menyimpan OTP state
    storage.configure(settings.redis_url)
    await storage.connect()

    # 2. Telethon manager (clients akan di-create on-demand per user)
    init_telethon_manager()

    log.info("post_init_done", extra={"event": "post_init_done"})


async def post_stop(application: Application) -> None:
    """Dipanggil setelah Application.stop() selesai.

    Di sini kita tutup semua koneksi (Telethon + Redis) dengan aman.
    """
    log.info("post_stop_starting", extra={"event": "post_stop_start"})

    try:
        manager = get_telethon_manager()
        await manager.close_all()
    except Exception as e:
        log.warning("telethon_close_all_failed: %s", e)

    try:
        await storage.close()
    except Exception as e:
        log.warning("redis_close_failed: %s", e)

    log.info("post_stop_done", extra={"event": "post_stop_done"})


# ============================================================
# Main
# ============================================================


def build_application() -> Application:
    """Bangun Application python-telegram-bot dengan semua handler & hooks."""
    application = Application.builder().token(settings.bot_token).build()

    # Daftarkan handler (command, contact, callback)
    register_handlers(application)

    # Hooks lifecycle
    application.post_init = post_init
    application.post_stop = post_stop

    return application


def main() -> int:
    """Main entry point. Exit code 0 = sukses, 1 = error."""
    setup_logging()
    log.info(
        "app_starting",
        extra={
            "event": "app_start",
            "webhook_url": settings.webhook_url,
            "port": settings.port,
            "webhook_path": settings.webhook_path,
        },
    )

    # Validasi config webhook SEBELUM build application
    settings.validate_for_webhook()

    application = build_application()

    # run_webhook adalah SYNCHRONOUS wrapper yang:
    #   1. initialize() -> calls post_init
    #   2. set_webhook(url=webhook_url) (otomatis)
    #   3. start_webhook server di 0.0.0.0:PORT
    #   4. block forever (sampai SIGTERM/SIGINT - Heroku kirim SIGTERM saat restart)
    #   5. stop() -> calls post_stop
    #   6. shutdown()
    application.run_webhook(
        listen="0.0.0.0",
        port=settings.port,
        url_path=settings.webhook_path,
        webhook_url=settings.webhook_full_url,
        # secret_token opsional - Telegram akan kirim header X-Telegram-Bot-Api-Secret-Token
        # secret_token=settings.webhook_secret,  # Uncomment jika ingin verifikasi extra
        drop_pending_updates=True,  # Buat update lama yang mungkin nyangkut
        allowed_updates=["message", "callback_query", "chat_member"],
    )

    log.info("app_exited", extra={"event": "app_exit"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
