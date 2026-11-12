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
