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
