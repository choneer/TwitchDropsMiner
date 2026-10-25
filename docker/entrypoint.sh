#!/bin/sh
set -eu
umask 077
# Fail closed: no unauthenticated remote desktop, even on a private network.
: "${WEB_PASSWORD_FILE:=/run/secrets/web_password}"
if [ ! -s "$WEB_PASSWORD_FILE" ]; then
    echo 'Missing WEB_PASSWORD_FILE: mount a strong password as a Docker secret.' >&2
    exit 1
fi
if [ "$(wc -c < "$WEB_PASSWORD_FILE")" -lt 16 ]; then
    echo 'Web password must contain at least 16 bytes.' >&2
    exit 1
fi
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
