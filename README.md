# Telegram OTP Bot

Bot Telegram untuk verifikasi nomor HP via OTP resmi Telegram (menggunakan Telethon Client API).

## Fitur

- Verifikasi OTP resmi via Telegram Client API (Telethon)
- Input kode via inline keypad (mirip dialer HP)
- Rate limiting & cooldown kirim ulang (Redis)
- Lockout otomatis setelah percobaan salah berulang
- Webhook mode (cocok untuk Railway / Heroku)
- Logging structured JSON (siap dikirim ke log drain)

## Struktur Project

```
.
├── app/                      # Package bot
│   ├── __init__.py
│   ├── main.py               # Entry point: python -m app.main
│   ├── bot_handlers.py       # Command & callback handlers
│   ├── config.py             # Settings dari env vars
│   ├── constants.py          # Teks UI & konstanta
│   ├── keyboards.py          # Inline & reply keyboards
│   ├── logger.py             # JSON logger
│   ├── otp_service.py        # Service layer OTP
│   ├── rate_limiter.py       # Rate limiter (Redis)
│   ├── storage.py            # Redis storage wrapper
│   └── telethon_manager.py   # Telethon client lifecycle
├── requirements.txt
├── runtime.txt               # Python 3.12.7
├── railway.toml              # Railway config
├── Procfile                  # Fallback start command
├── .env.example
└── .gitignore
```

## Setup Lokal

### 1. Clone & install dependencies

```bash
git clone <repo-anda>
cd <nama-repo>
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Dapatkan credentials Telegram

- **Bot Token**: dari [@BotFather](https://t.me/BotFather) → `/newbot`
- **API_ID & API_HASH**: dari https://my.telegram.org → API Development Tools

### 3. Setup Redis

```bash
# Opsional: pakai Docker untuk Redis lokal
docker run -d --name redis -p 6379:6379 redis:7-alpine
```

### 4. Konfigurasi env vars

```bash
cp .env.example .env
# Edit .env dengan token & credentials Anda
```

Untuk webhook lokal, gunakan tunnel:
```bash
ngrok http 8443
# Set WEBHOOK_URL di .env ke URL ngrok, mis. https://abc.ngrok.app
```

### 5. Run

```bash
python -m app.main
```

## Deploy ke Railway

### Cara 1: Connect GitHub repo (recommended)

1. Push project ke GitHub
2. Buka https://railway.com → New Project → Deploy from GitHub repo
3. Pilih repo
4. Tambah Redis plugin: + Add → Database → Redis
5. Set environment variables di tab Variables:
   - `BOT_TOKEN` (dari @BotFather)
   - `API_ID` (dari my.telegram.org)
   - `API_HASH` (dari my.telegram.org)
   - `WEBHOOK_URL` = URL Railway app Anda (mis. `https://bot-otp-anda.up.railway.app`)
6. Railway akan auto-deploy menggunakan `railway.toml`

### Cara 2: Railway CLI

```bash
npm i -g @railway/cli
railway login
railway init
railway add --database redis
railway up
railway variables set BOT_TOKEN=...
railway variables set API_ID=...
railway variables set API_HASH=...
railway variables set WEBHOOK_URL=https://<domain>.up.railway.app
```

## Set Webhook Telegram (Otomatis)

Bot ini otomatis mendaftarkan webhook ke Telegram saat startup via
`application.updater.start_webhook(webhook_url=...)`. Anda tidak perlu
manual set webhook via API Telegram.

## Environment Variables

Lihat [`.env.example`](.env.example) untuk daftar lengkap.

| Variable | Wajib | Default | Keterangan |
|---|---|---|---|
| `BOT_TOKEN` | Yes | - | Token dari @BotFather |
| `API_ID` | Yes | - | Dari my.telegram.org |
| `API_HASH` | Yes | - | Dari my.telegram.org |
| `WEBHOOK_URL` | Yes | - | URL publik Railway app |
| `REDIS_URL` | Auto | `redis://localhost:6379/0` | Otomatis dari Railway Redis plugin |
| `PORT` | Auto | `8443` | Otomatis dari Railway |
| `OTP_EXPIRY_SECONDS` | No | `180` | Masa berlaku OTP |
| `OTP_RESEND_COOLDOWN` | No | `180` | Cooldown kirim ulang |
| `OTP_MAX_ATTEMPTS` | No | `3` | Maks percobaan sebelum lockout |
| `RATE_LIMIT_MAX_REQUESTS` | No | `3` | Batas request per window |
| `RATE_LIMIT_WINDOW` | No | `600` | Window rate limit (detik) |
| `WEBHOOK_PATH` | No | `/webhook` | Path endpoint webhook |

## Commands Bot

- `/start` - Mulai verifikasi OTP
- `/cancel` - Batalkan verifikasi yang berjalan
- `/help` - Tampilkan bantuan

## Notes untuk Produksi

- **Session Telethon** disimpan di filesystem container (ephemeral di Railway).
  Saat container restart, session untuk flow yang sedang berjalan hilang.
  Untuk produksi traffic tinggi, pertimbangkan custom RedisSession Telethon.

- **Rate limit Telegram**: API resmi punya batas ketat (FloodWait).
  Bot sudah handle `FloodWaitError` dan kasih pesan ke user.

- **Privacy mode**: Pastikan bot tidak dalam Privacy Mode (untuk membaca pesan di grup),
  atau batasi bot ke private chat saja.