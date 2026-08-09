FROM python:3.12-slim-bookworm

ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BASK_DATA_DIR=/data \
    DBUS_SYSTEM_BUS_ADDRESS=unix:path=/var/run/dbus/system_bus_socket \
    HOME=/tmp

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid "$APP_GID" bask \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --no-create-home \
       --home-dir /nonexistent --shell /usr/sbin/nologin bask \
    && install -d -o "$APP_UID" -g "$APP_GID" -m 0700 /data

COPY --chown=${APP_UID}:${APP_GID} scanner ./scanner
COPY --chown=${APP_UID}:${APP_GID} server ./server
COPY --chown=${APP_UID}:${APP_GID} frontend ./frontend
COPY --chown=${APP_UID}:${APP_GID} config.example.json .
COPY --chown=${APP_UID}:${APP_GID} docker-entrypoint.sh .
RUN chmod 0555 docker-entrypoint.sh

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3).read()"]

USER bask:bask
ENTRYPOINT ["./docker-entrypoint.sh"]
