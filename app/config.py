import os
import secrets
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_URL = f"sqlite:///{DATA_DIR / 'lanparty.db'}"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")
# Accounts with these verified emails become organizers when they sign in.
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}
SITE_NAME = os.getenv("SITE_NAME", "LAN Party")
# Absolute URL used in QR codes / player links, e.g. https://lan.wolfe.house
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
# LAN address players type into games to reach hosted servers, e.g. 192.168.1.50
PUBLIC_HOST = os.getenv("PUBLIC_HOST", "")
DEFAULT_WATTS = int(os.getenv("DEFAULT_WATTS", "450"))

# ---- sign-in providers (each one is offered only when its keys are set) ----
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")  # optional, fills in persona name and avatar
STEAM_LOGIN = os.getenv("STEAM_LOGIN", "true").lower() in {"1", "true", "yes", "on"}
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
APPLE_CLIENT_ID = os.getenv("APPLE_CLIENT_ID", "")  # the Services ID, e.g. com.example.lan.web
APPLE_TEAM_ID = os.getenv("APPLE_TEAM_ID", "")
APPLE_KEY_ID = os.getenv("APPLE_KEY_ID", "")
APPLE_PRIVATE_KEY = os.getenv("APPLE_PRIVATE_KEY", "").replace("\\n", "\n")
APPLE_PRIVATE_KEY_FILE = os.getenv("APPLE_PRIVATE_KEY_FILE", "")

# ---- email (magic sign-in links, receipts, gifted tickets) ----
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)
SMTP_SECURITY = os.getenv("SMTP_SECURITY", "starttls").lower()  # starttls | ssl | none
# Without SMTP, write sign-in links to the container log instead. For testing only.
EMAIL_TO_LOG = os.getenv("EMAIL_TO_LOG", "").lower() in {"1", "true", "yes", "on"}

# ---- payments ----
CURRENCY = os.getenv("CURRENCY", "USD").upper()
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET", "")
PAYPAL_MODE = os.getenv("PAYPAL_MODE", "sandbox").lower()  # sandbox | live
# How long unpaid checkouts hold their tickets. Stripe's minimum session lifetime is 30 minutes.
HOLD_MINUTES = 31


def apple_private_key() -> str:
    if APPLE_PRIVATE_KEY:
        return APPLE_PRIVATE_KEY
    if APPLE_PRIVATE_KEY_FILE and Path(APPLE_PRIVATE_KEY_FILE).exists():
        return Path(APPLE_PRIVATE_KEY_FILE).read_text()
    return ""


def secret_key() -> str:
    if os.getenv("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    f = DATA_DIR / ".secret_key"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(32))
    return f.read_text().strip()
