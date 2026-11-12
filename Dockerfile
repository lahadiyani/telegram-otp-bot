# Dockerfile backup - pakai jika Nixpacks bermasalah pin Python version.
# Untuk menggunakannya, ubah builder Railway ke "DOCKERFILE" atau hapus railway.toml.

FROM python:3.12-slim AS builder

# Build deps untuk cryptography
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies dulu (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ============================================================
# Runtime image
# ============================================================
FROM python:3.12-slim

# Copy installed packages dari builder
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy source code
COPY . .

# Buat direktori sessions (untuk Telethon)
RUN mkdir -p /app/sessions

# Non-root user untuk keamanan
RUN useradd -m -u 1000 botuser && chown -R botuser:botuser /app
USER botuser

# Railway set PORT otomatis. Expose sebagai dokumentasi.
EXPOSE 8080

# Healthcheck: cek process jalan via Python
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# Start command - pakai -m agar relative import jalan
CMD ["python", "-m", "app.main"]