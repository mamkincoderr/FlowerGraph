import numpy as np
from threading import Lock


class RingBuffer:
    """Кольцевой буфер для хранения временных рядов по N каналам."""

    def __init__(self, n_channels: int, capacity: int):
        self._n = n_channels
        self._cap = capacity
        self._t = np.empty(capacity, dtype=np.float64)
        self._v = np.empty((capacity, n_channels), dtype=np.float32)
        self._head = 0
        self._size = 0
        self._lock = Lock()

    def push(self, times: np.ndarray, values: np.ndarray):
        n = len(times)
        if n == 0:
            return
        if n > self._cap:
            times = times[-self._cap:]
            values = values[-self._cap:]
            n = self._cap

        with self._lock:
            end = self._head + n
            if end <= self._cap:
                self._t[self._head:end] = times
                self._v[self._head:end] = values
            else:
                split = self._cap - self._head
                self._t[self._head:] = times[:split]
                self._v[self._head:] = values[:split]
                tail = end - self._cap
                self._t[:tail] = times[split:]
                self._v[:tail] = values[split:]
            self._head = end % self._cap
            self._size = min(self._size + n, self._cap)

    def get_last(self, n: int) -> tuple[np.ndarray, np.ndarray]:
        """Вернуть последние n отсчётов в хронологическом порядке (без копирования всего буфера)."""
        with self._lock:
            actual_n = min(n, self._size)
            if actual_n == 0:
                return (np.empty(0, dtype=np.float64),
                        np.empty((0, self._n), dtype=np.float32))

            end   = self._head                        # исключительный конец
            start = (end - actual_n) % self._cap

            if self._size < self._cap or start < end:
                return self._t[start:end].copy(), self._v[start:end].copy()
            else:
                t = np.concatenate([self._t[start:], self._t[:end]])
                v = np.concatenate([self._v[start:], self._v[:end]])
                return t, v

    def get(self) -> tuple[np.ndarray, np.ndarray]:
        with self._lock:
            if self._size == 0:
                return (np.empty(0, dtype=np.float64),
                        np.empty((0, self._n), dtype=np.float32))
            if self._size < self._cap:
                return self._t[:self._size].copy(), self._v[:self._size].copy()
            idx = np.roll(np.arange(self._cap), -self._head)
            return self._t[idx].copy(), self._v[idx].copy()

    def clear(self):
        with self._lock:
            self._head = 0
            self._size = 0

    @property
    def size(self) -> int:
        with self._lock:
            return self._size

    @property
    def n_channels(self) -> int:
        return self._n
