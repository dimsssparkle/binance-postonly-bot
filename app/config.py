import os
from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str | None = None) -> str:
    val = os.getenv(key, default)
    if val is None:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


API_KEY = _env("BINANCE_API_KEY", "")
API_SECRET = _env("BINANCE_API_SECRET", "")
BASE_URL = os.getenv("BINANCE_BASE_URL", "https://fapi.binance.com")
WS_BASE_URL = os.getenv("BINANCE_WS_BASE_URL", "wss://fstream.binance.com")

SYMBOL_DEFAULT = os.getenv("SYMBOL_DEFAULT", "ETHUSDT")
QTY_DEFAULT = float(os.getenv("QTY_DEFAULT", "0.01"))
LEVERAGE_DEFAULT = int(os.getenv("LEVERAGE_DEFAULT", "10"))
HEDGE_MODE = os.getenv("HEDGE_MODE", "off")  # "off" (one-way) или "on"

ORDER_TIMEOUT_MS = int(os.getenv("ORDER_TIMEOUT_MS", "1000"))
CLOSE_TIMEOUT_MS = int(os.getenv("CLOSE_TIMEOUT_MS", "2500"))
MAX_MAKER_ATTEMPTS = int(os.getenv("MAX_MAKER_ATTEMPTS", "3"))
MAX_CLOSE_RETRIES = int(os.getenv("MAX_CLOSE_RETRIES", "25"))

# Максимальный возраст WS-кэша лучшей цены (BookDepthRecorder), при котором
# он ещё считается доверенным для ценообразования ордеров — иначе REST
# фолбэк. Нативный каданс потока ~100ms; 400ms даёт запас на джиттер,
# оставаясь заметно свежее REST-альтернативы (~259ms по замерам).
BOOK_CACHE_MAX_STALENESS_MS = int(os.getenv("BOOK_CACHE_MAX_STALENESS_MS", "400"))

TP_PCT = float(os.getenv("TP_PCT", "0.0"))
SL_PCT = float(os.getenv("SL_PCT", "0.0"))

LISTEN_KEY_KEEPALIVE_SEC = int(os.getenv("LISTEN_KEY_KEEPALIVE_SEC", str(30 * 60)))

DB_PATH = os.getenv("DB_PATH", "bot.db")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
PORT = int(os.getenv("PORT", "8000"))
