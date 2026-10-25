"""Small root initializer; the application itself always runs as UID/GID 1000."""
from pathlib import Path
import os
import sys

os.umask(0o077)
source = Path(os.environ.get('WEB_PASSWORD_FILE', '/run/secrets/web_password'))
try:
    password = source.read_bytes().removesuffix(b'\n')
except OSError:
    raise SystemExit('Missing readable WEB_PASSWORD_FILE; mount a web password secret') from None
if not 16 <= len(password) <= 72 or any(c < 32 or c > 126 for c in password):
    raise SystemExit('Mount a single-line 16–72 printable ASCII byte web password secret')
for path in (Path('/data'), Path('/data/cache'), Path('/data/web-logs'), Path('/run/tdm-web')):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(path, 1000, 1000)
    os.chmod(path, 0o700)
target = Path('/run/tdm-web/web-password')
target.write_bytes(password)
os.chown(target, 1000, 1000)
os.chmod(target, 0o400)
os.environ['WEB_PASSWORD_FILE'] = str(target)
os.setgroups([])
os.setgid(1000)
os.setuid(1000)
os.execv(sys.executable, [sys.executable, '/opt/tdm/web_main.py'])
