class EngineBusyError(Exception):
    """Уже есть активный Intent по этому символу — новый сигнал отклонён."""


class EngineFailure(Exception):
    """Неустранимая ошибка при исполнении Intent — переход в состояние FAILED."""
