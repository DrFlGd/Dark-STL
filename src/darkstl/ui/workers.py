"""Background jobs so parsing, remeshing and exporting never freeze the window."""

from __future__ import annotations

import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class JobSignals(QObject):
    progress = Signal(float, str)
    finished = Signal(object)
    failed = Signal(str)


class Job(QRunnable):
    """Runs ``fn(progress)`` on the thread pool; ``progress(frac, message)`` is thread-safe."""

    def __init__(self, fn: Callable[[Callable[[float, str], None]], Any]):
        super().__init__()
        self.fn = fn
        self.signals = JobSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            result = self.fn(lambda f, m: self.signals.progress.emit(float(f), str(m)))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            self.signals.failed.emit(detail)
            traceback.print_exc()
            return
        self.signals.finished.emit(result)


def start(fn, on_done=None, on_error=None, on_progress=None) -> Job:
    job = Job(fn)
    if on_done:
        job.signals.finished.connect(on_done)
    if on_error:
        job.signals.failed.connect(on_error)
    if on_progress:
        job.signals.progress.connect(on_progress)
    QThreadPool.globalInstance().start(job)
    return job
