FROM python:3.11.17-slim@sha256:cce12316475d7d56d605f76e1d811d0dc4e7227f2d4afd29951521264f504ff8
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PHONEBRIDGE_DB_PATH=/data/phonebridge.sqlite3
RUN useradd --system --uid 10001 --user-group --no-create-home --home-dir /data --shell /usr/sbin/nologin phonebridge \
    && install -d -m 0700 -o phonebridge -g phonebridge /data
COPY requirements.txt /build/requirements.txt
RUN pip install --requirement /build/requirements.txt
COPY . /build
RUN pip install --no-deps /build && rm -rf /build
USER phonebridge
WORKDIR /data
VOLUME ["/data"]
EXPOSE 18940
CMD ["python", "-m", "uvicorn", "phonebridge_server.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "18940", "--workers", "1", "--no-access-log", "--ws", "websockets-sansio", "--ws-max-size", "1048576", "--ws-max-queue", "8", "--ws-ping-interval", "20", "--ws-ping-timeout", "20", "--limit-concurrency", "256", "--timeout-keep-alive", "5", "--timeout-graceful-shutdown", "15"]
