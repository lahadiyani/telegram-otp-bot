# Telegram OTP Bot

Bot Telegram untuk verifikasi OTP resmi menggunakan **Telegram Client API** (Telethon). Bot mengirimkan kode login resmi Telegram ke nomor user dan memverifikasinya melalui keypad numerik interaktif.

> ⚠️ **Penting**: Bot ini memakai OTP resmi Telegram (lewat akun resmi *Verification Codes* / *Telegram*), BUKAN OTP custom 6-digit. Setiap pengiriman OTP akan memakan kuota login Telegram dari `API_ID`/`API_HASH` Anda.

## ✨ Fitur

- ✅ **OTP resmi Telegram** via Telethon (`client.send_code_request` + `client.sign_in`)
- 🔢 **Numeric keypad inline** (0-9, mirip dialer HP) untuk input OTP yang mobile-friendly
- 🔁 **Tombol Kirim Ulang OTP** dengan cooldown 3 menit (timer countdown)
- 🛡️ **Rate limiting**: maksimal 3 request OTP baru per 10 menit per user
- 🔒 **Max attempts**: 3x percobaan salah → user di-lockout 30 menit
- 📱 **Validasi nomor HP** otomatis (format E.164 via library `phonenumbers`)
- 🚫 **Anti-spoofing**: cek nomor kontak benar-benar milik user yang menekan tombol
- 💾 **Storage Redis** (cocok untuk Heroku add-on `heroku-redis`)
- 🌐 **Webhook mode** (hemat dyno hours Heroku - bot tidur saat tidak ada traffic)
- 📋 **JSON logging** structured ke stdout (kompatibel Heroku LogDNA / Papertrail)

## 🏗️ Arsitektur

```
┌─────────────────────────────────────────────────────────────┐
│                    Telegram Cloud                            │
│  (Bot API + Client API untuk send_code_request/sign_in)     │
└──────────┬───────────────────────────────────┬──────────────┘
           │ HTTPS webhook (port 443)         │ MTProto (TCP 443)
           ▼                                   ▼
┌─────────────────────────┐       ┌─────────────────────────┐
│  Heroku Router (TLS)     │       │   Telethon clients      │
│  https://<app>.herokuapp │       │   (per-user session)    │
└──────────┬────────────────┘       └────────────┬────────────┘
           │                                       │
           ▼                                       │
┌─────────────────────────────────────┐           │
│  Web dyno (Python 3.11)             │           │
│  ┌─────────────────────────────┐    │           │
│  │ python-telegram-bot v22     │    │           │
│  │  - Command handlers         │    │           │
│  │  - Contact handler          │    │           │
│  │  - Callback query handlers  │    │           │
│  └─────────────┬───────────────┘    │           │
│                │                     │           │
│  ┌─────────────▼───────────────┐    │           │
│  │ OTP Service (orchestrator)   │────┘           │
│  │  - validate_phone             │                │
│  │  - send_otp (Telethon)        │                │
│  │  - verify_otp (Telethon)      │                │
│  │  - rate_limiter               │                │
│  └─────────────┬───────────────┘                │
│                │                                  │
│  ┌─────────────▼───────────────┐                │
│  │ Storage Layer (async)        │                │
│  │  - OTP records (TTL)          │                │
│  │  - Rate limit counters        │                │
│  │  - Cooldown timers            │                │
│  │  - Input keypad buffer        │                │
│  └─────────────┬───────────────┘                │
└────────────────┼─────────────────────────────────┘
                 │
                 ▼
       ┌─────────────────────┐
       │  Heroku Data for     │
       │  Redis (add-on)      │
       └─────────────────────┘
```

### Komponen

| Komponen | Teknologi | Peran |
|----------|-----------|-------|
| Frontend UI | Telegram Bot API (`python-telegram-bot` v22) | KeyboardButton kontak, inline keypad, callback handlers |
| Storage Sesi | Redis (Heroku add-on) | OTP records, rate limit, cooldown, input buffer |
| Telegram Client Engine | Telethon v1.36 | `send_code_request` + `sign_in` + `log_out` |
| Security Layer | Rate limiter + max attempts + lockout | Anti-brute-force & anti-spam |
| Phone Validation | `phonenumbers` library | Format E.164 + cek nomor valid |
| Web Server | `python-telegram-bot` webhook (HTTP) | Bind ke Heroku PORT |

## 📂 Struktur Proyek

```
telegram-otp-bot/
├── app/
│   ├── __init__.py              # Package marker
│   ├── main.py                  # Entry point - webhook setup
│   ├── config.py                # Env-based config (Settings dataclass)
│   ├── logger.py                # JSON logging ke stdout
│   ├── constants.py             # Teks pesan ID + callback patterns
│   ├── storage.py               # Async Redis wrapper (OTP, rate, cooldown, input)
│   ├── rate_limiter.py          # Sliding window rate limiter
│   ├── telethon_manager.py      # TelegramClient lifecycle per user
│   ├── otp_service.py           # Orchestrator (validate → send → verify → cleanup)
│   ├── bot_handlers.py          # Telegram update handlers (command, contact, callback)
│   └── keyboards.py             # Inline keypad + ReplyKeyboardButton contact
├── sessions/                    # Telethon session files (auto-created, gitignored)
├── requirements.txt
├── runtime.txt                  # Python version untuk Heroku
├── Procfile                     # Heroku process type
├── app.json                     # Konfigurasi Heroku Button (one-click deploy)
├── .env.example                 # Template env vars untuk lokal
└── .gitignore
```

## 🚀 Deploy ke Heroku

### Prasyarat

1. **Bot Token** dari [@BotFather](https://t.me/BotFather) di Telegram.
2. **API ID & API Hash** dari [my.telegram.org](https://my.telegram.org) → API Development Tools.
3. Akun Heroku (gratis sudah cukup untuk testing).

### Opsi A: Deploy via Heroku Button (paling cepat)

1. Push repo ini ke GitHub Anda sendiri.
2. Edit `app.json`, ganti `"repository"` dengan URL repo GitHub Anda.
3. Buka README di GitHub → klik tombol "Deploy to Heroku" (atau kunjungi URL `https://heroku.com/deploy?template=https://github.com/USERNAME/telegram-otp-bot`).
4. Isi config vars (BOT_TOKEN, API_ID, API_HASH, WEBHOOK_URL) di form Heroku.
5. Klik **Deploy App**.

### Opsi B: Deploy via Heroku CLI (manual, lebih kontrol)

```bash
# 1. Clone repo ini
git clone <repo-url> telegram-otp-bot
cd telegram-otp-bot

# 2. Login Heroku & buat app
heroku login
heroku create my-telegram-otp-bot    # Ganti nama app sesuai keinginan

# 3. Tambahkan Redis add-on
#    Mini plan ($5/bln) - cukup untuk produksi kecil
heroku addons:create heroku-redis:mini -a my-telegram-otp-bot

# 4. Set config vars (WAJIB)
heroku config:set BOT_TOKEN="123456789:ABCdef..." -a my-telegram-otp-bot
heroku config:set API_ID="1234567" -a my-telegram-otp-bot
heroku config:set API_HASH="abcdef1234567890..." -a my-telegram-otp-bot

# 5. Set WEBHOOK_URL = URL aplikasi Heroku Anda
#    (Heroku otomatis menyediakan URL https://<app-name>.herokuapp.com)
heroku config:set WEBHOOK_URL="https://my-telegram-otp-bot.herokuapp.com" -a my-telegram-otp-bot

# 6. (Opsional) Set parameter OTP lain - lihat .env.example untuk default
# heroku config:set OTP_EXPIRY_SECONDS=180 -a my-telegram-otp-bot
# heroku config:set OTP_RESEND_COOLDOWN=180 -a my-telegram-otp-bot
# heroku config:set OTP_MAX_ATTEMPTS=3 -a my-telegram-otp-bot
# heroku config:set RATE_LIMIT_MAX_REQUESTS=3 -a my-telegram-otp-bot
# heroku config:set RATE_LIMIT_WINDOW=600 -a my-telegram-otp-bot

# 7. Deploy!
git push heroku main
# atau jika branch bernama master: git push heroku master

# 8. Pastikan dyno running
heroku ps:scale web=1 -a my-telegram-otp-bot

# 9. Cek log untuk memastikan webhook ter-set
heroku logs --tail -a my-telegram-otp-bot
# Anda harus melihat log JSON: {"event":"set_webhook","webhook_url":"https://..."}
```

### Verifikasi Deploy

```bash
# 1. Cek webhook sudah terdaftar di Telegram
curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo" | python -m json.tool
# Harus return: {"ok":true,"result":{"url":"https://my-app.herokuapp.com/webhook",...}}

# 2. Cek health Redis
heroku redis:info -a my-telegram-otp-bot

# 3. Test bot: kirim /start ke bot Telegram Anda
```

## 💻 Menjalankan Lokal (Development)

### Prasyarat

- Python 3.11+
- Redis lokal (atau via Docker: `docker run -d -p 6379:6379 redis:7`)

### Setup

```bash
# 1. Buat virtual env
python3.11 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy .env.example ke .env & isi nilainya
cp .env.example .env
# Edit .env: isi BOT_TOKEN, API_ID, API_HASH
# Untuk lokal, WEBHOOK_URL bisa dikosongkan - bot akan fallback...

# Wait - bot ini WEBHOOK-ONLY. Untuk lokal, Anda butuh tunnel publik (ngrok).

# 4. Jalankan ngrok untuk dapat URL publik
ngrok http 8443
# Copy URL https forward (misalnya https://abc123.ngrok.io)

# 5. Set WEBHOOK_URL ke URL ngrok di .env
echo 'WEBHOOK_URL=https://abc123.ngrok.io' >> .env

# 6. Jalankan bot
python -m app.main

# 7. Di terminal lain, tunnel ngrok sebelumnya harus tetap berjalan
```

## ⚙️ Konfigurasi

Semua konfigurasi via environment variables (lihat `.env.example`):

| Variable | Default | Deskripsi |
|----------|---------|-----------|
| `BOT_TOKEN` | (wajib) | Token dari @BotFather |
| `API_ID` | (wajib) | API ID dari my.telegram.org |
| `API_HASH` | (wajib) | API Hash dari my.telegram.org |
| `REDIS_URL` | `redis://localhost:6379/0` | URL Redis (Heroku set otomatis via add-on) |
| `WEBHOOK_URL` | (wajib) | URL publik app Heroku, mis. `https://app.herokuapp.com` |
| `PORT` | `8443` | Port HTTP untuk webhook (Heroku set otomatis) |
| `WEBHOOK_PATH` | `/webhook` | Path endpoint webhook |
| `OTP_EXPIRY_SECONDS` | `180` (3 menit) | Masa berlaku OTP |
| `OTP_RESEND_COOLDOWN` | `180` (3 menit) | Cooldown sebelum bisa kirim ulang |
| `OTP_MAX_ATTEMPTS` | `3` | Maksimal percobaan OTP salah sebelum lockout |
| `RATE_LIMIT_MAX_REQUESTS` | `3` | Maksimal request OTP baru per window |
| `RATE_LIMIT_WINDOW` | `600` (10 menit) | Window rate limit (detik) |

## 📋 Flow Penggunaan Bot

```
User: /start
Bot:  [welcome message]
Bot:  [tombol "📱 Kirim Nomor HP"]

User: [tap tombol, send contact]
Bot:  → validate_phone()
      → check rate limit (3/10min)
      → Telethon.send_code_request(phone)
      → simpan record ke Redis (TTL 240s)
      → set cooldown kirim ulang (180s)
Bot:  "✅ Kode OTP telah dikirim via Telegram..."
Bot:  [numeric keypad inline]

User: [tap 1][2][3][4][5] [✅ Verifikasi]
Bot:  → verify OTP via Telethon.sign_in()
      → SUCCESS: cleanup, kirim "🎉 Verifikasi Berhasil!"
      → WRONG: increment attempts, reset keypad, "❌ Kode salah (sisa: 2)"

User: [tap 🔁 Kirim Ulang] (jika cooldown sudah lewat)
Bot:  → Telethon.send_code_request(phone)
      → update record (hash baru, expiry baru)
      → reset cooldown
Bot:  "🔁 Kode OTP baru telah dikirim..."

User: /cancel
Bot:  → cleanup Telethon session + Redis state
Bot:  "🚫 Verifikasi dibatalkan."
```

## 🔐 Catatan Keamanan

1. **Session Telethon disimpan di filesystem dyno** (ephemeral di Heroku).
   - Artinya: jika dyno restart/sleep saat user di tengah verifikasi, session hilang
     dan user harus minta OTP baru. Trade-off dapat diterima untuk window 3 menit.
   - Untuk produksi traffic tinggi: implement custom `RedisSession` Telethon
     (lihat [Telethon Session docs](https://docs.telethon.dev/en/stable/concepts/sessions.html)).

2. **OTP code TIDAK disimpan di Redis**. Yang disimpan hanya `phone_code_hash`
   (hash dari Telegram server). Verifikasi dilakukan oleh Telegram sendiri
   via `client.sign_in()`, bukan dicocokkan lokal.

3. **Anti-spoofing contact**: bot mengecek `contact.user_id == update.effective_user.id`
   untuk memastikan nomor yang dikirim benar-benar milik user yang menekan tombol.

4. **API_ID/API_HASH adalah kredensial sensitif**. Jangan commit ke repo.
   Hanya pakai via Heroku config vars.

5. **Setelah verifikasi sukses**, bot otomatis:
   - Call `client.log_out()` ke Telegram (invalidate session)
   - Hapus session file dari filesystem
   - Purge state Redis milik user (OTP, input buffer, cooldown)

6. **Rate limit TIDAK direset setelah verifikasi sukses**. User yang sudah pakai
   3 request OTP dalam 10 menit tetap harus menunggu window selesai.

## 🐛 Troubleshooting

### Bot tidak respons setelah deploy

```bash
# 1. Cek webhook info
curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo" | python -m json.tool
# Lihat field "last_error_message" jika ada

# 2. Cek log Heroku
heroku logs --tail -a my-app

# 3. Pastikan WEBHOOK_URL benar (HTTPS, tanpa trailing slash)
heroku config:get WEBHOOK_URL -a my-app
# Harus: https://my-app.herokuapp.com (bukan .../webhook)

# 4. Pastikan dyno web running
heroku ps -a my-app
# Harus ada: web.1: up ...
```

### Telegram bilang "Too many attempts"

Telegram sendiri punya rate limit untuk `send_code_request`. Setiap API_ID/API_HASH
punya kuota global per hari. Solusi:
- Untuk testing, gunakan nomor berbeda-beda
- Untuk produksi, pertimbangkan multiple API_ID dari beberapa akun Telegram

### Redis connection error

```bash
# Cek Redis add-on
heroku addons -a my-app

# Cek REDIS_URL
heroku config:get REDIS_URL -a my-app
# Harus: redis://:password@host:port

# Restart dyno untuk re-connect
heroku restart -a my-app
```

### Session file Telethon menumpuk

Bot otomatis menghapus session file setelah verifikasi sukses atau cancel.
Tapi jika user abai (tidak verifikasi, tidak cancel), file tetap ada sampai TTL Redis habis.
Untuk Heroku ini tidak masalah (filesystem ephemeral). Untuk VPS, tambahkan cron cleanup:

```bash
# Cron harian: hapus session file lebih dari 1 jam
find sessions/ -name "*.session" -mmin +60 -delete
```

## 📈 Scaling Notes

- **Single dyno** (Eco/Basic plan) cukup untuk ~1000 verifikasi/hari
- Untuk traffic lebih, scale horizontal: `heroku ps:scale web=2`
  - Session Telethon di-cache per dyno → setiap request user harus di-handle dyno yang sama
  - Solusi: tambahkan sticky session via router Heroku (perlu add-on) atau implement RedisSession Telethon

## 📜 License

MIT License - bebas dipakai untuk proyek komersial maupun personal.

## 🤝 Kontribusi

Issue & PR welcome. Untuk perubahan besar, buka issue dulu untuk diskusi.
=======
# telegram-otp-bot
