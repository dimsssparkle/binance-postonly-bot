"""Декларативная схема параметров стратегии — чтобы дашборд мог сам
нарисовать форму настроек и провалидировать ввод, не зная заранее о
конкретных стратегиях. Явные ручные ParamSpec, а не интроспекция
__init__ (type hints) — потому что типовые метаданные вроде "tf это
выбор из списка" или "min/max/русский label" в аннотациях не выразить.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Sequence


class ParamType(str, Enum):
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    ENUM = "enum"
    STRATEGY_REF = "strategy_ref"  # id другого strategy_configs — см. registry.py


@dataclass(frozen=True)
class ParamSpec:
    name: str                          # должно совпадать с именем kwarg в __init__ стратегии
    type: ParamType
    default: Any
    label: str
    min: Optional[float] = None        # только для INT/FLOAT
    max: Optional[float] = None
    choices: Optional[Sequence[str]] = None  # только для ENUM

    def validate(self, raw: Any) -> Any:
        if self.type == ParamType.INT:
            v = int(raw)
            if self.min is not None and v < self.min:
                raise ValueError(f"{self.name}: {v} < min {self.min}")
            if self.max is not None and v > self.max:
                raise ValueError(f"{self.name}: {v} > max {self.max}")
            return v
        if self.type == ParamType.FLOAT:
            v = float(raw)
            if self.min is not None and v < self.min:
                raise ValueError(f"{self.name}: {v} < min {self.min}")
            if self.max is not None and v > self.max:
                raise ValueError(f"{self.name}: {v} > max {self.max}")
            return v
        if self.type == ParamType.BOOL:
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str):
                if raw.lower() in ("true", "1"):
                    return True
                if raw.lower() in ("false", "0"):
                    return False
                raise ValueError(f"{self.name}: не булево значение: {raw!r}")
            return bool(raw)
        if self.type == ParamType.ENUM:
            v = str(raw)
            if self.choices is not None and v not in self.choices:
                raise ValueError(f"{self.name}: {v!r} не входит в {self.choices}")
            return v
        if self.type == ParamType.STRATEGY_REF:
            # Только структурная проверка (это id другого strategy_configs) —
            # существование записи, отсутствие циклов (не ссылка на другой
            # regime_router) и совпадение tf проверяются на уровне API
            # (routes_strategies.py), где есть доступ к репозиторию.
            return int(raw)
        raise ValueError(f"неизвестный ParamType: {self.type}")


def validate_params(specs: list[ParamSpec], raw: dict) -> dict:
    """Валидирует raw (из JSON API-запроса) против specs: неизвестные ключи
    отклоняются, отсутствующие берут default, каждое присутствующее значение
    проходит через ParamSpec.validate(). Возвращает чистый dict, готовый
    передать как **kwargs в конструктор стратегии."""
    known = {s.name for s in specs}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"неизвестные параметры: {sorted(unknown)}")
    out: dict[str, Any] = {}
    for s in specs:
        out[s.name] = s.validate(raw[s.name]) if s.name in raw else s.default
    return out
