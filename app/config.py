import os
import secrets
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_URL = f"sqlite:///{DATA_DIR / 'lanparty.db'}"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")
# Absolute URL used in QR codes / player links, e.g. https://lan.wolfe.house
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
# LAN address players type into games to reach hosted servers, e.g. 192.168.1.50
PUBLIC_HOST = os.getenv("PUBLIC_HOST", "")
DEFAULT_WATTS = int(os.getenv("DEFAULT_WATTS", "450"))


def secret_key() -> str:
    if os.getenv("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    f = DATA_DIR / ".secret_key"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(32))
    return f.read_text().strip()
