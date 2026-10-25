#!/bin/sh
set -eu
umask 077
# Fail closed: no unauthenticated remote desktop, even on a private network.
: "${WEB_PASSWORD_FILE:=/run/secrets/web_password}"
if [ ! -s "$WEB_PASSWORD_FILE" ]; then
    echo 'Missing WEB_PASSWORD_FILE: mount a strong password as a Docker secret.' >&2
    exit 1
fi
/opt/venv/bin/python - "$WEB_PASSWORD_FILE" <<'PY'
from pathlib import Path
import sys
password = Path(sys.argv[1]).read_bytes().removesuffix(b'\n')
if not 16 <= len(password) <= 72 or any(c < 32 or c > 126 for c in password):
    raise SystemExit('Web password must be one line of 16-72 printable ASCII bytes.')
PY
mkdir -p /run/tdm /data/cache
chown miner:miner /data /data/cache
chmod 700 /data
# Password is read on stdin, never passed as a process argument or printed.
htpasswd -i -B -c /run/tdm/htpasswd admin < "$WEB_PASSWORD_FILE" >/dev/null 2>&1
chown root:www-data /run/tdm/htpasswd
chmod 640 /run/tdm/htpasswd
chmod 750 /run/tdm
chown root:www-data /run/tdm
exec /usr/bin/supervisord -n -c /opt/container/supervisord.conf
