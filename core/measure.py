"""Чистые расчёты шкалы и статистики. Без Qt — их гоняет CI."""

import numpy as np


def index_range(times: np.ndarray, t0: float, t1: float) -> tuple[int, int]:
    """Полуинтервал [t0, t1] по searchsorted left/right, без соседних отсчётов."""
    if len(times) == 0:
        return 0, 0
    lo, hi = (t0, t1) if t0 <= t1 else (t1, t0)
    i0 = int(np.searchsorted(times, lo, side='left'))
    i1 = int(np.searchsorted(times, hi, side='right'))
    i0 = max(0, min(i0, len(times)))
    i1 = max(i0, min(i1, len(times)))
    return i0, i1


def scale_from_y_per_div(y_per_div: float) -> float:
    """Визуальный множитель при фиксированной сетке: меньше цена деления — крупнее сигнал."""
    return 1.0 / float(y_per_div)


def y_per_div_from_scale(scale: float) -> float:
    if scale == 0:
        return 1.0
    return 1.0 / float(scale)


def parse_y_text(text: str) -> float:
    """Разобрать цену деления: 1.5, 1,5, 10k, 2m, 1e-3, 1e-3m."""
    s = text.strip().lower().replace(' ', '').replace(',', '.')
    for suffix in ('/дел', '/д', 'дел'):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    mult = 1.0
    if s.endswith('k') and 'e' not in s:
        mult = 1e3
        s = s[:-1]
    elif s.endswith('m'):
        # «m» — приставка милли, в том числе после научной записи (1e-3m).
        s = s[:-1]
        if 'e' not in s:
            mult = 1e-3
    if not s:
        raise ValueError('empty')
    return float(s) * mult


def fmt_span(t: float) -> str:
    a = abs(t)
    if a < 1e-3:
        return f'{t * 1e6:.4g} мкс'
    if a < 1.0:
        return f'{t * 1e3:.4g} мс'
    if a < 60.0:
        return f'{t:.4g} с'
    if a < 3600.0:
        return f'{t / 60:.4g} мин'
    return f'{t / 3600:.4g} ч'


def fragment_stats(data: np.ndarray) -> dict | None:
    if data is None or len(data) == 0:
        return None
    x = np.asarray(data, dtype=np.float64)
    return {
        'mean': float(np.mean(x)),
        'std': float(np.std(x)),
        'rms': float(np.sqrt(np.mean(x * x))),
        'min': float(np.min(x)),
        'max': float(np.max(x)),
        'pp': float(np.max(x) - np.min(x)),
        'n': int(x.size),
    }
