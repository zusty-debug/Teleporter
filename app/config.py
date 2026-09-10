"""Application configuration loaded from environment variables / .env file."""
import os
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.environ.get("DB_PATH", os.path.join(DATA_DIR, "teleporter.db"))

# Optional access password for the web UI. If set, every /api call requires it.
APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()

# Optional pre-fill defaults for Telegram credentials
DEFAULT_API_ID = os.environ.get("DEFAULT_API_ID", "").strip()
DEFAULT_API_HASH = os.environ.get("DEFAULT_API_HASH", "").strip()

# Default pause (seconds) between copied messages, to stay under Telegram flood limits
DEFAULT_DELAY = float(os.environ.get("DEFAULT_DELAY", "1.0"))

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

APP_NAME = "Teleporter"
APP_VERSION = "1.0.0"
