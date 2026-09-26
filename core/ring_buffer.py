import numpy as np
from threading import Lock


def _lod_view(t: np.ndarray, v: np.ndarray, max_pts: int):
    """Min/max по уже непрерывному куску. Вид не копируется целиком."""
    n = len(t)
    if n <= max_pts or n < 2:
        return t.copy(), v.copy()
    n_bins = max(1, max_pts // 2)
    step = int(np.ceil(n / n_bins))
    if step < 2:
        return t.copy(), v.copy()
    n_bins = int(np.ceil(n / step))
    if n_bins * 2 > max_pts:
        n_bins = max_pts // 2
        step = int(np.ceil(n / n_bins))
    pad = n_bins * step - n
    if pad > 0:
        t = np.concatenate([t, np.repeat(t[-1:], pad)])
        v = np.concatenate([v, np.repeat(v[-1:], pad, axis=0)])
    col0 = np.ascontiguousarray(v[:n_bins * step, 0]).reshape(n_bins, step)
    imin = col0.argmin(axis=1)
    imax = col0.argmax(axis=1)
    base = np.arange(n_bins, dtype=np.intp) * step
    idx_min = base + imin
    idx_max = base + imax
    mask = imin <= imax
    t_a = np.where(mask, t[idx_min], t[idx_max])
    t_b = np.where(mask, t[idx_max], t[idx_min])
    v_a = np.where(mask[:, None], v[idx_min], v[idx_max])
    v_b = np.where(mask[:, None], v[idx_max], v[idx_min])
    t_out = np.empty(2 * n_bins, dtype=np.float64)
    t_out[0::2] = t_a
    t_out[1::2] = t_b
    v_out = np.empty((2 * n_bins, v.shape[1]), dtype=np.float32)
    v_out[0::2] = v_a
    v_out[1::2] = v_b
    return t_out, v_out


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

    def grow(self, new_cap: int) -> None:
        """Увеличить ёмкость, не выбрасывая уже накопленные отсчёты."""
        if new_cap <= self._cap:
            return
        with self._lock:
            t_new = np.empty(new_cap, dtype=np.float64)
            v_new = np.empty((new_cap, self._n), dtype=np.float32)
            n = self._size
            if n:
                if n < self._cap:
                    t_new[:n] = self._t[:n]
                    v_new[:n] = self._v[:n]
                else:
                    first = self._cap - self._head
                    t_new[:first] = self._t[self._head:]
                    t_new[first:n] = self._t[:self._head]
                    v_new[:first] = self._v[self._head:]
                    v_new[first:n] = self._v[:self._head]
            self._t = t_new
            self._v = v_new
            self._cap = new_cap
            self._head = n % new_cap

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

    def get_span(self, t_lo: float, t_hi: float, max_points: int | None = None
                 ) -> tuple[np.ndarray, np.ndarray]:
        """Видимый кусок кольца. Копирует не больше max_points точек, без снимка всего буфера."""
        with self._lock:
            n = self._size
            if n < 2:
                return (np.empty(0, dtype=np.float64),
                        np.empty((0, self._n), dtype=np.float32))
            cap = self._cap
            base = self._head if n == cap else 0
            if t_lo > t_hi:
                t_lo, t_hi = t_hi, t_lo

            def t_at(logical: int) -> float:
                return float(self._t[(base + logical) % cap])

            lo, hi = 0, n
            while lo < hi:
                mid = (lo + hi) // 2
                if t_at(mid) < t_lo:
                    lo = mid + 1
                else:
                    hi = mid
            i0 = max(0, lo - 1)
            lo, hi = i0, n
            while lo < hi:
                mid = (lo + hi) // 2
                if t_at(mid) <= t_hi:
                    lo = mid + 1
                else:
                    hi = mid
            i1 = min(n, lo + 1)
            count = i1 - i0
            if count <= 0:
                return (np.empty(0, dtype=np.float64),
                        np.empty((0, self._n), dtype=np.float32))
            step = 1
            if max_points is not None and count > max_points:
                step = int(np.ceil(count / max_points))
            logical = np.arange(i0, i1, step)
            idx = (base + logical) % cap
            return self._t[idx].copy(), self._v[idx].copy()

    def get_decimated(self, max_points: int) -> tuple[np.ndarray, np.ndarray]:
        """Равномерная выборка всего кольца, не больше max_points."""
        with self._lock:
            n = self._size
            if n < 2:
                return (np.empty(0, dtype=np.float64),
                        np.empty((0, self._n), dtype=np.float32))
            t0 = float(self._t[(self._head if n == self._cap else 0)])
            last = (self._head - 1) % self._cap
            t1 = float(self._t[last])
        return self.get_span(t0, t1, max_points)

    def time_bounds(self) -> tuple[float, float] | None:
        with self._lock:
            if self._size < 2:
                return None
            base = self._head if self._size == self._cap else 0
            t0 = float(self._t[base])
            t1 = float(self._t[(self._head - 1) % self._cap])
            return t0, t1

    def get_window_lod(self, t_lo: float, t_hi: float, max_points: int
                       ) -> tuple[np.ndarray, np.ndarray]:
        """Произвольный интервал, min/max до max_points. Для навигации во время записи."""
        with self._lock:
            n = self._size
            if n < 2:
                return (np.empty(0, dtype=np.float64),
                        np.empty((0, self._n), dtype=np.float32))
            cap = self._cap
            base = self._head if n == cap else 0
            if t_lo > t_hi:
                t_lo, t_hi = t_hi, t_lo

            def t_at(logical: int) -> float:
                return float(self._t[(base + logical) % cap])

            lo, hi = 0, n
            while lo < hi:
                mid = (lo + hi) // 2
                if t_at(mid) < t_lo:
                    lo = mid + 1
                else:
                    hi = mid
            i0 = max(0, lo - 1)
            lo, hi = i0, n
            while lo < hi:
                mid = (lo + hi) // 2
                if t_at(mid) <= t_hi:
                    lo = mid + 1
                else:
                    hi = mid
            i1 = min(n, max(i0 + 1, lo))
            return self._lod_logical(base, i0, i1, max_points)

    def get_recent_lod(self, duration: float, max_points: int
                       ) -> tuple[np.ndarray, np.ndarray]:
        """Последние `duration` секунд, уже прореженные min/max до max_points.

        Не копирует всё окно: экстремумы считаются по видам кольца.
        """
        with self._lock:
            n = self._size
            if n < 2:
                return (np.empty(0, dtype=np.float64),
                        np.empty((0, self._n), dtype=np.float32))
            cap = self._cap
            base = self._head if n == cap else 0
            t1 = float(self._t[(self._head - 1) % cap])
            t0 = t1 - float(duration)

            def t_at(logical: int) -> float:
                return float(self._t[(base + logical) % cap])

            lo, hi = 0, n
            while lo < hi:
                mid = (lo + hi) // 2
                if t_at(mid) < t0:
                    lo = mid + 1
                else:
                    hi = mid
            i0 = max(0, lo - 1)
            return self._lod_logical(base, i0, n, max_points)

    def _lod_logical(self, base: int, i0: int, i1: int, max_points: int):
        count = i1 - i0
        if count <= 0:
            return (np.empty(0, dtype=np.float64),
                    np.empty((0, self._n), dtype=np.float32))
        cap = self._cap
        seam = cap - base if base else cap
        if i0 >= seam or i1 <= seam or base == 0 and self._size < cap:
            return _lod_view(*self._phys_slice(base, i0, i1), max_points)
        left_n = seam - i0
        right_n = i1 - seam
        left_pts = max(2, int(max_points * left_n / count))
        right_pts = max(2, max_points - left_pts)
        t_l, v_l = _lod_view(*self._phys_slice(base, i0, seam), left_pts)
        t_r, v_r = _lod_view(*self._phys_slice(base, seam, i1), right_pts)
        if len(t_l) == 0:
            return t_r, v_r
        if len(t_r) == 0:
            return t_l, v_l
        return np.concatenate([t_l, t_r]), np.concatenate([v_l, v_r])

    def _phys_slice(self, base: int, i0: int, i1: int):
        a = (base + i0) % self._cap
        b = (base + i1 - 1) % self._cap + 1
        return self._t[a:b], self._v[a:b]

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
