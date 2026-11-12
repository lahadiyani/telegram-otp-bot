"""Entry point aplikasi Telegram OTP Bot untuk Railway.

Catatan kompatibilitas:
- Python 3.14+ menghapus auto-create event loop di asyncio.get_event_loop().
  Karena itu kita TIDAK memakai application.run_webhook() (helper sinkron PTB
  yang masih memanggil asyncio.get_event_loop() internal), melainkan menjalankan
  lifecycle Application secara manual di dalam asyncio.run().

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


async def post_init(application: Application) -> None:
    log.info("post_init_starting", extra={"event": "post_init_start"})
    storage.configure(settings.redis_url)
    await storage.connect()
    init_telethon_manager()
    log.info("post_init_done", extra={"event": "post_init_done"})


async def post_stop(application: Application) -> None:
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


def build_application() -> Application:
    # post_init & post_stop di-hook via builder (cara resmi PTB v20+),
    # bukan assignment attribute langsung.
    application = (
        Application.builder()
        .token(settings.bot_token)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )
    register_handlers(application)
    return application


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
    # jadi kita replace dengan set Event agar finally block dijalankan.
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
    try:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
    except Exception as e:
        log.warning("shutdown_error: %s", e)
    log.info("app_exited", extra={"event": "app_exit"})

    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())