# SIVED. Python 3.12 and the standard library. No pip packages.
# python:3.12-slim-bookworm
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

# LibreOffice is large. Leave it out unless authors upload legacy .doc.
ARG INSTALL_SOFFICE=0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        openssl \
        poppler-utils \
    && if [ "$INSTALL_SOFFICE" = "1" ]; then \
        apt-get install -y --no-install-recommends libreoffice-writer; \
    fi \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/desk

COPY app ./app
COPY migrations ./migrations
COPY share ./share
COPY docs ./docs
COPY README.md LICENSE NOTICE ./

LABEL org.opencontainers.image.title="SIVED" \
    org.opencontainers.image.description="Super Intelligence Virtual Editting Desk from KlemSidd LLC. The Python application is MIT. Debian packages in this image keep their own licenses." \
    org.opencontainers.image.source="https://github.com/KlemSidd/sived" \
    org.opencontainers.image.vendor="KlemSidd LLC"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8080

RUN groupadd --gid 10001 desk \
    && useradd --uid 10001 --gid desk --home-dir /tmp --no-create-home --shell /usr/sbin/nologin desk \
    && mkdir -p /data \
    && chown desk:desk /data

USER desk

VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"

CMD ["python", "-m", "app", "web"]
