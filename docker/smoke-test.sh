#!/bin/bash
# Isolated test: no Twitch/network access, credentials or production data.
set -euo pipefail
image=${1:-tdm:test}
test_root=$(mktemp -d)
name="tdm-smoke-${RANDOM}"
volume="${name}-data"
cleanup() {
    docker logs "$name" 2>/dev/null || true
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker volume rm "$volume" >/dev/null 2>&1 || true
    rm -f "$test_root/password"
    rmdir "$test_root"
}
trap cleanup EXIT
umask 077
printf '%s\n' 'offline-smoke-password-not-for-production' > "$test_root/password"
if docker run --rm --network none "$image"; then
    echo 'Missing password was incorrectly accepted' >&2
    exit 1
fi
docker volume create "$volume" >/dev/null
docker run -d --name "$name" --network none \
    -v "$volume:/data" \
    -v "$test_root/password:/run/secrets/web_password:ro" "$image" >/dev/null
ready=false
for attempt in $(seq 1 60); do
    if [ "$(docker inspect --format '{{.State.Health.Status}}' "$name")" = healthy ]; then
        ready=true
        break
    fi
    sleep 2
done
if [ "$ready" != true ]; then
    docker inspect --format '{{json .State.Health}}' "$name"
    docker exec "$name" supervisorctl -c /opt/container/supervisord.conf status || true
    docker exec --user miner "$name" timeout 10 /opt/container/start-miner.sh || true
    exit 1
fi
docker exec -i "$name" /opt/venv/bin/python - <<'PY'
import base64
import socket
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, '/opt/tdm')
sys.argv[0] = '/opt/tdm/main.py'
import constants
assert constants.SETTINGS_PATH == Path('/data/settings.json')
assert constants.COOKIES_PATH == Path('/data/cookies.jar')
assert constants.LANG_PATH == Path('/opt/tdm/lang')
assert constants.LANG_PATH.is_dir()
desktop_env = dict(os.environ)
desktop_env.pop('TDM_DATA_DIR', None)
subprocess.run([sys.executable, '-c',
    "import sys; sys.path.insert(0, '/opt/tdm'); sys.argv[0]='/opt/tdm/main.py'; "
    "import constants; assert constants.DATA_DIR == constants.WORKING_DIR; "
    "assert constants.COOKIES_PATH == constants.WORKING_DIR / 'cookies.jar'"],
    env=desktop_env, check=True)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for path in ('/vnc.html', '/websockify'):
    try:
        opener.open('http://127.0.0.1:8080' + path)
    except urllib.error.HTTPError as exc:
        assert exc.code == 401, exc.code
    else:
        raise AssertionError('Anonymous desktop access')
password = Path('/run/secrets/web_password').read_text().strip()
auth = base64.b64encode(('admin:' + password).encode()).decode()
request = urllib.request.Request('http://127.0.0.1:8080/vnc.html',
    headers={'Authorization': 'Basic ' + auth})
assert opener.open(request, timeout=5).status == 200
with socket.create_connection(('127.0.0.1', 8080), timeout=5) as sock:
    sock.sendall((
        'GET /websockify HTTP/1.1\r\nHost: localhost\r\n'
        'Upgrade: websocket\r\nConnection: Upgrade\r\n'
        'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n'
        'Sec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: binary\r\n'
        'Authorization: Basic ' + auth + '\r\n\r\n').encode())
    response = sock.recv(4096)
    assert b'101 Switching Protocols' in response, response[:100]
Path('/data/.smoke-persistence').write_text('retained')
assert Path('/data').stat().st_mode & 0o777 == 0o700
print('PASS: GUI processes, authentication, HTML, WebSocket, state/resource separation')
PY
# Test graceful stop and a new container using the same volume, not just restart.
docker stop --time 150 "$name" >/dev/null
test "$(docker inspect --format '{{.State.ExitCode}}' "$name")" = 0
docker rm "$name" >/dev/null
docker run -d --name "$name" --network none \
    -v "$volume:/data" \
    -v "$test_root/password:/run/secrets/web_password:ro" "$image" >/dev/null
docker exec "$name" /opt/venv/bin/python -c \
    "from pathlib import Path; assert Path('/data/.smoke-persistence').read_text() == 'retained'"
echo 'PASS: graceful stop and volume persistence after recreation'
