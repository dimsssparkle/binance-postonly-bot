import os
from dotenv import load_dotenv

load_dotenv()

def env(key: str, default: str | None = None) -> str:
    val = os.getenv(key, default)
    if val is None:
        raise RuntimeError(f"Missing required env var: {key}")
    return val

API_KEY = env("BINANCE_API_KEY", "")
API_SECRET = env("BINANCE_API_SECRET", "")
BASE_URL = os.getenv("BINANCE_BASE_URL", "https://fapi.binance.com")  # USD-M Futures
SYMBOL_DEFAULT = os.getenv("SYMBOL_DEFAULT", "ETHUSDT")
QTY_DEFAULT = float(os.getenv("QTY_DEFAULT", "0.01"))
LEVERAGE_DEFAULT = int(os.getenv("LEVERAGE_DEFAULT", "10"))
ORDER_TIMEOUT_MS = int(os.getenv("ORDER_TIMEOUT_MS", "200"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "25"))  # 25 * 200ms ≈ 5 сек
CLOSE_TIMEOUT_MS = int(os.getenv("CLOSE_TIMEOUT_MS", "2500"))
HEDGE_MODE = os.getenv("HEDGE_MODE", "off")  # "off" (one-way) или "on"

# Безопасность вебхука TradingView (рекомендуется)
TV_WEBHOOK_SECRET = os.getenv("TV_WEBHOOK_SECRET", "")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
PORT = int(os.getenv("PORT", "8000"))
