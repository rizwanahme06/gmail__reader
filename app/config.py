import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    """Tiny .env loader so there is no extra dependency."""
    env = BASE_DIR / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env()

GMAIL_CREDENTIALS_FILE = BASE_DIR / os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = BASE_DIR / os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_QUERY = os.getenv("GMAIL_QUERY", "newer_than:7d")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "120"))

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")

DB_PATH = BASE_DIR / os.getenv("DB_PATH", "data/quotepo.db")
INBOX_DIR = BASE_DIR / os.getenv("INBOX_DIR", "data/inbox")
ATTACH_DIR = BASE_DIR / os.getenv("ATTACH_DIR", "data/attachments")
REVIEW_THRESHOLD = float(os.getenv("REVIEW_THRESHOLD", "0.80"))

for d in (DB_PATH.parent, INBOX_DIR, ATTACH_DIR):
    d.mkdir(parents=True, exist_ok=True)
