# Struktur Proyek: `app`

- 📄 `__init__.py`
- 📄 `bot_handlers.py`
- 📄 `config.py`
- 📄 `constants.py`
- 📄 `keyboards.py`
- 📄 `logger.py`
- 📄 `main.py`
- 📄 `otp_service.py`
- 📄 `rate_limiter.py`
- 📄 `storage.py`
- 📄 `telethon_manager.py`

---

# Isi Kode Sumber

## File: `__init__.py`

```py
"""
Telegram OTP Bot - Verifikasi OTP resmi via Telegram Client API (Telethon).

Dibuat untuk hosting di Heroku (webhook mode + Redis add-on).
"""
__version__ = "1.0.0"

```

## File: `bot_handlers.py`

```py
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
    request_phone_keyboard,
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
    """Handler /start - kirim pesan welcome + tombol request phone."""
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
        C.REQUEST_PHONE_MSG,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=request_phone_keyboard(),
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
# Contact Handler (menerima nomor HP dari KeyboardButton)
# ============================================================


async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler ketika user menekan tombol 'Kirim Nomor HP' (request_contact=True).

    Memastikan:
    - Pesan contact datang dari user yang sama dengan pemilik nomor (anti spoofing)
    - Format nomor valid
    - Trigger OTP resmi via Telethon
    """
    if not update.message or not update.message.contact:
        return

    user = update.effective_user
    contact = update.message.contact

    # 1. Verifikasi nomor HP benar-benar milik user yang menekan tombol
    #    Telegram mengisi contact.user_id dengan ID user pemilik nomor (jika ada di kontak Telegram)
    if contact.user_id is not None and contact.user_id != user.id:
        log.warning(
            "phone_not_owned",
            extra={
                "event": "phone_not_owned",
                "user_id": user.id,
                "contact_user_id": contact.user_id,
            },
        )
        await update.message.reply_text(
            "❌ Nomor HP ini bukan milik akun Anda.\n"
            "Silakan gunakan nomor yang terhubung dengan akun Telegram Anda.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    raw_phone = contact.phone_number or ""
    log.info(
        "contact_received",
        extra={
            "event": "contact_received",
            "user_id": user.id,
            "phone": raw_phone,
        },
    )

    # 2. Kirim OTP via service
    outcome = await otp_service.send_otp(user_id=user.id, raw_phone=raw_phone)

    # 3. Handle outcome
    if outcome.status == SendOtpStatus.SUCCESS:
        expiry_menit = max(1, outcome.expiry_seconds // 60)
        await update.message.reply_text(
            C.OTP_SENT_MSG.format(expiry_menit=expiry_menit),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove(),
        )
        # Kirim keypad input OTP
        await _send_otp_input_message(update)

    elif outcome.status == SendOtpStatus.PHONE_INVALID:
        await update.message.reply_text(
            C.PHONE_INVALID_MSG,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=request_phone_keyboard(),
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
                "user_id": user.id,
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

    # Contact handler (untuk request_contact)
    application.add_handler(MessageHandler(filters.CONTACT, contact_handler))

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

```

## File: `config.py`

```py
"""Konfigurasi aplikasi yang dibaca dari environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

# Muat .env jika ada (lokal). Di Heroku, env vars sudah diset otomatis.
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

        # Redis URL: Heroku menyediakan REDIS_URL otomatis via add-on.
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0").strip()

        # URL publik untuk webhook (Heroku app URL)
        webhook_url = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")

        # PORT: Heroku menyediakan secara otomatis
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
                "WEBHOOK_URL belum diset. Set ke URL publik aplikasi Heroku Anda, "
                "misalnya: https://nama-app.herokuapp.com"
            )


# Singleton settings yang dimuat sekali saat import
settings = Settings.from_env()

```

## File: `constants.py`

```py
"""Konstanta & teks pesan antarmuka bot (Bahasa Indonesia)."""
from __future__ import annotations

# ============================================================
# Pesan Pengguna
# ============================================================

WELCOME_MSG = (
    "👋 *Selamat datang di Bot Verifikasi OTP Telegram*\n\n"
    "Bot ini akan memverifikasi kepemilikan nomor Telegram Anda menggunakan "
    "sistem OTP resmi Telegram.\n\n"
    "📌 *Cara kerja:*\n"
    "1️⃣ Tekan tombol *Kirim Nomor HP* di bawah ini\n"
    "2️⃣ Telegram akan mengirimkan kode OTP ke akun Telegram Anda\n"
    "3️⃣ Masukkan kode 6-digit menggunakan numeric keypad\n"
    "4️⃣ Verifikasi selesai ✅\n\n"
    "🔒 _Kami tidak menyimpan nomor HP Anda setelah verifikasi selesai._"
)

REQUEST_PHONE_MSG = (
    "📱 Silakan tekan tombol di bawah untuk mengirim nomor HP Anda.\n\n"
    "Bot akan meminta Telegram mengirim kode OTP ke akun Anda."
)

PHONE_INVALID_MSG = (
    "❌ Format nomor HP tidak valid.\n"
    "Pastikan nomor dalam format internasional, contoh: *+628123456789*.\n\n"
    "Silakan tekan tombol *Kirim Nomor HP* lagi."
)

OTP_SENT_MSG = (
    "✅ *Kode OTP telah dikirim via Telegram*\n\n"
    "Telegram mengirimkan kode 6-digit ke akun Anda. "
    "Periksa pesan masuk dari akun Telegram resmi *Verification Codes* atau *Telegram*.\n\n"
    "⏱️ Kode berlaku *{expiry_menit} menit*.\n"
    "🔢 Masukkan kode menggunakan tombol keypad di bawah.\n\n"
    "_Jangan bagikan kode ini kepada siapapun._"
)

OTP_INPUT_MSG = "🔢 *Masukkan kode OTP:*\n\n`{display}`\n\n_Pencet_ ✅ *Verifikasi* _untuk memeriksa kode._"

OTP_RESEND_COOLDOWN_MSG = (
    "⏳ Mohon tunggu *{seconds}* detik lagi sebelum meminta kode baru."
)

OTP_RESEND_SENT_MSG = (
    "🔁 Kode OTP baru telah dikirim ke akun Telegram Anda.\n"
    "⏱️ Berlaku *{expiry_menit} menit*."
)

OTP_RESEND_FAILED_MSG = (
    "❌ Gagal mengirim ulang kode OTP. Silakan coba beberapa saat lagi."
)

OTP_SUCCESS_MSG = (
    "🎉 *Verifikasi Berhasil!*\n\n"
    "Nomor HP *{phone}* telah terverifikasi sebagai milik akun Telegram Anda.\n\n"
    "✅ Sesi verifikasi telah dihapus. Anda aman.\n\n"
    "Ketik /start untuk memulai verifikasi baru."
)

OTP_WRONG_MSG = (
    "❌ *Kode OTP salah.*\n\n"
    "Sisa percobaan: *{remaining}*\n"
    "Silakan masukkan kembali kode yang benar."
)

OTP_EXPIRED_MSG = (
    "⏰ *Kode OTP telah kedaluwarsa.*\n\n"
    "Silakan minta kode baru dengan mengetik /start."
)

OTP_LOCKED_MSG = (
    "🔒 *Verifikasi diblokir sementara.*\n\n"
    "Anda telah salah memasukkan kode sebanyak *{max}* kali.\n"
    "Silakan coba lagi nanti atau ketik /start untuk memulai ulang."
)

RATE_LIMIT_MSG = (
    "⚠️ *Batas permintaan tercapai.*\n\n"
    "Anda telah meminta OTP *{used}* kali dalam 10 menit terakhir.\n"
    "Silakan coba lagi dalam *{seconds}* detik."
)

OTP_RESEND_TOO_SOON_MSG = (
    "⏳ Tombol kirim ulang baru bisa dipakai dalam *{seconds}* detik."
)

CANCEL_MSG = (
    "🚫 Verifikasi dibatalkan.\n\n"
    "Ketik /start untuk memulai lagi."
)

HELP_MSG = (
    "ℹ️ *Bantuan*\n\n"
    "/start - Mulai verifikasi OTP\n"
    "/cancel - Batalkan verifikasi yang sedang berjalan\n"
    "/help - Tampilkan pesan ini\n\n"
    "Jika mengalami kendala, pastikan:\n"
    "• Nomor HP terhubung dengan akun Telegram Anda\n"
    "• Anda tidak memblokir pesan dari sistem Telegram\n"
    "• Kuota OTP harian Telegram belum terlampaui"
)

ERROR_MSG = (
    "⚠️ Terjadi kesalahan teknis. Tim kami telah diberi notifikasi.\n"
    "Silakan coba beberapa saat lagi atau ketik /start."
)

TELETHON_ERROR_MSG = (
    "⚠️ Layanan OTP Telegram sedang sibuk. Silakan coba lagi dalam beberapa menit.\n"
    "Jika masalah berlanjut, ketik /start untuk memulai ulang."
)

# ============================================================
# Teks Tombol
# ============================================================

BTN_SEND_PHONE = "📱 Kirim Nomor HP"
BTN_RESEND = "🔁 Kirim Ulang OTP"
BTN_VERIFY = "✅ Verifikasi"
BTN_CLEAR = "🗑️ Hapus"
BTN_CANCEL = "🚫 Batal"
BTN_DIGIT_TEMPLATE = "{digit}"  # 0-9

# ============================================================
# Callback Data Patterns
# ============================================================

CB_RESEND = "otp:resend"
CB_VERIFY = "otp:verify"
CB_CLEAR = "otp:clear"
CB_CANCEL = "otp:cancel"
CB_DIGIT_PREFIX = "otp:digit:"  # contoh callback_data: otp:digit:5

# ============================================================
# TTL & Limit Default
# ============================================================

OTP_LENGTH = 6  # Telegram selalu kirim 5 digit untuk login code, kita pakai 6 max
MAX_INPUT_DIGITS = 5  # Telegram login code adalah 5 digit
SESSION_DIR = "sessions"

```

## File: `keyboards.py`

```py
"""Inline keyboards untuk bot OTP.

Layout numeric keypad (mirip dialer HP):
    [1] [2] [3]
    [4] [5] [6]
    [7] [8] [9]
    [🗑️ Hapus] [0] [✅ Verifikasi]

Tombol Kirim Ulang ditampilkan di bawah keypad (terpisah).
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from . import constants as C


# ============================================================
# Reply Keyboard (untuk request contact phone)
# ============================================================


def request_phone_keyboard() -> ReplyKeyboardMarkup:
    """Keyboard biasa dengan tombol 'Kirim Nomor HP' yang request contact.

    Tombol KeyboardButton(request_contact=True) akan otomatis mengirim
    nomor HP user (yang terdaftar di Telegram) ke bot. User tidak perlu
    mengetik manual.
    """
    keyboard = [[KeyboardButton(text=C.BTN_SEND_PHONE, request_contact=True)]]
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,  # Ukuran compact mengikuti tombol
        one_time_keyboard=True,  # Hilang setelah dipakai
        input_field_placeholder="Tekan tombol di bawah untuk mengirim nomor HP Anda",
    )


# ============================================================
# Inline Keypad (untuk input OTP)
# ============================================================


def _digit_callback(digit: str) -> str:
    """Buat callback_data untuk tombol digit."""
    return f"{C.CB_DIGIT_PREFIX}{digit}"


def otp_input_keyboard() -> InlineKeyboardMarkup:
    """Numeric keypad 3x4 mirip dialer HP untuk input OTP.

    Layout:
        [1] [2] [3]
        [4] [5] [6]
        [7] [8] [9]
        [🗑️ Hapus] [0] [✅ Verifikasi]
    """
    # Baris 1-3: digit 1-9
    rows = []
    for row_start in (1, 4, 7):
        rows.append(
            [
                InlineKeyboardButton(text=str(d), callback_data=_digit_callback(str(d)))
                for d in (row_start, row_start + 1, row_start + 2)
            ]
        )

    # Baris 4: Hapus, 0, Verifikasi
    rows.append(
        [
            InlineKeyboardButton(text=C.BTN_CLEAR, callback_data=C.CB_CLEAR),
            InlineKeyboardButton(text="0", callback_data=_digit_callback("0")),
            InlineKeyboardButton(text=C.BTN_VERIFY, callback_data=C.CB_VERIFY),
        ]
    )

    # Baris 5: Tombol Kirim Ulang + Batal
    rows.append(
        [
            InlineKeyboardButton(text=C.BTN_RESEND, callback_data=C.CB_RESEND),
            InlineKeyboardButton(text=C.BTN_CANCEL, callback_data=C.CB_CANCEL),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def otp_input_keyboard_with_disabled_resend(seconds_left: int) -> InlineKeyboardMarkup:
    """Variant keypad dengan tombol resend disabled (cooldown aktif).

    Telegram tidak mendukung tombol disabled asli, jadi kita tampilkan
    teks countdown dan pakai callback_data yang berbeda (otp:resend_wait)
    untuk membedakan dari tombol resend aktif.

    Args:
        seconds_left: Sisa detik cooldown.
    """
    rows = []
    for row_start in (1, 4, 7):
        rows.append(
            [
                InlineKeyboardButton(text=str(d), callback_data=_digit_callback(str(d)))
                for d in (row_start, row_start + 1, row_start + 2)
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(text=C.BTN_CLEAR, callback_data=C.CB_CLEAR),
            InlineKeyboardButton(text="0", callback_data=_digit_callback("0")),
            InlineKeyboardButton(text=C.BTN_VERIFY, callback_data=C.CB_VERIFY),
        ]
    )

    # Tombol resend disabled-style: tampilkan countdown, callback berbeda
    rows.append(
        [
            InlineKeyboardButton(
                text=f"⏳ Kirim Ulang ({seconds_left}s)",
                callback_data="otp:resend_wait",
            ),
            InlineKeyboardButton(text=C.BTN_CANCEL, callback_data=C.CB_CANCEL),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_keyboard() -> InlineKeyboardMarkup:
    """Keyboard sederhana hanya dengan tombol Batal."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=C.BTN_CANCEL, callback_data=C.CB_CANCEL)]]
    )

```

## File: `logger.py`

```py
"""Logging setup dengan format structured untuk Heroku log drains."""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class JsonFormatter(logging.Formatter):
    """Formatter yang mengeluarkan log dalam format JSON (satu baris per record).

    Cocok untuk Heroku karena setiap baris stdout otomatis terkirim ke
    Heroku LogDNA / Papertrail / Datadog. Hindari karakter newline di pesan.
    """

    def format(self, record: logging.LogRecord) -> str:
        # Tambahkan timestamp ISO8601 UTC
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # Sertakan field tambahan jika ada (dari extra=...)
        for key in ("user_id", "phone", "otp_id", "event", "status"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value

        # Sertakan exception info jika ada
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # Pastikan tidak ada newline agar Heroku tidak memecah baris
        return json.dumps(payload, ensure_ascii=False, default=str).replace("\n", " ")


def setup_logging(level: int = logging.INFO) -> None:
    """Konfigurasi root logger untuk Heroku (stdout, JSON, satu baris per record)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    # Bersihkan handler yang mungkin sudah ada (re-init aman)
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Reduksi noise dari library pihak ketiga
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("redis").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Ambil logger dengan nama modul."""
    return logging.getLogger(name)

```

## File: `main.py`

```py
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
```

## File: `otp_service.py`

```py
"""OTP service - orchestrasi lengkap flow OTP resmi Telegram.

Layer ini menggabungkan:
- Validasi nomor HP (phonenumbers library)
- Rate limiting (RateLimiter + Storage)
- Pengiriman OTP resmi (Telethon)
- Penyimpanan state (Redis via Storage)
- Cooldown kirim ulang (Storage)
- Verifikasi kode (Telethon sign_in)
- Cleanup sesi (Telethon logout + Redis purge)

Bot handlers hanya berinteraksi dengan service ini, tidak langsung
ke storage/telethon. Memudahkan testing & maintenance.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import phonenumbers
from phonenumbers import NumberParseException

from .config import settings
from .logger import get_logger
from .rate_limiter import RateLimiter, RateLimitResult
from .storage import OtpRecord, storage
from .telethon_manager import get_telethon_manager

log = get_logger(__name__)


# ============================================================
# Result Types (enum-based untuk readability)
# ============================================================


class SendOtpStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PHONE_INVALID = "PHONE_INVALID"
    RATE_LIMITED = "RATE_LIMITED"
    LOCKED = "LOCKED"
    TELETHON_FAILED = "TELETHON_FAILED"
    FLOOD_WAIT = "FLOOD_WAIT"


class VerifyOtpStatus(str, Enum):
    SUCCESS = "SUCCESS"
    NO_ACTIVE_OTP = "NO_ACTIVE_OTP"
    EXPIRED = "EXPIRED"
    WRONG_CODE = "WRONG_CODE"
    MAX_ATTEMPTS_REACHED = "MAX_ATTEMPTS_REACHED"
    LOCKED = "LOCKED"
    TELETHON_FAILED = "TELETHON_FAILED"
    FLOOD_WAIT = "FLOOD_WAIT"


@dataclass
class SendOtpOutcome:
    status: SendOtpStatus
    phone: str = ""
    expiry_seconds: int = 0
    retry_after_seconds: int = 0  # untuk RATE_LIMITED / FLOOD_WAIT
    error_message: str = ""


@dataclass
class VerifyOtpOutcome:
    status: VerifyOtpStatus
    phone: str = ""
    attempts_remaining: int = 0
    needs_2fa: bool = False
    error_message: str = ""


# ============================================================
# Service
# ============================================================


class OtpService:
    """Service layer untuk semua operasi OTP."""

    def __init__(self):
        self.rate_limiter = RateLimiter(
            max_requests=settings.rate_limit_max_requests,
            window_seconds=settings.rate_limit_window,
        )

    # ============================================================
    # Phone Validation
    # ============================================================

    @staticmethod
    def validate_phone(raw_phone: str) -> Optional[str]:
        """Validasi nomor HP dan kembalikan format E.164.

        Args:
            raw_phone: Nomor HP dari kontak Telegram, biasanya sudah ada kode negara.

        Returns:
            Nomor dalam format E.164 (contoh: +628123456789) atau None jika invalid.
        """
        if not raw_phone:
            return None
        # Pastikan ada tanda + di depan untuk parsing
        phone = raw_phone.strip()
        if not phone.startswith("+"):
            # Telegram contact kadang memberi nomor tanpa +
            phone = "+" + phone.lstrip("+")
        try:
            parsed = phonenumbers.parse(phone, None)
            if not phonenumbers.is_valid_number(parsed):
                return None
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        except NumberParseException:
            # Fallback: coba dengan region ID
            try:
                parsed = phonenumbers.parse(phone, "ID")
                if phonenumbers.is_valid_number(parsed):
                    return phonenumbers.format_number(
                        parsed, phonenumbers.PhoneNumberFormat.E164
                    )
            except NumberParseException:
                pass
            return None

    # ============================================================
    # Send OTP (initial)
    # ============================================================

    async def send_otp(self, user_id: int, raw_phone: str) -> SendOtpOutcome:
        """Kirim OTP resmi Telegram ke nomor user.

        Steps:
            1. Validasi format nomor HP
            2. Cek apakah user sedang di-lockout
            3. Cek rate limit
            4. Call Telethon send_code_request
            5. Simpan state ke Redis (dengan TTL = expiry OTP)
            6. Set cooldown kirim ulang

        Args:
            user_id: ID Telegram user
            raw_phone: Nomor HP dari kontak Telegram

        Returns:
            SendOtpOutcome dengan status dan metadata.
        """
        # 1. Validasi nomor HP
        phone = self.validate_phone(raw_phone)
        if phone is None:
            return SendOtpOutcome(
                status=SendOtpStatus.PHONE_INVALID,
                error_message="Format nomor HP tidak valid.",
            )

        # 2. Cek lockout
        if await storage.is_locked(user_id):
            return SendOtpOutcome(
                status=SendOtpStatus.LOCKED,
                error_message="User sedang di-lockout karena terlalu banyak percobaan salah.",
            )

        # 3. Cek rate limit (increment counter)
        rate: RateLimitResult = await self.rate_limiter.check(user_id)
        if not rate.allowed:
            log.warning(
                "rate_limited",
                extra={
                    "event": "rate_limited",
                    "user_id": user_id,
                    "phone": phone,
                },
            )
            return SendOtpOutcome(
                status=SendOtpStatus.RATE_LIMITED,
                phone=phone,
                retry_after_seconds=rate.retry_after_seconds,
                error_message=f"Rate limit tercapai. Coba lagi dalam {rate.retry_after_seconds} detik.",
            )

        # 4. Bersihkan state lama jika ada (OTP sebelumnya yang belum selesai)
        await self._cleanup_previous_flow(user_id)

        # 5. Kirim OTP via Telethon
        manager = get_telethon_manager()
        result = await manager.send_otp(user_id, phone)
        if not result.success:
            log.error(
                "send_otp_failed",
                extra={
                    "event": "send_otp_fail",
                    "user_id": user_id,
                    "phone": phone,
                    "error_code": result.error_code,
                },
            )
            status = (
                SendOtpStatus.FLOOD_WAIT
                if result.error_code == "FLOOD_WAIT"
                else SendOtpStatus.TELETHON_FAILED
            )
            return SendOtpOutcome(
                status=status,
                phone=phone,
                retry_after_seconds=result.flood_wait_seconds,
                error_message=result.error_message,
            )

        # 6. Simpan state ke Redis dengan TTL = OTP expiry
        record = OtpRecord(
            user_id=user_id,
            phone=phone,
            phone_code_hash=result.phone_code_hash,
            expires_at=time.time() + settings.otp_expiry_seconds,
            attempts_used=0,
            ts_sent=time.time(),
        )
        # TTL Redis sedikit lebih panjang dari OTP expiry agar kita bisa
        # deteksi "expired" dengan jelas (Redis belum auto-delete saat OTP expire)
        await storage.save_otp(record, ttl_seconds=settings.otp_expiry_seconds + 60)

        # 7. Set cooldown kirim ulang
        await storage.set_cooldown(user_id, settings.otp_resend_cooldown)

        # 8. Reset input keypad
        await storage.clear_input(user_id)

        return SendOtpOutcome(
            status=SendOtpStatus.SUCCESS,
            phone=phone,
            expiry_seconds=settings.otp_expiry_seconds,
        )

    # ============================================================
    # Resend OTP
    # ============================================================

    async def resend_otp(self, user_id: int) -> SendOtpOutcome:
        """Kirim ulang OTP ke nomor yang sama.

        Flow:
            1. Cek apakah ada OTP aktif (record di Redis)
            2. Cek cooldown kirim ulang
            3. Call Telethon send_code_request lagi (Telegram akan kirim OTP baru)
            4. Update record Redis (phone_code_hash baru, expiry baru)
            5. Reset cooldown
        """
        # 1. Ambil record OTP aktif
        record = await storage.get_otp(user_id)
        if record is None or record.is_expired:
            return SendOtpOutcome(
                status=SendOtpStatus.NO_ACTIVE_OTP if record is None else SendOtpStatus.EXPIRED,
                error_message="Tidak ada OTP aktif untuk dikirim ulang.",
            )

        # 2. Cek cooldown
        remaining_cooldown = await storage.get_cooldown_remaining(user_id)
        if remaining_cooldown > 0:
            return SendOtpOutcome(
                status=SendOtpStatus.RATE_LIMITED,
                phone=record.phone,
                retry_after_seconds=remaining_cooldown,
                error_message=f"Tunggu {remaining_cooldown} detik sebelum kirim ulang.",
            )

        # 3. Kirim OTP baru via Telethon (Telegram akan kirim kode baru)
        manager = get_telethon_manager()
        result = await manager.send_otp(user_id, record.phone)
        if not result.success:
            log.error(
                "resend_otp_failed",
                extra={
                    "event": "resend_otp_fail",
                    "user_id": user_id,
                    "phone": record.phone,
                    "error_code": result.error_code,
                },
            )
            return SendOtpOutcome(
                status=(
                    SendOtpStatus.FLOOD_WAIT
                    if result.error_code == "FLOOD_WAIT"
                    else SendOtpStatus.TELETHON_FAILED
                ),
                phone=record.phone,
                retry_after_seconds=result.flood_wait_seconds,
                error_message=result.error_message,
            )

        # 4. Update record dengan hash & expiry baru, reset attempts
        record.phone_code_hash = result.phone_code_hash
        record.expires_at = time.time() + settings.otp_expiry_seconds
        record.attempts_used = 0
        record.ts_sent = time.time()
        await storage.save_otp(record, ttl_seconds=settings.otp_expiry_seconds + 60)

        # 5. Reset cooldown
        await storage.set_cooldown(user_id, settings.otp_resend_cooldown)

        # 6. Reset input keypad
        await storage.clear_input(user_id)

        return SendOtpOutcome(
            status=SendOtpStatus.SUCCESS,
            phone=record.phone,
            expiry_seconds=settings.otp_expiry_seconds,
        )

    # ============================================================
    # Verify OTP
    # ============================================================

    async def verify_otp(self, user_id: int, code: str) -> VerifyOtpOutcome:
        """Verifikasi kode OTP yang dimasukkan user.

        Flow:
            1. Ambil record OTP aktif
            2. Cek expiry
            3. Cek lockout
            4. Call Telethon sign_in
            5. Jika sukses: cleanup & return SUCCESS
            6. Jika gagal: increment attempts, cek max attempts, lock jika perlu
        """
        # 1. Ambil record
        record = await storage.get_otp(user_id)
        if record is None:
            return VerifyOtpOutcome(
                status=VerifyOtpStatus.NO_ACTIVE_OTP,
                error_message="Tidak ada OTP aktif. Ketik /start untuk memulai.",
            )

        # 2. Cek expiry
        if record.is_expired:
            await self._cleanup_previous_flow(user_id)
            return VerifyOtpOutcome(
                status=VerifyOtpStatus.EXPIRED,
                error_message="Kode OTP sudah kedaluwarsa. Ketik /start untuk minta baru.",
            )

        # 3. Cek lockout
        if await storage.is_locked(user_id):
            return VerifyOtpOutcome(
                status=VerifyOtpStatus.LOCKED,
                error_message="Akun di-lock karena terlalu banyak percobaan salah.",
            )

        # 4. Cek apakah sudah melebihi max attempts
        if record.attempts_used >= settings.otp_max_attempts:
            await storage.lock_user(user_id, lock_seconds=1800)
            await self._cleanup_previous_flow(user_id)
            return VerifyOtpOutcome(
                status=VerifyOtpStatus.MAX_ATTEMPTS_REACHED,
                error_message=f"Maksimal {settings.otp_max_attempts} percobaan tercapai.",
            )

        # 5. Verifikasi via Telethon
        manager = get_telethon_manager()
        result = await manager.verify_otp(
            user_id=user_id,
            phone=record.phone,
            code=code,
            phone_code_hash=record.phone_code_hash,
        )

        if result.success:
            # 6. Sukses - cleanup
            phone = record.phone
            await self._cleanup_after_success(user_id)
            return VerifyOtpOutcome(
                status=VerifyOtpStatus.SUCCESS,
                phone=phone,
                needs_2fa=result.needs_2fa,
            )

        # 7. Gagal - increment attempts
        new_attempts = await storage.increment_attempts(user_id)
        remaining = max(0, settings.otp_max_attempts - new_attempts)

        # Handle FLOOD_WAIT khusus
        if result.error_code == "FLOOD_WAIT":
            return VerifyOtpOutcome(
                status=VerifyOtpStatus.FLOOD_WAIT,
                phone=record.phone,
                attempts_remaining=remaining,
                error_message=result.error_message,
            )

        if remaining <= 0:
            # Lock user setelah max attempts tercapai
            await storage.lock_user(user_id, lock_seconds=1800)
            await self._cleanup_previous_flow(user_id)
            return VerifyOtpOutcome(
                status=VerifyOtpStatus.MAX_ATTEMPTS_REACHED,
                error_message=f"Maksimal {settings.otp_max_attempts} percobaan tercapai.",
            )

        # Wrong code - masih ada sisa percobaan
        if result.error_code in ("CODE_INVALID", "CODE_UNOCCUPIED"):
            return VerifyOtpOutcome(
                status=VerifyOtpStatus.WRONG_CODE,
                phone=record.phone,
                attempts_remaining=remaining,
                error_message="Kode OTP salah.",
            )
        elif result.error_code == "CODE_EXPIRED":
            await self._cleanup_previous_flow(user_id)
            return VerifyOtpOutcome(
                status=VerifyOtpStatus.EXPIRED,
                error_message="Kode OTP sudah kedaluwarsa.",
            )

        # Telethon error lain
        return VerifyOtpOutcome(
            status=VerifyOtpStatus.TELETHON_FAILED,
            phone=record.phone,
            attempts_remaining=remaining,
            error_message=result.error_message,
        )

    # ============================================================
    # Cancel & Cleanup helpers
    # ============================================================

    async def cancel_flow(self, user_id: int) -> None:
        """Batalkan verifikasi yang sedang berjalan."""
        await self._cleanup_previous_flow(user_id)
        log.info("otp_cancelled", extra={"event": "otp_cancel", "user_id": user_id})

    async def _cleanup_previous_flow(self, user_id: int) -> None:
        """Cleanup state Redis + Telethon session untuk flow sebelumnya."""
        # Logout Telethon (hapus session file)
        try:
            manager = get_telethon_manager()
            await manager.logout_and_cleanup(user_id)
        except Exception as e:
            log.warning("cleanup_telethon_failed: %s", e)

        # Purge Redis state
        await storage.purge_user(user_id)

    async def _cleanup_after_success(self, user_id: int) -> None:
        """Cleanup khusus setelah verifikasi sukses (lebih thorough)."""
        await self._cleanup_previous_flow(user_id)
        log.info(
            "otp_success_cleaned",
            extra={"event": "otp_success_clean", "user_id": user_id},
        )

    # ============================================================
    # State queries (untuk handlers)
    # ============================================================

    async def has_active_otp(self, user_id: int) -> bool:
        """Cek apakah user punya OTP aktif yang belum expire."""
        record = await storage.get_otp(user_id)
        return record is not None and not record.is_expired

    async def get_cooldown_remaining(self, user_id: int) -> int:
        """Sisa detik cooldown kirim ulang."""
        return await storage.get_cooldown_remaining(user_id)


# Singleton service - diinisialisasi saat import
otp_service = OtpService()

```

## File: `rate_limiter.py`

```py
"""Rate limiter untuk request OTP berbasis Redis sliding window.

Implementasi sederhana: counter dengan TTL window. Cocok untuk rate limit
per-user. Untuk produksi dengan traffic tinggi, pertimbangkan sliding window
log atau token bucket, tapi untuk bot OTP ini cukup memadai.
"""
from __future__ import annotations

from dataclasses import dataclass

from .storage import storage


@dataclass
class RateLimitResult:
    """Hasil pengecekan rate limit."""

    allowed: bool
    current_count: int
    max_requests: int
    retry_after_seconds: int

    @property
    def remaining_requests(self) -> int:
        return max(0, self.max_requests - self.current_count)


class RateLimiter:
    """Rate limiter untuk OTP request per user."""

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds

    async def check(self, user_id: int) -> RateLimitResult:
        """Cek dan INCREMENT counter. Return hasil rate limit.

        Side effect: counter di Redis di-increment ATOMIK. Jika user
        belum pernah request dalam window ini, TTL diset ke window_seconds.
        """
        allowed, count, retry_after = await storage.check_rate_limit(
            user_id=user_id,
            max_requests=self.max_requests,
            window_seconds=self.window_seconds,
        )
        return RateLimitResult(
            allowed=allowed,
            current_count=count,
            max_requests=self.max_requests,
            retry_after_seconds=retry_after,
        )

    async def peek(self, user_id: int) -> RateLimitResult:
        """Cek tanpa increment (preview). Dipakai untuk notifikasi state."""
        # Baca counter tanpa increment - pakai GET langsung
        # NOTE: ini tidak atomic, tapi cukup untuk preview
        from .storage import KEY_RATELIMIT

        key = f"{KEY_RATELIMIT}:{user_id}"
        raw = await storage.client.get(key)
        count = int(raw) if raw else 0
        ttl = await storage.client.ttl(key)
        retry_after = max(0, ttl if ttl and ttl > 0 else 0)

        return RateLimitResult(
            allowed=count < self.max_requests,
            current_count=count,
            max_requests=self.max_requests,
            retry_after_seconds=retry_after,
        )

```

## File: `storage.py`

```py
"""Storage layer async untuk OTP state menggunakan Redis.

Skema key (semua dengan prefix `otpbot:`):
  - otpbot:otp:{user_id}        -> hash {phone, phone_code_hash, expires_at, attempts, ts_sent}
  - otpbot:ratelimit:{user_id}  -> counter (TTL = window rate limit)
  - otpbot:cooldown:{user_id}   -> "1" (TTL = cooldown kirim ulang)
  - otpbot:input:{user_id}     -> string digit yang sudah diketik user pada keypad
  - otpbot:locked:{user_id}    -> "1" (TTL = lockout duration)

Semua operasi async, menggunakan redis.asyncio.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional

import redis.asyncio as redis_asyncio

from .logger import get_logger

log = get_logger(__name__)

# Prefix untuk semua key agar tidak konflik dengan app lain di Redis bersama
KEY_PREFIX = "otpbot"

KEY_OTP = f"{KEY_PREFIX}:otp"          # hash
KEY_RATELIMIT = f"{KEY_PREFIX}:ratelimit"
KEY_COOLDOWN = f"{KEY_PREFIX}:cooldown"
KEY_INPUT = f"{KEY_PREFIX}:input"
KEY_LOCKED = f"{KEY_PREFIX}:locked"


@dataclass
class OtpRecord:
    """Record OTP yang sedang berjalan untuk seorang user."""

    user_id: int
    phone: str
    phone_code_hash: str
    expires_at: float  # unix timestamp
    attempts_used: int
    ts_sent: float

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    @property
    def remaining_seconds(self) -> int:
        return max(0, int(self.expires_at - time.time()))

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "phone": self.phone,
            "phone_code_hash": self.phone_code_hash,
            "expires_at": self.expires_at,
            "attempts_used": self.attempts_used,
            "ts_sent": self.ts_sent,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OtpRecord":
        return cls(
            user_id=int(d["user_id"]),
            phone=d["phone"],
            phone_code_hash=d["phone_code_hash"],
            expires_at=float(d["expires_at"]),
            attempts_used=int(d.get("attempts_used", 0)),
            ts_sent=float(d.get("ts_sent", 0)),
        )


class Storage:
    """Wrapper async untuk Redis dengan operasi khusus OTP bot."""

    def __init__(self, redis_url: str = ""):
        self._redis_url = redis_url
        self._client: Optional[redis_asyncio.Redis] = None

    def configure(self, redis_url: str) -> None:
        """Set Redis URL sebelum memanggil connect(). Dipakai dari main.py."""
        self._redis_url = redis_url

    async def connect(self) -> None:
        """Inisialisasi koneksi Redis. Health check dengan PING."""
        if not self._redis_url:
            raise RuntimeError("REDIS_URL belum dikonfigurasi. Panggil configure() dulu.")
        self._client = redis_asyncio.from_url(
            self._redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=10,
            retry_on_timeout=True,
            health_check_interval=30,
        )
        # Test koneksi
        await self._client.ping()
        log.info("redis_connected", extra={"event": "redis_connect"})

    async def close(self) -> None:
        """Tutup koneksi Redis dengan aman."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            log.info("redis_closed", extra={"event": "redis_close"})

    @property
    def client(self) -> redis_asyncio.Redis:
        if self._client is None:
            raise RuntimeError("Redis client belum diinisialisasi. Panggil connect() dulu.")
        return self._client

    # ============================================================
    # OTP Record
    # ============================================================

    async def save_otp(self, record: OtpRecord, ttl_seconds: int) -> None:
        """Simpan record OTP dengan TTL otomatis (auto-expire)."""
        key = f"{KEY_OTP}:{record.user_id}"
        value = json.dumps(record.to_dict(), ensure_ascii=False)
        await self.client.set(key, value, ex=ttl_seconds)
        log.info(
            "otp_saved",
            extra={
                "event": "otp_save",
                "user_id": record.user_id,
                "phone": record.phone,
            },
        )

    async def get_otp(self, user_id: int) -> Optional[OtpRecord]:
        """Ambil record OTP aktif. Return None jika tidak ada / sudah expire di Redis."""
        key = f"{KEY_OTP}:{user_id}"
        raw = await self.client.get(key)
        if raw is None:
            return None
        try:
            return OtpRecord.from_dict(json.loads(raw))
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            log.warning(
                "otp_record_corrupt",
                extra={"event": "otp_corrupt", "user_id": user_id},
            )
            log.exception("Corrupt OTP record for user %s: %s", user_id, e)
            await self.client.delete(key)
            return None

    async def delete_otp(self, user_id: int) -> None:
        """Hapus record OTP (setelah verifikasi sukses atau abort)."""
        key = f"{KEY_OTP}:{user_id}"
        await self.client.delete(key)

    async def increment_attempts(self, user_id: int) -> int:
        """Tambah counter percobaan OTP salah. Return nilai baru."""
        record = await self.get_otp(user_id)
        if record is None:
            return 0
        record.attempts_used += 1
        # Pertahankan TTL asli - baca TTL lalu simpan ulang
        key = f"{KEY_OTP}:{user_id}"
        ttl = await self.client.ttl(key)
        if ttl is None or ttl < 0:
            ttl = 60  # fallback 1 menit
        await self.client.set(key, json.dumps(record.to_dict()), ex=ttl)
        return record.attempts_used

    # ============================================================
    # Rate Limiting (sliding window via INCR + EXPIRE)
    # ============================================================

    async def check_rate_limit(
        self, user_id: int, max_requests: int, window_seconds: int
    ) -> tuple[bool, int, int]:
        """Cek apakah user masih dalam batas rate limit.

        Returns:
            (allowed, current_count, retry_after_seconds)
            - allowed=True jika user boleh request lagi
            - current_count = jumlah request dalam window saat ini
            - retry_after_seconds = berapa detik lagi user boleh request (0 jika allowed)
        """
        key = f"{KEY_RATELIMIT}:{user_id}"
        # INCR atomic, lalu set EXPIRE hanya jika key baru
        current = await self.client.incr(key)
        if current == 1:
            # First request in window - set TTL
            await self.client.expire(key, window_seconds)
        if current <= max_requests:
            return True, current, 0
        # Sudah lewat batas - beri tahu user berapa lama harus nunggu
        ttl = await self.client.ttl(key)
        retry_after = max(1, ttl if ttl and ttl > 0 else window_seconds)
        return False, current, retry_after

    # ============================================================
    # Resend Cooldown
    # ============================================================

    async def set_cooldown(self, user_id: int, cooldown_seconds: int) -> None:
        """Set cooldown untuk kirim ulang OTP."""
        key = f"{KEY_COOLDOWN}:{user_id}"
        await self.client.set(key, "1", ex=cooldown_seconds)

    async def get_cooldown_remaining(self, user_id: int) -> int:
        """Sisa detik cooldown. 0 jika tidak ada cooldown aktif."""
        key = f"{KEY_COOLDOWN}:{user_id}"
        ttl = await self.client.ttl(key)
        if ttl is None or ttl < 0:
            return 0
        return ttl

    async def is_in_cooldown(self, user_id: int) -> bool:
        return await self.get_cooldown_remaining(user_id) > 0

    async def clear_cooldown(self, user_id: int) -> None:
        """Hapus cooldown (untuk resend yang sukses, agar cooldown baru bisa dipasang)."""
        await self.client.delete(f"{KEY_COOLDOWN}:{user_id}")

    # ============================================================
    # Keypad Input Buffer
    # ============================================================

    async def set_input(self, user_id: int, digits: str, ttl_seconds: int = 600) -> None:
        """Simpan string digit yang sudah diketik user pada keypad."""
        key = f"{KEY_INPUT}:{user_id}"
        await self.client.set(key, digits, ex=ttl_seconds)

    async def get_input(self, user_id: int) -> str:
        """Ambil digit yang sudah diketik. Return string kosong jika belum ada."""
        key = f"{KEY_INPUT}:{user_id}"
        raw = await self.client.get(key)
        return raw if raw else ""

    async def append_digit(self, user_id: int, digit: str, max_digits: int) -> str:
        """Tambah satu digit ke buffer. Kembalikan string baru."""
        current = await self.get_input(user_id)
        if len(current) >= max_digits:
            return current  # Sudah penuh, abaikan
        new_value = current + digit
        # Pertahankan TTL lama
        key = f"{KEY_INPUT}:{user_id}"
        ttl = await self.client.ttl(key)
        if ttl is None or ttl < 0:
            ttl = 600
        await self.client.set(key, new_value, ex=ttl)
        return new_value

    async def clear_input(self, user_id: int) -> None:
        """Kosongkan buffer input."""
        await self.client.delete(f"{KEY_INPUT}:{user_id}")

    # ============================================================
    # Lockout (setelah melebihi max attempts)
    # ============================================================

    async def lock_user(self, user_id: int, lock_seconds: int = 1800) -> None:
        """Kunci user selama lock_seconds (default 30 menit)."""
        await self.client.set(f"{KEY_LOCKED}:{user_id}", "1", ex=lock_seconds)

    async def is_locked(self, user_id: int) -> bool:
        ttl = await self.client.ttl(f"{KEY_LOCKED}:{user_id}")
        return ttl is not None and ttl > 0

    # ============================================================
    # Cleanup seluruh state untuk user (verifikasi sukses / cancel)
    # ============================================================

    async def purge_user(self, user_id: int) -> None:
        """Hapus SEMUA state Redis milik user (OTP, input, cooldown, lock)."""
        # Note: rate limit TIDAK dihapus agar abuse tetap terbatasi
        keys = [
            f"{KEY_OTP}:{user_id}",
            f"{KEY_INPUT}:{user_id}",
            f"{KEY_COOLDOWN}:{user_id}",
        ]
        if keys:
            await self.client.delete(*keys)


# Singleton storage - diinisialisasi di main.py saat startup
storage = Storage("")  # URL di-set saat connect() dipanggil dari main

```

## File: `telethon_manager.py`

```py
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

```

