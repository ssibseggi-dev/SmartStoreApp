"""API 호출 등 오래 걸리는 작업을 백그라운드 스레드에서 실행하고 결과를 UI 스레드로 전달한다."""
from __future__ import annotations

import logging
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

log = logging.getLogger(__name__)


class _Signals(QObject):
    finished = Signal(object)
    failed = Signal(object)
    progress = Signal(object)


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    def run(self) -> None:
        try:
            result = self.fn()
        except BaseException as e:  # UI에 그대로 전달
            invalid = getattr(e, "invalid_inputs", None)
            log.exception("백그라운드 작업 실패%s", f" invalidInputs={invalid}" if invalid else "")
            self._emit(self.signals.failed, e if isinstance(e, Exception) else RuntimeError(repr(e)))
        else:
            log.debug("백그라운드 작업 완료")
            self._emit(self.signals.finished, result)

    @staticmethod
    def _emit(signal, value) -> None:
        try:
            signal.emit(value)
        except RuntimeError:   # 작업이 끝나기 전에 앱(창)이 닫혀 받을 곳이 없음
            log.debug("앱 종료 후 끝난 백그라운드 작업 결과는 버립니다.")


_active: set[_Task] = set()


def run_async(fn: Callable[[], Any], on_done: Callable[[Any], None],
              on_error: Callable[[Exception], None]) -> None:
    _start(_Task(fn), on_done, on_error)


def run_with_progress(fn: Callable[[Callable[[Any], None]], Any], on_progress: Callable[[Any], None],
                      on_done: Callable[[Any], None], on_error: Callable[[Exception], None]) -> None:
    """fn은 진행 상황 보고 함수(report)를 인자로 받는다. report(x)는 UI 스레드에서 on_progress(x)로 전달된다."""
    task = _Task(lambda: fn(task.signals.progress.emit))
    task.signals.progress.connect(on_progress)
    _start(task, on_done, on_error)


def _start(task: _Task, on_done: Callable[[Any], None], on_error: Callable[[Exception], None]) -> None:
    task.setAutoDelete(False)
    _active.add(task)  # 시그널 객체가 작업 종료 전 GC되지 않도록 보관

    def done(result):
        _active.discard(task)
        on_done(result)

    def error(exc):
        _active.discard(task)
        on_error(exc)

    task.signals.finished.connect(done)
    task.signals.failed.connect(error)
    QThreadPool.globalInstance().start(task)
