# opencloddie server image.  The server is pure-stdlib Python 3.12; no pip
# install step and no runtime dependency on Claude Code or tmux.  Sessions live
# on the clients; this container is the gateway, web UI, and session registry.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/app/bin:$PATH \
    OPENCLODDIE_CONFIG=/data/config.json \
    OPENCLODDIE_STATE_DIR=/data

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin opencloddie

WORKDIR /app
COPY opencloddie /app/opencloddie
COPY bin /app/bin
COPY deploy/entrypoint.sh /entrypoint.sh

RUN chmod 0755 /app/bin/opencloddie /app/bin/opencloddie-server /entrypoint.sh \
    && mkdir -p /data \
    && chown -R opencloddie:opencloddie /app /data

USER opencloddie
VOLUME ["/data"]
EXPOSE 8787 8788

HEALTHCHECK --interval=30s --timeout=6s --start-period=15s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3); urllib.request.urlopen('http://127.0.0.1:8788/healthz', timeout=3)"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["run"]
