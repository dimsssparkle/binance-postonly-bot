from __future__ import annotations
from binance.error import ClientError

# Централизованные коды ошибок Binance USD-M Futures, на которые опирается движок.
POST_ONLY_WOULD_CROSS = -5022     # GTX ордер отклонён — сразу исполнился бы как taker
MARGIN_TYPE_ALREADY_SET = -4046   # change_margin_type на уже установленном типе
POSITION_MODE_NO_CHANGE = -4059   # change_position_mode на уже установленном режиме


def error_code(exc: ClientError) -> int | None:
    return getattr(exc, "error_code", None)


def is_code(exc: ClientError, code: int) -> bool:
    return error_code(exc) == code
