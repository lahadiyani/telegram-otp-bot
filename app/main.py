"""Entry point aplikasi Telegram OTP Bot untuk Railway.

Catatan kompatibilitas:
- Python 3.14+ menghapus auto-create event loop di asyncio.get_event_loop().
  Karena itu kita TIDAK memakai application.run_webhook() (helper sinkron PTB
  yang masih memanggil asyncio.get_event_loop() internal), melainkan menjalankan
  lifecycle Application secara manual di dalam asyncio.run().

- Inisialisasi Redis & Telethon dilakukan LANGSUNG di main_async() (bukan via
  post_init PTB) karena pada PTB v22 dengan lifecycle manual, hook post_init
  kadang tidak ter-trigger, menyebabkan Redis belum di-connect saat handler
  dijalankan.

- SIGTERM (yang dikirim Railway saat stop/redeploy) ditangani via
  loop.add_signal_handler() agar shutdown graceful (Redis & Telethon
  di-cleanup dengan benar sebelum container mati).
"""
from __future__ import annotations

import asyncio
import signal
import sys

from telegram.ext import Application

from .bot_handlers import register_handlers
from .config import settings
from .logger import get_logger, setup_logging
from .storage import storage
from .telethon_manager import get_telethon_manager, init_telethon_manager

log = get_logger(__name__)


def build_application() -> Application:
    """Bangun Application PTB + daftarkan handlers.

    Hook post_init/post_stop TIDAK dipakai - inisialisasi Redis & Telethon
    dilakukan langsung di main_async() agar lebih reliable & terlihat di log.
    """
    application = Application.builder().token(settings.bot_token).build()
    register_handlers(application)
    return application


async def _init_dependencies() -> None:
    """Inisialisasi Redis & Telethon manager sebelum bot menerima request.

    Dipanggil LANGSUNG dari main_async() - bukan dari post_init PTB - agar
    kita yakin dependencies ready sebelum webhook start.
    """
    log.info("init_dependencies_starting", extra={"event": "init_deps_start"})

    # 1. Connect Redis
    storage.configure(settings.redis_url)
    await storage.connect()  # ini akan PING Redis & log "redis_connected"
    log.info("redis_ready", extra={"event": "redis_ready"})

    # 2. Init Telethon manager (singleton)
    init_telethon_manager()
    log.info("telethon_ready", extra={"event": "telethon_ready"})

    log.info("init_dependencies_done", extra={"event": "init_deps_done"})


async def _close_dependencies() -> None:
    """Cleanup Redis & Telethon saat shutdown."""
    log.info("close_dependencies_starting", extra={"event": "close_deps_start"})

    try:
        manager = get_telethon_manager()
        await manager.close_all()
        log.info("telethon_closed", extra={"event": "telethon_closed"})
    except Exception as e:
        log.warning("telethon_close_all_failed: %s", e)

    try:
        await storage.close()
        log.info("redis_closed_ok", extra={"event": "redis_closed"})
    except Exception as e:
        log.warning("redis_close_failed: %s", e)

    log.info("close_dependencies_done", extra={"event": "close_deps_done"})


async def main_async() -> int:
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

    settings.validate_for_webhook()

    # Inisialisasi dependencies (Redis + Telethon) SEBELUM Application start.
    # Ini memastikan Redis sudah terhubung saat handler pertama dijalankan.
    await _init_dependencies()

    application = build_application()

    # Lifecycle manual (bukan application.run_webhook) - aman untuk Python 3.15.
    await application.initialize()
    await application.start()

    await application.updater.start_webhook(
        listen="0.0.0.0",
        port=settings.port,
        url_path=settings.webhook_path.lstrip("/"),
        webhook_url=settings.webhook_full_url,
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query", "chat_member"],
    )

    log.info("webhook_started", extra={"event": "webhook_start"})

    # Tunggu sinyal shutdown (SIGTERM dari Railway, atau SIGINT/Ctrl+C).
    # Penting: default SIGTERM handler Python langsung terminate tanpa cleanup,
    # jadi kita replace dengan set Event agar cleanup dijalankan.
    stop_signal = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_signal.set)
        except (NotImplementedError, RuntimeError):
            # Fallback untuk platform yang tidak support add_signal_handler
            # (mis. Windows). Railway/Linux pasti support.
            signal.signal(sig, lambda *_: stop_signal.set())

    await stop_signal.wait()

    log.info("app_stopping", extra={"event": "app_stop"})

    # Cleanup PTB application
    try:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
    except Exception as e:
        log.warning("ptb_shutdown_error: %s", e)

    # Cleanup Redis & Telethon
    await _close_dependencies()

    log.info("app_exited", extra={"event": "app_exit"})
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())