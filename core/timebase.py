"""Дискретная шкала времени и прореживание кривой. Без Qt, чтобы тесты шли в CI."""

import numpy as np

TIME_DIV_SEQ: list[float] = [
    1e-6, 2e-6, 5e-6,
    1e-5, 2e-5, 5e-5,
    1e-4, 2e-4, 5e-4,
    1e-3, 2e-3, 5e-3,
    1e-2, 2e-2, 5e-2,
    0.1,  0.2,  0.5,
    1.0,  2.0,  5.0,  10.0, 20.0, 30.0, 60.0,
    120.0, 300.0, 600.0, 1200.0, 1800.0, 3600.0,
]
N_DIV = 10
DEFAULT_IDX = 12  # 10 мс/дел → окно 100 мс

Y_DIV_SEQ: list[float] = [
    1000.0, 500.0, 200.0, 100.0, 50.0, 20.0, 10.0,
    5.0, 2.0, 1.0, 0.5, 0.2, 0.1,
    0.05, 0.02, 0.01, 0.005, 0.002, 0.001,
]
Y_DIV_DEFAULT_IDX = 9


def max_div_idx_for_span(span: float) -> int:
    """Самая крупная клетка, при которой окно из 10 клеток ещё не шире данных."""
    limit = 0
    for i, div in enumerate(TIME_DIV_SEQ):
        if div * N_DIV <= max(span, 0.0) * 1.02:
            limit = i
        else:
            break
    return limit


def time_div_idx_for_span(span: float) -> int:
    """Ближайший 1-2-5, при котором окно (N_DIV делений) покрывает span."""
    if span <= 0:
        return DEFAULT_IDX
    needed = span / N_DIV
    for i, t in enumerate(TIME_DIV_SEQ):
        if t >= needed:
            return i
    return len(TIME_DIV_SEQ) - 1


def fmt_y_div(v: float) -> str:
    if v >= 1000:
        return f'{v / 1000:g}k'
    if v >= 1:
        return f'{v:g}'
    if v >= 0.001:
        return f'{v * 1000:g}m'
    return f'{v:g}'


def fmt_time_div(t: float) -> str:
    if t < 1e-3:
        return f'{t * 1e6:g} мкс/дел'
    if t < 1.0:
        return f'{t * 1e3:g} мс/дел'
    if t < 60.0:
        return f'{t:g} с/дел'
    if t < 3600.0:
        return f'{t / 60:g} мин/дел'
    return f'{t / 3600:g} ч/дел'



def lod_indices(v: np.ndarray, max_pts: int) -> np.ndarray:
    """Select chronological LOD indices while retaining each channel's extrema."""
    n = len(v)
    if n == 0 or max_pts <= 0:
        return np.empty(0, dtype=np.intp)
    if n <= max_pts:
        return np.arange(n, dtype=np.intp)

    n_ch = v.shape[1] if v.ndim == 2 else 0
    if n_ch == 0 or max_pts <= 2 * n_ch + 2:
        return np.linspace(0, n - 1, max_pts, dtype=np.intp)

    n_bins = max(1, (max_pts - 2) // (2 * n_ch))
    step = int(np.ceil(n / n_bins))
    n_bins = int(np.ceil(n / step))
    pad = n_bins * step - n
    if pad:
        v = np.concatenate([v, np.repeat(v[-1:], pad, axis=0)])

    bins = np.ascontiguousarray(v[:n_bins * step]).reshape(n_bins, step, n_ch)
    base = np.arange(n_bins, dtype=np.intp)[:, None] * step
    idx_min = base + bins.argmin(axis=1)
    idx_max = base + bins.argmax(axis=1)
    indices = np.concatenate((idx_min.ravel(), idx_max.ravel(), [0, n - 1]))
    return np.unique(np.clip(indices, 0, n - 1))


def lod_decimate(t: np.ndarray, v: np.ndarray, max_pts: int):
    """Min/max LOD preserving every channel's extrema and both window endpoints."""
    idx = lod_indices(v, max_pts)
    if len(idx) == len(t) and (len(idx) == 0 or idx[-1] == len(t) - 1):
        return t, v
    return t[idx], v[idx]
