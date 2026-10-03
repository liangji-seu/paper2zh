"""Serialize PyMuPDF work onto one lazily started daemon thread."""

from __future__ import annotations

import functools
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable, TypeVar


T = TypeVar("T")


@dataclass
class _Call:
    function: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    done: threading.Event
    result: Any = None
    error: BaseException | None = None
    traceback: Any = None


_calls: queue.Queue[_Call] = queue.Queue()
_state_lock = threading.Lock()
_ready = threading.Event()
_worker: threading.Thread | None = None
_worker_ident: int | None = None


def _run() -> None:
    global _worker_ident
    _worker_ident = threading.get_ident()
    _ready.set()
    while True:
        call = _calls.get()
        try:
            call.result = call.function(*call.args, **call.kwargs)
        except BaseException as error:  # return all callable failures to the caller
            call.error = error
            call.traceback = error.__traceback__
        finally:
            call.done.set()
            _calls.task_done()


def _ensure_worker() -> None:
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _state_lock:
        if _worker is None or not _worker.is_alive():
            _ready.clear()
            _worker = threading.Thread(target=_run, name="paper2zh-pdf-worker", daemon=True)
            _worker.start()
    _ready.wait()


def pdf_serialized(function: Callable[..., T]) -> Callable[..., T]:
    """Run a PDF operation on the dedicated thread and synchronously return it.

    Calls originating from the PDF worker execute directly so decorated helper
    functions can safely call one another without waiting on themselves.
    """

    @functools.wraps(function)
    def invoke(*args: Any, **kwargs: Any) -> T:
        if threading.get_ident() == _worker_ident:
            return function(*args, **kwargs)
        _ensure_worker()
        call = _Call(function=function, args=args, kwargs=kwargs, done=threading.Event())
        _calls.put(call)
        call.done.wait()
        if call.error is not None:
            raise call.error.with_traceback(call.traceback)
        return call.result

    return invoke


__all__ = ["pdf_serialized"]
