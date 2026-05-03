FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        iputils-ping \
        nmap \
        curl \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# rclone (MIT) — multi-cloud uploader, ~50 MB single Go binary.
ARG RCLONE_REL=1.68.2
RUN ARCH="$(dpkg --print-architecture)" && \
    case "$ARCH" in \
      amd64) URL_ARCH=amd64 ;; \
      arm64) URL_ARCH=arm64 ;; \
      *) echo "unsupported arch: $ARCH" && exit 1 ;; \
    esac && \
    curl -fsSL -o /tmp/rclone.zip "https://downloads.rclone.org/v${RCLONE_REL}/rclone-v${RCLONE_REL}-linux-${URL_ARCH}.zip" && \
    unzip -q /tmp/rclone.zip -d /tmp && \
    install -m 0755 /tmp/rclone-v${RCLONE_REL}-linux-${URL_ARCH}/rclone /usr/local/bin/rclone && \
    rm -rf /tmp/rclone* && \
    rclone version

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/

# VERSION file is read at runtime by app.updater.current_version().
# Build pipelines override this via `docker build --build-arg VERSION=...`.
ARG VERSION=dev
RUN echo "$VERSION" > /app/app/VERSION

# NOTICES.md is mounted in via docker-compose (the project root holds
# it). Route looks at /data/NOTICES.md and falls back to repo-root.

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
