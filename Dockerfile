FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps for PyMuPDF and Pillow. libmupdf-dev is not needed since PyMuPDF
# wheels ship the fitz binary; libjpeg for Pillow JPEG support.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libjpeg62-turbo \
      libopenjp2-7 \
      libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py config.yaml manifest.json service-worker.js ./
COPY icons/ ./icons/

# Persistent volume mounted here by fly.toml
VOLUME /data
ENV PAPER_ROOT=/data

# Fly sets PORT automatically for external routing.
ENV PORT=8080
EXPOSE 8080

CMD ["python", "-u", "server.py"]
