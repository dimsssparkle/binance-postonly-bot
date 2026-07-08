from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class IntentState(str, Enum):
    NEW = "new"
    CANCELLING_EXITS = "cancelling_exits"
    CLOSING_OPPOSITE = "closing_opposite"
    ENTRY_MAKER_PENDING = "entry_maker_pending"
    ENTRY_MARKET_PENDING = "entry_market_pending"
    PLACING_EXITS = "placing_exits"
    OPEN = "open"
    FLAT = "flat"
    FAILED = "failed"


class OrderRole(str, Enum):
    CLOSE_OPPOSITE = "close_opposite"
    ENTRY_MAKER = "entry_maker"
    ENTRY_MARKET = "entry_market"
    TP = "tp"
    SL = "sl"


class OrderStatus(str, Enum):
    PENDING = "pending"
    ACKED = "acked"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


@dataclass
class Intent:
    id: Optional[int]
    symbol: str
    desired_side: Side
    qty: str
    state: IntentState
    attempt_no: int = 0
    entry_price: Optional[str] = None
    failure_reason: Optional[str] = None
    created_at_ms: int = 0
    updated_at_ms: int = 0


@dataclass
class IntentOrder:
    id: Optional[int]
    intent_id: int
    role: OrderRole
    client_order_id: str
    side: str  # "BUY" | "SELL"
    order_type: str  # LIMIT | MARKET | TAKE_PROFIT_MARKET | STOP_MARKET
    exchange_order_id: Optional[int] = None
    requested_qty: Optional[str] = None
    requested_price: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: str = "0"
    commission: str = "0"
    commission_asset: Optional[str] = None
    filled_price: Optional[str] = None
    realized_pnl: str = "0"
    created_at_ms: int = 0
    updated_at_ms: int = 0
