"""Состояние одного канала — единственная модель для панелей и графика."""

from dataclasses import dataclass


@dataclass
class ChannelState:
    index: int
    name: str
    unit: str = ''
    color: str = '#888888'
    visible: bool = True
    y_per_div: float = 1.0
    offset: float = 0.0
    calib_a: float = 1.0
    calib_b: float = 0.0
