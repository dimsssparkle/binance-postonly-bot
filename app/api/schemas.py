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
