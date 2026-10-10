# RL Prenota Online — one image recipe, two targets:
#   master: web apps, Telegram bot, scheduler (no browser)
#   worker: headless Chromium + Selenium
# Base pinned by digest; Python dependencies installed only with matching hashes.

FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS base
# Italian time for pages, logs and the nightly backup (Docker defaults to UTC)
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 HOME=/tmp TZ=Europe/Rome
COPY deploy/requirements.lock /tmp/requirements.lock
RUN pip install --require-hashes --no-deps -r /tmp/requirements.lock && rm /tmp/requirements.lock
WORKDIR /app
# Code owned by root and read-only for the service user (compose runs it as the NAS user, never root)
COPY rlprenota /app/rlprenota
RUN chown -R root:root /app && chmod -R a+rX,go-w /app
USER 10001:10001

FROM base AS master
EXPOSE 8000 8001 8002
CMD ["python", "-m", "rlprenota.master"]

FROM base AS worker
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium chromium-driver fonts-liberation \
 && rm -rf /var/lib/apt/lists/*
ENV RLP_CHROMIUM_BINARY=/usr/bin/chromium RLP_CHROMEDRIVER=/usr/bin/chromedriver
USER 10001:10001
CMD ["python", "-m", "rlprenota.worker"]
