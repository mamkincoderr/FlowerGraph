"""Read bounded plot windows from the temporary files used by live recording."""

from __future__ import annotations

import math
import os

import numpy as np


def read_recording_window(
    times_path: str,
    values_path: str,
    n_channels: int,
    t_lo: float,
    t_hi: float,
    max_points: int,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Read a chronological, bounded sample of ``[t_lo, t_hi]`` from disk.

    The live writer appends raw float64 timestamps and interleaved float32
    channel values. Memory maps keep large recordings out of process RAM; only
    the selected display window is copied into ordinary arrays.
    """
    n_channels = int(n_channels)
    max_points = max(2, int(max_points))
    if n_channels <= 0 or max_points <= 0:
        return None
    try:
        time_count = os.path.getsize(times_path) // np.dtype(np.float64).itemsize
        value_count = (os.path.getsize(values_path)
                       // (np.dtype(np.float32).itemsize * n_channels))
        sample_count = min(time_count, value_count)
        if sample_count <= 0:
            return None

        times_map = np.memmap(
            times_path, dtype=np.float64, mode='r', shape=(sample_count,)
        )
        values_map = np.memmap(
            values_path, dtype=np.float32, mode='r',
            shape=(sample_count, n_channels),
        )
        lo = min(float(t_lo), float(t_hi))
        hi = max(float(t_lo), float(t_hi))
        first = max(0, int(np.searchsorted(times_map, lo, side='left')) - 1)
        stop = min(sample_count,
                   int(np.searchsorted(times_map, hi, side='right')) + 1)
        count = stop - first
        if count <= 0:
            del values_map, times_map
            return None

        if count <= max_points:
            indices = np.arange(first, stop, dtype=np.intp)
        else:
            step = max(1, int(math.ceil((count - 1) / (max_points - 1))))
            indices = np.arange(first, stop, step, dtype=np.intp)
            if indices[-1] != stop - 1:
                indices = np.append(indices, stop - 1)
        result_times = np.asarray(times_map[indices], dtype=np.float64).copy()
        result_values = np.asarray(values_map[indices], dtype=np.float32).copy()
        del values_map, times_map
        return result_times, result_values
    except (OSError, ValueError, IndexError):
        return None
