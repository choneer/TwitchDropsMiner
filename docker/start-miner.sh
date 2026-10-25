#!/bin/sh
set -eu
umask 077
# Xvfb is supervised independently; wait for readiness instead of a fixed sleep.
for attempt in $(seq 1 60); do
    if xdpyinfo -display :0 >/dev/null 2>&1; then
        exec dbus-run-session -- /opt/venv/bin/python /opt/tdm/main.py
    fi
    sleep 1
done
echo 'Display did not become ready.' >&2
exit 1
