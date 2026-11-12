"""Bot handlers untuk Telegram OTP Bot.

Semua handler adalah fungsi async dengan signature:
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None

Komunikasi dengan OTP service dilakukan via `otp_service` singleton.
"""
from __future__ import annotations

from telegram import Update, ReplyKeyboardRemove
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from . import constants as C
from .config import settings
from .keyboards import (
    cancel_keyboard,
    otp_input_keyboard,
    otp_input_keyboard_with_disabled_resend,
)
from .logger import get_logger
from .otp_service import OtpService, SendOtpStatus, VerifyOtpStatus, otp_service
from .storage import storage

log = get_logger(__name__)

# ConversationHandler states (tidak dipakai, kita pakai state-based di Redis)
# Didefinisikan hanya untuk konvensi
STATE_AWAITING_PHONE = 1
STATE_AWAITING_OTP = 2


# ============================================================
# Helper functions
# ============================================================


def _mask_otp_input(digits: str, total: int = 5) -> str:
    """Render string input sebagai placeholder monospace.

    Contoh: "12" -> "12 _ _ _ _" (untuk total 5 digit Telegram code)
    """
    filled = list(digits)
    while len(filled) < total:
        filled.append("•")
    # Pisah dengan spasi agar mudah dibaca
    return "  ".join(filled)


async def _send_otp_input_message(update: Update, current_digits: str = "") -> None:
    """(Re)kirim pesan input OTP dengan keypad & display digit saat ini."""
    display = _mask_otp_input(current_digits)
    text = C.OTP_INPUT_MSG.format(display=display)
    # Hapus pesan keypad sebelumnya (jika ada) lalu kirim baru
    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=otp_input_keyboard(),
    )


async def _update_otp_input_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, new_digits: str
) -> None:
    """Edit pesan keypad yang ada untuk update tampilan digit.

    Dipakai ketika user menekan tombol digit - kita edit pesan yang sama
    agar tidak menumpuk pesan baru.
    """
    display = _mask_otp_input(new_digits)
    text = C.OTP_INPUT_MSG.format(display=display)
    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=update.callback_query.message.message_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=otp_input_keyboard(),
        )
    except Exception as e:
        # Jika edit gagal (misal pesan sama), kirim baru
        log.warning("edit_message_failed: %s", e)
        await update.callback_query.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=otp_input_keyboard(),
        )


# ============================================================
# Command Handlers
# ============================================================


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler /start - kirim pesan welcome + instruksi input nomor HP teks."""
    user = update.effective_user
    log.info(
        "start_command",
        extra={"event": "start", "user_id": user.id if user else None},
    )

    # Jika user punya OTP aktif, batalkan dulu
    if user and await otp_service.has_active_otp(user.id):
        await otp_service.cancel_flow(user.id)

    await update.effective_message.reply_text(
        C.WELCOME_MSG,
        parse_mode=ParseMode.MARKDOWN,
    )
    await update.effective_message.reply_text(
        "📱 Silakan ketik dan kirim nomor HP tujuan secara langsung di chat ini (contoh: `+628123456789`).\n\n"
        "Bisa menggunakan nomor Anda sendiri maupun nomor lain.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler /help."""
    await update.effective_message.reply_text(
        C.HELP_MSG,
        parse_mode=ParseMode.MARKDOWN,
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler /cancel - batalkan verifikasi yang berjalan."""
    user = update.effective_user
    if user:
        await otp_service.cancel_flow(user.id)
    await update.effective_message.reply_text(
        C.CANCEL_MSG,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )


# ============================================================
# Text Phone Handler (menerima input nomor HP secara bebas via teks)
# ============================================================


async def text_phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler untuk menerima input teks nomor HP secara bebas (nomor sendiri/lain)."""
    if not update.message or not update.message.text:
        return

    user = update.effective_user
    raw_text = update.message.text.strip()

    # Abaikan jika diawali perintah command (/)
    if raw_text.startswith("/"):
        return

    # Validasi ringan: pastikan format menyerupai nomor (diawali + atau angka)
    if not raw_text.startswith("+") and not raw_text.isdigit():
        return

    log.info(
        "free_text_phone_received",
        extra={
            "event": "free_text_phone_received",
            "user_id": user.id if user else None,
            "target_phone": raw_text,
        },
    )

    # 1. Kirim OTP via service ke nomor yang diketik user
    outcome = await otp_service.send_otp(user_id=user.id, raw_phone=raw_text)

    # 2. Handle outcome
    if outcome.status == SendOtpStatus.SUCCESS:
        expiry_menit = max(1, outcome.expiry_seconds // 60)
        await update.message.reply_text(
            f"📱 Nomor tujuan: `{raw_text}`\n\n" + C.OTP_SENT_MSG.format(expiry_menit=expiry_menit),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
        )
        # Kirim keypad input OTP
        await _send_otp_input_message(update)

    elif outcome.status == SendOtpStatus.PHONE_INVALID:
        await update.message.reply_text(
            C.PHONE_INVALID_MSG,
            parse_mode=ParseMode.MARKDOWN,
        )

    elif outcome.status == SendOtpStatus.RATE_LIMITED:
        await update.message.reply_text(
            C.RATE_LIMIT_MSG.format(
                used=outcome.error_message,
                seconds=outcome.retry_after_seconds,
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
        )

    elif outcome.status == SendOtpStatus.LOCKED:
        await update.message.reply_text(
            C.OTP_LOCKED_MSG.format(max=settings.otp_max_attempts),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
        )

    elif outcome.status == SendOtpStatus.FLOOD_WAIT:
        await update.message.reply_text(
            f"⏳ Telegram meminta Anda menunggu *{outcome.retry_after_seconds} detik* "
            f"sebelum request OTP baru.\n\nSilakan coba lagi nanti.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
        )

    else:  # TELETHON_FAILED or unknown
        log.error(
            "send_otp_error_to_user",
            extra={
                "event": "send_otp_error",
                "user_id": user.id if user else None,
                "phone": outcome.phone,
                "error": outcome.error_message,
            },
        )
        await update.message.reply_text(
            C.TELETHON_ERROR_MSG,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
        )


# ============================================================
# Callback Query Handlers (numeric keypad, verify, resend, cancel)
# ============================================================


async def digit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler ketika user menekan tombol digit 0-9."""
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()  # Acknowledge tap (hilangkan loading di tombol)

    user = update.effective_user
    if not user:
        return

    # Pastikan user punya OTP aktif
    if not await otp_service.has_active_otp(user.id):
        await query.message.reply_text(
            "⚠️ Tidak ada OTP aktif. Ketik /start untuk memulai.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    # Ambil digit dari callback_data (format: otp:digit:5)
    parts = query.data.split(":")
    if len(parts) != 3:
        return
    digit = parts[2]
    if not digit.isdigit():
        return

    # Append digit ke buffer di Redis
    new_digits = await storage.append_digit(
        user_id=user.id, digit=digit, max_digits=C.MAX_INPUT_DIGITS
    )
    await _update_otp_input_message(update, context, new_digits)


async def clear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler ketika user menekan tombol Hapus (clear input)."""
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    if not user:
        return

    await storage.clear_input(user.id)
    await _update_otp_input_message(update, context, "")


async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler ketika user menekan tombol Verifikasi."""
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    if not user:
        return

    # Ambil digit yang sudah diketik
    digits = await storage.get_input(user.id)
    if not digits or len(digits) < C.MAX_INPUT_DIGITS:
        await query.message.reply_text(
            f"⚠️ Kode OTP harus {C.MAX_INPUT_DIGITS} digit.\n"
            f"Anda baru memasukkan {len(digits) if digits else 0} digit.",
        )
        return

    # Verifikasi via service
    outcome = await otp_service.verify_otp(user_id=user.id, code=digits)

    if outcome.status == VerifyOtpStatus.SUCCESS:
        await query.message.reply_text(
            C.OTP_SUCCESS_MSG.format(phone=outcome.phone),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
        )

    elif outcome.status == VerifyOtpStatus.WRONG_CODE:
        # Reset input buffer agar user bisa input ulang
        await storage.clear_input(user.id)
        await query.message.reply_text(
            C.OTP_WRONG_MSG.format(remaining=outcome.attempts_remaining),
            parse_mode=ParseMode.MARKDOWN,
        )
        # Tampilkan keypad lagi dengan input kosong
        await _update_otp_input_message(update, context, "")

    elif outcome.status == VerifyOtpStatus.EXPIRED:
        await query.message.reply_text(
            C.OTP_EXPIRED_MSG,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
        )

    elif outcome.status == VerifyOtpStatus.MAX_ATTEMPTS_REACHED:
        await query.message.reply_text(
            C.OTP_LOCKED_MSG.format(max=settings.otp_max_attempts),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
        )

    elif outcome.status == VerifyOtpStatus.LOCKED:
        await query.message.reply_text(
            C.OTP_LOCKED_MSG.format(max=settings.otp_max_attempts),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
        )

    elif outcome.status == VerifyOtpStatus.FLOOD_WAIT:
        await query.message.reply_text(
            f"⏳ {outcome.error_message}",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif outcome.status == VerifyOtpStatus.NO_ACTIVE_OTP:
        await query.message.reply_text(
            "⚠️ Tidak ada OTP aktif. Ketik /start untuk memulai.",
            reply_markup=ReplyKeyboardRemove(),
        )

    else:  # TELETHON_FAILED
        await query.message.reply_text(
            C.TELETHON_ERROR_MSG,
            parse_mode=ParseMode.MARKDOWN,
        )


async def resend_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler ketika user menekan tombol Kirim Ulang OTP."""
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    if not user:
        return

    outcome = await otp_service.resend_otp(user_id=user.id)

    if outcome.status == SendOtpStatus.SUCCESS:
        expiry_menit = max(1, outcome.expiry_seconds // 60)
        await query.message.reply_text(
            C.OTP_RESEND_SENT_MSG.format(expiry_menit=expiry_menit),
            parse_mode=ParseMode.MARKDOWN,
        )
        # Reset input keypad dan tampilkan ulang
        await storage.clear_input(user.id)
        await _send_otp_input_message(update)

    elif outcome.status == SendOtpStatus.RATE_LIMITED:
        # Cooldown masih aktif
        await query.message.reply_text(
            C.OTP_RESEND_TOO_SOON_MSG.format(seconds=outcome.retry_after_seconds),
            parse_mode=ParseMode.MARKDOWN,
        )

    elif outcome.status == SendOtpStatus.NO_ACTIVE_OTP:
        await query.message.reply_text(
            "⚠️ Tidak ada OTP aktif untuk dikirim ulang. Ketik /start.",
            reply_markup=ReplyKeyboardRemove(),
        )

    elif outcome.status == SendOtpStatus.EXPIRED:
        await query.message.reply_text(
            C.OTP_EXPIRED_MSG,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
        )

    elif outcome.status == SendOtpStatus.FLOOD_WAIT:
        await query.message.reply_text(
            f"⏳ Telegram meminta Anda menunggu *{outcome.retry_after_seconds} detik*.",
            parse_mode=ParseMode.MARKDOWN,
        )

    else:  # TELETHON_FAILED
        await query.message.reply_text(
            C.OTP_RESEND_FAILED_MSG,
            parse_mode=ParseMode.MARKDOWN,
        )


async def resend_wait_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler ketika user menekan tombol Kirim Ulang yang sedang cooldown.

    Tombol ini menampilkan countdown - kita jawab dengan popup singkat.
    """
    query = update.callback_query
    user = update.effective_user
    if not user:
        return
    seconds_left = await otp_service.get_cooldown_remaining(user.id)
    await query.answer(
        text=f"Tunggu {seconds_left} detik lagi sebelum kirim ulang OTP.",
        show_alert=False,
    )


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler ketika user menekan tombol Batal (inline button)."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if user:
        await otp_service.cancel_flow(user.id)
    await query.message.reply_text(
        C.CANCEL_MSG,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )


# ============================================================
# Error Handler
# ============================================================


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler global untuk error yang tidak tertangani."""
    log.error(
        "unhandled_error",
        extra={"event": "unhandled_error"},
    )
    log.exception("Unhandled exception: %s", context.error)

    # Beritahu user (best-effort, jangan crash jika gagal)
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(C.ERROR_MSG)
        except Exception:
            pass


# ============================================================
# Handler Registration Helper
# ============================================================


def register_handlers(application) -> None:
    """Daftarkan semua handler ke Application python-telegram-bot.

    Dipanggil dari main.py setelah Application dibuat.
    """
    # Command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel_command))

    # Text phone handler (menangkap input teks nomor HP bebas, menggantikan tombol kontak)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_phone_handler))

    # Callback query handlers (urutan PENTING - paling spesifik dulu)
    # Digit: otp:digit:N
    application.add_handler(CallbackQueryHandler(digit_callback, pattern=r"^otp:digit:\d$"))
    # Verifikasi
    application.add_handler(CallbackQueryHandler(verify_callback, pattern=r"^otp:verify$"))
    # Clear
    application.add_handler(CallbackQueryHandler(clear_callback, pattern=r"^otp:clear$"))
    # Resend aktif
    application.add_handler(CallbackQueryHandler(resend_callback, pattern=r"^otp:resend$"))
    # Resend cooldown (popup saja)
    application.add_handler(
        CallbackQueryHandler(resend_wait_callback, pattern=r"^otp:resend_wait$")
    )
    # Cancel
    application.add_handler(CallbackQueryHandler(cancel_callback, pattern=r"^otp:cancel$"))

    # Error handler
    application.add_error_handler(error_handler)