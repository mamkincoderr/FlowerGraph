from abc import ABC, abstractmethod
from typing import Callable
import queue

import numpy as np
from core.timebase import mark_time_gaps

DataCallback  = Callable[[np.ndarray, np.ndarray], None]
ErrorCallback = Callable[[str], None]
RecordCallback = Callable[[np.ndarray, np.ndarray], None]

_QMAX = 512


def put_drop_oldest(q: queue.Queue, item) -> bool:
    dropped = False
    try:
        q.put_nowait(item)
    except queue.Full:
        dropped = True
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(item)
        except queue.Full:
            pass
    return dropped


class BaseSource(ABC):
    """
    Источник данных. Воркер кладёт семплы в очередь;
    QTimer в GUI-потоке вызывает _drain_queue → _emit.
    Ошибки тоже через очередь — не трогать Qt из serial-потока.
    """

    def __init__(self):
        self._data_cb:  DataCallback  | None = None
        self._record_cb: RecordCallback | None = None
        self._error_cb: ErrorCallback | None = None
        self._running = False
        self._err_q: queue.Queue = queue.Queue()
        self._last_gui_time: float | None = None

    def set_data_callback(self, cb: DataCallback):
        self._data_cb = cb

    def set_record_callback(self, cb: RecordCallback | None):
        """Set the lossless recording sink, called by the acquisition thread."""
        self._record_cb = cb

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

    def _record(self, times: np.ndarray, values: np.ndarray):
        if self._record_cb:
            try:
                self._record_cb(times, values)
            except Exception as exc:
                self._emit_error(f'Ошибка записи потока: {exc}')

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

    def _drain_data_queue(self, data_queue: queue.Queue):
        """Emit a bounded snapshot of queued samples as one callback.

        COBS sources enqueue one NumPy array per packet. Emitting every packet
        separately made the GUI perform file I/O and plotting bookkeeping at
        packet rate. Snapshotting qsize keeps each timer tick bounded even if
        the producer continues to enqueue while the queue is being drained.
        """
        count = data_queue.qsize()
        if count <= 0:
            return

        times_parts = []
        values_parts = []
        for _ in range(count):
            try:
                times, values = data_queue.get_nowait()
            except queue.Empty:
                break
            times_parts.append(times)
            values_parts.append(values)

        if not times_parts:
            return
        if len(times_parts) == 1:
            times, values = times_parts[0], values_parts[0]
        else:
            times = np.concatenate(times_parts)
            values = np.concatenate(values_parts, axis=0)
        try:
            rate = self.effective_sample_rate()
        except (AttributeError, TypeError, ValueError):
            rate = 0
        times, values, self._last_gui_time = mark_time_gaps(
            times, values, rate, self._last_gui_time
        )
        self._emit(times, values)
