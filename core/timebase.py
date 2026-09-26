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


def lod_decimate(t: np.ndarray, v: np.ndarray, max_pts: int):
    """Min/max LOD. Хвост окна не отбрасывается — иначе кривая обрывается косым срезом."""
    n = len(t)
    if n <= max_pts or n < 2:
        return t, v
    n_bins = max(1, max_pts // 2)
    step = int(np.ceil(n / n_bins))
    if step < 2:
        return t, v
    n_bins = int(np.ceil(n / step))
    if n_bins * 2 > max_pts:
        n_bins = max_pts // 2
        step = int(np.ceil(n / n_bins))
    pad = n_bins * step - n
    if pad > 0:
        t = np.concatenate([t, np.repeat(t[-1:], pad)])
        v = np.concatenate([v, np.repeat(v[-1:], pad, axis=0)])
    v0 = np.ascontiguousarray(v[:n_bins * step, 0]).reshape(n_bins, step)
    imin = v0.argmin(axis=1)
    imax = v0.argmax(axis=1)
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
