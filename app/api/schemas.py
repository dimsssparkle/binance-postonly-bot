from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, field_validator, model_validator


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


class StrategyConfigCreatePayload(BaseModel):
    strategy_key: str
    name: str
    params: dict[str, Any] = {}


class StrategyConfigUpdatePayload(BaseModel):
    name: str
    params: dict[str, Any] = {}


class EnabledPayload(BaseModel):
    enabled: bool


class BacktestRunPayload(BaseModel):
    # либо сохранённый конфиг (config_id), либо ad-hoc (strategy_key+params) —
    # ровно один из двух наборов должен быть задан.
    config_id: Optional[int] = None
    strategy_key: Optional[str] = None
    params: Optional[dict[str, Any]] = None

    symbol: str = "ETHUSDT"
    days: int = 180
    entry_tf: Optional[str] = None
    exit_tf: str = "1m"
    tp_pct: float = 0.0023
    sl_pct: float = 0.0015
    exit_mode: str = "fixed"

    @model_validator(mode="after")
    def validate_exactly_one_source(self):
        has_config = self.config_id is not None
        has_adhoc = self.strategy_key is not None
        if has_config == has_adhoc:
            raise ValueError("укажите ровно одно из: config_id ИЛИ strategy_key")
        return self


class StrategyPreviewPayload(BaseModel):
    strategy_key: str
    params: dict[str, Any] = {}
    symbol: str = "ETHUSDT"
    limit: int = 300
