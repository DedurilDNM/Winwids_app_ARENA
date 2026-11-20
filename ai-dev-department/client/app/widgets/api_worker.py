"""Run blocking ApiClient calls off the GUI thread.

Usage:
    run_in_thread(lambda: api.list_accounts(), on_ok, on_err)

`on_ok(result)` / `on_err(ApiError)` are always invoked in the GUI thread
(Qt queued signal delivery). The worker keeps itself alive until it finishes,
so callers do not need to store a reference. It is intentionally NOT
Qt-parented to the calling widget: closing a window while a request is in
flight must not destroy a running QThread (that aborts the process).
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QThread, Signal

from app.api_client import ApiError


class ApiWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(object)  # ApiError

    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
        except ApiError as exc:
            self.failed.emit(exc)
        except Exception as exc:  # unexpected bug: surface it instead of killing the thread silently
            self.failed.emit(ApiError(-1, f"{exc.__class__.__name__}: {exc}"))
        else:
            self.succeeded.emit(result)


_running: set[ApiWorker] = set()


def run_in_thread(
    fn: Callable[[], Any],
    on_ok: Callable[[Any], None] | None = None,
    on_err: Callable[[ApiError], None] | None = None,
) -> ApiWorker:
    worker = ApiWorker(fn)
    if on_ok is not None:
        worker.succeeded.connect(on_ok)
    if on_err is not None:
        worker.failed.connect(on_err)

    def _cleanup() -> None:
        _running.discard(worker)
        worker.deleteLater()

    worker.finished.connect(_cleanup)
    _running.add(worker)
    worker.start()
    return worker
