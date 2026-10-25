FROM debian:bookworm-slim

LABEL org.opencontainers.image.source="https://github.com/choneer/TwitchDropsMiner" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.title="Twitch Drops Miner - Docker fork"

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:0 HOME=/data TDM_DATA_DIR=/data \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 XMODIFIERS=@im=none

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates python3 python3-venv python3-tk python3-gi python3-pil.imagetk \
    gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 gir1.2-notify-0.7 \
    at-spi2-core xfce4-notifyd python3-dev libgirepository1.0-dev libcairo2-dev \
    pkg-config gcc libc6-dev xvfb x11-utils x11vnc openbox tint2 dbus-x11 \
    novnc websockify nginx apache2-utils supervisor fonts-noto-cjk tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --home-dir /data miner \
    && python3 -m venv --system-site-packages /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
    && /opt/venv/bin/python -c 'import PIL.ImageTk, tkinter, gi, truststore'
WORKDIR /opt/tdm
COPY *.py ./
COPY LICENSE ./LICENSE
COPY icons ./icons
COPY lang ./lang
COPY docker /opt/container
RUN chmod +x /opt/container/*.sh \
    && rm -f /etc/nginx/sites-enabled/default \
    && cp /opt/container/nginx.conf /etc/nginx/nginx.conf
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD ["/opt/venv/bin/python", "/opt/container/healthcheck.py"]
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/container/entrypoint.sh"]
