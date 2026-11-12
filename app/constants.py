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
