import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return os.getenv(name) or default


PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", "http://localhost:8000")

# Dev bypass (/auth/dev-login) is active ONLY when this is exactly "local".
# Default is anything-else so a prod .env that forgets the var stays locked.
APP_ENV = env("APP_ENV", "prod")

OAUTH_CLIENT_ID = env("OAUTH_CLIENT_ID")
OAUTH_CLIENT_SECRET = env("OAUTH_CLIENT_SECRET")
SESSION_SECRET = env("SESSION_SECRET")
ALLOWED_EMAILS = [e.strip().lower() for e in env("OAUTH_ALLOWED_EMAILS").split(",") if e.strip()]

REQUIRED = ["OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET", "SESSION_SECRET", "OAUTH_ALLOWED_EMAILS"]


def missing_env() -> list[str]:
    return [k for k in REQUIRED if not env(k)]
