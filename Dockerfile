# Multi-arch base — works on Oracle ARM64 A1 and x86
FROM python:3.11-slim

# Install ffmpeg (from Debian/Ubuntu repos, arm64-native)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY main.py .
COPY templates/ templates/

# Storage dirs are created at runtime via volume mount,
# but ensure they exist if no volume is bound
RUN mkdir -p /app/storage/uploads /app/storage/watermark /app/storage/processed

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
