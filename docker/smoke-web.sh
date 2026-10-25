#!/bin/bash
set -euo pipefail
image=${1:-tdm-web:test}
scratch=$(mktemp -d)
name="tdm-web-smoke-${RANDOM}"
volume="${name}-data"
cleanup() {
    docker logs "$name" 2>/dev/null || true
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker volume rm "$volume" >/dev/null 2>&1 || true
    rm -f "$scratch/password"
    rmdir "$scratch"
}
trap cleanup EXIT
umask 077
printf '%s\n' 'offline-web-smoke-password' > "$scratch/password"
if docker run --rm --network none "$image"; then
    echo 'Missing secret incorrectly accepted'; exit 1
fi
docker volume create "$volume" >/dev/null
docker run -d --name "$name" --network none -v "$volume:/data" \
    -v "$scratch/password:/run/secrets/web_password:ro" "$image" >/dev/null
ready=false
for attempt in $(seq 1 30); do
    if [ "$(docker inspect --format '{{.State.Health.Status}}' "$name")" = healthy ]; then
        ready=true; break
    fi
    sleep 2
done
test "$ready" = true
docker exec -i "$name" python - <<'PY'
import http.cookiejar
import json
from pathlib import Path
import urllib.request
import urllib.error
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar))
root = 'http://127.0.0.1:8080'
assert op.open(root).status == 200
try:
    op.open(root + '/api/state')
except urllib.error.HTTPError as exc:
    assert exc.code == 401
else:
    raise AssertionError('Unauthenticated state access')
password = Path('/run/secrets/web_password').read_text().strip()
request = urllib.request.Request(root + '/api/login', data=json.dumps({'password': password}).encode(),
    headers={'Content-Type': 'application/json'})
session = json.load(op.open(request))
state = json.load(op.open(root + '/api/state'))
assert state['phase'] == 'stopped' and state['account']['user_id'] is None
assert state['campaigns'] == []
headers = {'Content-Type': 'application/json', 'X-CSRF-Token': session['csrf']}
request = urllib.request.Request(root + '/api/settings', method='PUT',
    data=json.dumps({'priority': ['Offline fixture game']}).encode(), headers=headers)
assert op.open(request).status == 200
# No desktop binaries, Tk, pystray or display variables are needed in this image.
import importlib.util
import sys
from twitch import Twitch
assert 'tkinter' not in sys.modules and 'gui' not in sys.modules
assert importlib.util.find_spec('pystray') is None
assert not Path('/usr/bin/Xvfb').exists() and not Path('/usr/bin/x11vnc').exists()
assert Path('/data/settings.json').exists()
print('PASS: native HTML, authenticated API, CSRF, settings persistence, no desktop dependencies')
PY
docker stop --time 150 "$name" >/dev/null
test "$(docker inspect --format '{{.State.ExitCode}}' "$name")" = 0
docker rm "$name" >/dev/null
docker run -d --name "$name" --network none -v "$volume:/data" \
    -v "$scratch/password:/run/secrets/web_password:ro" "$image" >/dev/null
sleep 2
docker exec "$name" python -c "import json; from pathlib import Path; assert json.loads(Path('/data/settings.json').read_text())['priority']==['Offline fixture game']"
echo 'PASS: graceful shutdown and data retained after recreation'
