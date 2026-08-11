FROM python:3.12-slim-bookworm

ARG APP_UID=10001
ARG APP_GID=10001
ARG VCS_REF=unknown
ARG VERSION=dev
ARG SOURCE_URL=https://github.com/jlyfshhh/bask

LABEL org.opencontainers.image.title="Bask" \
      org.opencontainers.image.description="Local-first enclosure climate monitoring" \
      org.opencontainers.image.source="$SOURCE_URL" \
      org.opencontainers.image.revision="$VCS_REF" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BASK_DATA_DIR=/data \
    HOME=/tmp

WORKDIR /app

COPY requirements.txt .
# The appliance never installs packages at runtime. Keeping pip in the
# published image adds an unnecessary package manager and previously left its
# own advisories in an otherwise clean runtime dependency scan.
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m pip uninstall --yes pip

# This tiny filtering proxy is the only process allowed to see the host system
# bus. It authenticates to BlueZ as root, then exposes a method-level allowlist
# to the unprivileged scanner container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends xdg-dbus-proxy \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid "$APP_GID" bask \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --no-create-home \
       --home-dir /nonexistent --shell /usr/sbin/nologin bask \
    && install -d -o "$APP_UID" -g "$APP_GID" -m 0700 /data

COPY --chown=${APP_UID}:${APP_GID} scanner ./scanner
COPY --chown=${APP_UID}:${APP_GID} server ./server
COPY --chown=${APP_UID}:${APP_GID} frontend ./frontend
COPY --chown=${APP_UID}:${APP_GID} config.example.json .
COPY --chown=${APP_UID}:${APP_GID} docker-entrypoint.sh .
COPY --chown=0:0 dbus-proxy-entrypoint.sh .
RUN chmod 0555 docker-entrypoint.sh dbus-proxy-entrypoint.sh

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3).read()"]

USER bask:bask
ENTRYPOINT ["./docker-entrypoint.sh"]
