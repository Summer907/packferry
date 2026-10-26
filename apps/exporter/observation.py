"""Optional thread-local task observations; no resolver or Bundle semantics."""
from contextvars import ContextVar
from typing import Callable

Observer = Callable[[str, str], None]
observer: ContextVar[Observer | None] = ContextVar('export_observer', default=None)


def observe(stage: str, operation: str) -> None:
    callback = observer.get()
    if callback is not None:
        callback(stage, operation)
