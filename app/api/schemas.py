from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, field_validator


def _validate_side(v: str) -> str:
    v2 = v.lower()
    if v2 not in ("long", "short", "flat"):
        raise ValueError("side must be 'long', 'short' or 'flat'")
    return v2


class ManualTradePayload(BaseModel):
    side: str
    qty: Optional[float] = None

    @field_validator("side")
    @classmethod
    def validate_side(cls, v: str) -> str:
        return _validate_side(v)


class TradingSettingsPayload(BaseModel):
    leverage: Optional[int] = None
    qty: Optional[float] = None
    tp_pct: Optional[float] = None
    sl_pct: Optional[float] = None

    @field_validator("tp_pct", "sl_pct")
    @classmethod
    def validate_non_negative(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v < 0:
            raise ValueError("must be >= 0")
        return v

    @field_validator("qty")
    @classmethod
    def validate_qty(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v <= 0:
            raise ValueError("qty must be > 0")
        return v

    @field_validator("leverage")
    @classmethod
    def validate_leverage(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and not (1 <= v <= 125):
            raise ValueError("leverage must be between 1 and 125")
        return v
