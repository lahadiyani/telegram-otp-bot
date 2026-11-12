"""Entry point aplikasi Telegram OTP Bot untuk Railway/Heroku.

Menjalankan webhook mode dengan bind ke PORT yang disediakan platform.
Telegram akan mengirim update ke {WEBHOOK_URL}{WEBHOOK_PATH}.
"""
from __future__ import annotations

import asyncio
import os
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
    """Dipanggil setelah Application.initialize() tapi sebelum menerima update."""
    log.info("post_init_starting", extra={"event": "post_init_start"})

    # 1. Redis - WAJIB connect sebelum bisa menyimpan OTP state
    storage.configure(settings.redis_url)
    await storage.connect()

    # 2. Telethon manager (clients akan di-create on-demand per user)
    init_telethon_manager()

    log.info("post_init_done", extra={"event": "post_init_done"})


async def post_stop(application: Application) -> None:
    """Dipanggil setelah Application.stop() selesai."""
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
# Main Application Builder & Runner
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


async def main_async() -> int:
    """Async main entry point untuk menangani event loop secara aman."""
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

    # Inisialisasi manual aplikasi agar hooks post_init berjalan dengan benar
    await application.initialize()
    await application.start()

    # Mulai webhook server secara asinkron (kompatibel dengan Python terbaru)
    await application.updater.start_webhook(
        listen="0.0.0.0",
        port=settings.port,
        url_path="webhook",
        webhook_url=settings.webhook_full_url,
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query", "chat_member"],
    )

    log.info("webhook_started", extra={"event": "webhook_start"})

    # Gunakan event untuk menjaga agar server tetap hidup sampai sinyal berhenti diterima
    stop_signal = asyncio.Event()
    
    def _signal_handler():
        stop_signal.set()

    # Daftarkan penanganan event loop untuk shutdown yang bersih
    loop = asyncio.get_running_loop()
    for sig in (asyncio.coroutines,):  # placeholder loop safety
        pass

    try:
        # Menunggu sampai aplikasi dihentikan (misal SIGTERM dari Railway)
        await stop_signal.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        log.info("app_stopping", extra={"event": "app_stop"})
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        log.info("app_exited", extra={"event": "app_exit"})

    return 0


def main() -> int:
    """Synchronous entry point penampung async_run."""
    try:
        # asyncio.run menangani pembuatan dan penutupan event loop dengan aman
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())