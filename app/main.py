"""Entry point aplikasi Telegram OTP Bot untuk Railway."""
from __future__ import annotations

import asyncio
import sys

from telegram.ext import Application

from .bot_handlers import register_handlers
from .config import settings
from .logger import get_logger, setup_logging
from .storage import storage
from .telethon_manager import init_telethon_manager, get_telethon_manager

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
    application = Application.builder().token(settings.bot_token).build()
    register_handlers(application)
    application.post_init = post_init
    application.post_stop = post_stop
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

    stop_signal = asyncio.Event()
    try:
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
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())