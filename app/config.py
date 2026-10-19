import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(ROOT_DIR / ".env")


def get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return database_url
    return f"sqlite:///{DATA_DIR / 'paper_trading.db'}"


def get_delta_ws_url() -> str:
    return os.getenv("DELTA_WS_URL", "wss://public-socket.india.delta.exchange")


def get_jwt_secret() -> str:
    return os.getenv("JWT_SECRET", "dev-insecure-secret-change-me")


def get_jwt_expire_minutes() -> int:
    return int(os.getenv("JWT_EXPIRE_MINUTES", "10080"))


def get_cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:3000")
    return [o.strip() for o in raw.split(",") if o.strip()]


def get_seed_admin() -> tuple[str | None, str | None]:
    email = os.getenv("SEED_ADMIN_EMAIL")
    password = os.getenv("SEED_ADMIN_PASSWORD")
    if email and password:
        return email, password
    return None, None
