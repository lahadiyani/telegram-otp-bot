"""Bot handlers untuk Telegram OTP Bot (Auto-Fetch OTP Version)."""
from __future__ import annotations

from telegram import Update, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import constants as C
from .config import settings
from .logger import get_logger
from .otp_service import SendOtpStatus, otp_service
from .storage import storage

log = get_logger(__name__)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    log.info("start_command", extra={"event": "start", "user_id": user.id if user else None})

    if user and await otp_service.has_active_otp(user.id):
        await otp_service.cancel_flow(user.id)

    await update.effective_message.reply_text(
        C.WELCOME_MSG,
        parse_mode=ParseMode.MARKDOWN,
    )
    await update.effective_message.reply_text(
        "📱 Silakan ketik dan kirim nomor HP tujuan secara langsung (contoh: `+628123456789`).\n\n"
        "Kode OTP akan otomatis tertampil di bot setelah dikirim oleh Telegram.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(C.HELP_MSG, parse_mode=ParseMode.MARKDOWN)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        await otp_service.cancel_flow(user.id)
    await update.effective_message.reply_text(
        C.CANCEL_MSG,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )


async def text_phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler untuk menangkap input nomor bebas dan otomatis mengambil kode OTP."""
    if not update.message or not update.message.text:
        return

    user = update.effective_user
    raw_text = update.message.text.strip()

    if raw_text.startswith("/"):
        return

    if not raw_text.startswith("+") and not raw_text.isdigit():
        return

    log.info(
        "free_text_phone_received",
        extra={"event": "free_text_phone_received", "user_id": user.id if user else None, "target_phone": raw_text},
    )

    # Kirim status awal
    status_msg = await update.message.reply_text(
        f"📱 Nomor tujuan: `{raw_text}`\n⏳ Mengirim permintaan OTP ke Telegram...",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )

    # 1. Kirim OTP via service
    outcome = await otp_service.send_otp(user_id=user.id, raw_phone=raw_text)

    if outcome.status != SendOtpStatus.SUCCESS:
        await status_msg.edit_text(
            f"❌ Gagal mengirim OTP: {outcome.error_message}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # 2. Update status: Menunggu pesan masuk
    await status_msg.edit_text(
        f"📱 Nomor tujuan: `{raw_text}`\n"
        f"✅ OTP terkirim! Menunggu pesan masuk dari Telegram...",
        parse_mode=ParseMode.MARKDOWN,
    )

    # 3. Tangkap kode OTP secara otomatis dari pesan masuk Telegram
    otp_code = await otp_service.fetch_incoming_otp(user.id)

    if otp_code:
        # Tampilkan kode OTP agar langsung tinggal dicopy oleh client
        await update.message.reply_text(
            f"🎉 **Kode OTP Berhasil Ditangkap!**\n\n"
            f"Nomor: `{raw_text}`\n"
            f"Kode OTP: `{otp_code}`\n\n"
            f"_Ketuk kode di atas untuk menyalin (copy)._",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            "⚠️ Waktu menunggu habis (timeout). Kode OTP tidak tertangkap secara otomatis. Silakan coba lagi.",
            parse_mode=ParseMode.MARKDOWN,
        )


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled exception: %s", context.error)
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(C.ERROR_MSG)
        except Exception:
            pass


def register_handlers(application) -> None:
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel_command))

    # Hanya menggunakan input teks bebas untuk nomor HP
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_phone_handler))

    application.add_error_handler(error_handler)