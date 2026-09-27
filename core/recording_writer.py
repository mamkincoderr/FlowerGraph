"""Background writer for streaming acquisition data."""

from __future__ import annotations

import queue
import threading
import time

import numpy as np


class RecordingWriter:
    """Write queued sample batches without blocking the acquisition or GUI thread."""

    _STOP = object()
    _MAX_BATCH_ITEMS = 1024
    _BATCH_WAIT_SEC = 0.01

    def __init__(self, times_file, values_file):
        self._times_file = times_file
        self._values_file = values_file
        self._queue: queue.Queue = queue.Queue()
        self._error_lock = threading.Lock()
        self._error: str | None = None
        self._thread = threading.Thread(
            target=self._run, name='FlowerGraphRecorder', daemon=True
        )
        self._thread.start()

    @property
    def error(self) -> str | None:
        with self._error_lock:
            return self._error

    def submit(self, times: np.ndarray, values: np.ndarray) -> bool:
        if self.error is not None:
            return False
        self._queue.put((times, values))
        return True

    def flush(self, *, wait: bool = False) -> bool:
        if not self._thread.is_alive():
            return self.error is None
        done = threading.Event()
        self._queue.put(('flush', done))
        if wait:
            if not done.wait(timeout=2.0):
                return False
            return done.is_set() and self.error is None
        return self.error is None

    def close(self) -> str | None:
        if self._thread.is_alive():
            self._queue.put(self._STOP)
            self._thread.join()
        return self.error

    def _set_error(self, exc: Exception) -> None:
        with self._error_lock:
            if self._error is None:
                self._error = f'{type(exc).__name__}: {exc}'

    def _write_batch(self, items: list[tuple[np.ndarray, np.ndarray]]) -> None:
        if len(items) == 1:
            times, values = items[0]
        else:
            times = np.concatenate([item[0] for item in items])
            values = np.concatenate([item[1] for item in items], axis=0)
        times.astype(np.float64, copy=False).tofile(self._times_file)
        values.astype(np.float32, copy=False).tofile(self._values_file)

    def _flush_files(self) -> None:
        self._times_file.flush()
        self._values_file.flush()

    @staticmethod
    def _is_flush(item) -> bool:
        return (
            isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], str)
            and item[0] == 'flush'
        )

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is self._STOP:
                try:
                    self._flush_files()
                except Exception as exc:
                    self._set_error(exc)
                return
            if self._is_flush(item):
                try:
                    self._flush_files()
                except Exception as exc:
                    self._set_error(exc)
                item[1].set()
                continue

            batch = [item]
            deadline = time.monotonic() + self._BATCH_WAIT_SEC
            control = None
            while len(batch) < self._MAX_BATCH_ITEMS:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    next_item = self._queue.get(timeout=remaining)
                except queue.Empty:
                    break
                if next_item is self._STOP or self._is_flush(next_item):
                    control = next_item
                    break
                batch.append(next_item)

            try:
                self._write_batch(batch)
                if control is not None:
                    self._flush_files()
            except Exception as exc:
                self._set_error(exc)
                if self._is_flush(control):
                    control[1].set()
                return

            if self._is_flush(control):
                control[1].set()
            elif control is self._STOP:
                return
