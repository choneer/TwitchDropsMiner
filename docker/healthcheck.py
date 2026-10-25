"""Check the GUI process, display and authentication boundary without Twitch requests."""
import subprocess
import urllib.error
import urllib.request

for name in ("display", "desktop", "panel", "vnc", "websocket", "miner", "web"):
    result = subprocess.run(
        ["supervisorctl", "-c", "/opt/container/supervisord.conf", "status", name],
        capture_output=True, text=True, timeout=3,
    )
    if result.returncode or "RUNNING" not in result.stdout:
        raise SystemExit(1)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    opener.open("http://127.0.0.1:8080/vnc.html", timeout=3)
except urllib.error.HTTPError as exc:
    if exc.code != 401:
        raise SystemExit(1)
else:
    raise SystemExit(1)
