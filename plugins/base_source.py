from abc import ABC, abstractmethod
from typing import Callable
import queue

import numpy as np

DataCallback  = Callable[[np.ndarray, np.ndarray], None]
ErrorCallback = Callable[[str], None]

_QMAX = 512


def put_drop_oldest(q: queue.Queue, item) -> None:
    try:
        q.put_nowait(item)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(item)
        except queue.Full:
            pass


class BaseSource(ABC):
    """
    Источник данных. Воркер кладёт семплы в очередь;
    QTimer в GUI-потоке вызывает _drain_queue → _emit.
    Ошибки тоже через очередь — не трогать Qt из serial-потока.
    """

    def __init__(self):
        self._data_cb:  DataCallback  | None = None
        self._error_cb: ErrorCallback | None = None
        self._running = False
        self._err_q: queue.Queue = queue.Queue()

    def set_data_callback(self, cb: DataCallback):
        self._data_cb = cb

    def set_error_callback(self, cb: ErrorCallback):
        self._error_cb = cb

    @property
    def is_running(self) -> bool:
        return self._running

    @abstractmethod
    def get_name(self) -> str:
        ...

    @abstractmethod
    def get_channel_count(self) -> int:
        ...

    @abstractmethod
    def get_channel_names(self) -> list[str]:
        ...

    @abstractmethod
    def start(self) -> bool:
        """Начать приём. True — поток запущен."""

    @abstractmethod
    def stop(self):
        ...

    @abstractmethod
    def get_config_widget(self):
        ...

    def _emit(self, times: np.ndarray, values: np.ndarray):
        if self._data_cb:
            self._data_cb(times, values)

    def _emit_error(self, message: str):
        self._err_q.put(message)

    def _drain_errors(self):
        while True:
            try:
                msg = self._err_q.get_nowait()
            except queue.Empty:
                break
            if self._error_cb:
                self._error_cb(msg)
