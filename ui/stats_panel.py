"""
StatsPanel — панель статистики по выделенному сегменту.

Показывает для каждого канала: среднее, СКО, максимум, минимум.
"""

import math
import numpy as np
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QFrame, QScrollArea
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont


class StatsPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(60)
        self.setMaximumHeight(220)
        self._build_ui()
        self._rows: list[_StatRow] = []

    def _build_ui(self):
        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        hdr = QLabel('  Статистика фрагмента')
        hdr.setStyleSheet(
            'background:#e8e8e8; font-weight:bold; padding:3px 4px;'
            'border-top:1px solid #ccc; font-size:11px;'
        )
        vl.addWidget(hdr)

        # Заголовок колонок
        col_hdr = QWidget()
        ch = QHBoxLayout(col_hdr)
        ch.setContentsMargins(4, 1, 4, 1)
        ch.setSpacing(0)
        for txt, w in [('', 16), ('Кан.', 36), ('Ср.', 62), ('СКО', 62), ('Мин', 55), ('Макс', 55)]:
            l = QLabel(txt)
            l.setFixedWidth(w)
            l.setStyleSheet('font-size:9px; color:#666;')
            l.setAlignment(Qt.AlignCenter)
            ch.addWidget(l)
        col_hdr.setStyleSheet('background:#f4f4f4;')
        vl.addWidget(col_hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet('color:#ddd;')
        vl.addWidget(sep)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.NoFrame)
        vl.addWidget(self._scroll)

        self._lbl_empty = QLabel('  Нет выделения')
        self._lbl_empty.setStyleSheet('color:#aaa; font-size:10px; padding:4px;')
        self._scroll.setWidget(self._lbl_empty)

    def setup(self, names: list[str], colors: list[str]):
        self._rows.clear()
        container = QWidget()
        vl = QVBoxLayout(container)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)
        for i, (name, color) in enumerate(zip(names, colors)):
            r = _StatRow(i, name, color)
            vl.addWidget(r)
            self._rows.append(r)
        vl.addStretch()
        self._scroll.setWidget(container)

    def update_stats(self, channel_data: list[np.ndarray | None], units: list[str]):
        """
        channel_data: список из np.ndarray (1D) или None для каждого канала.
        units:        список единиц измерения.
        """
        for i, row in enumerate(self._rows):
            u = units[i] if i < len(units) else ''
            if i < len(channel_data) and channel_data[i] is not None:
                row.update(channel_data[i], u)
            else:
                row.clear()

    def clear(self):
        for r in self._rows:
            r.clear()


class _StatRow(QWidget):
    def __init__(self, idx: int, name: str, color: str, parent=None):
        super().__init__(parent)
        self.setFixedHeight(20)
        lo = QHBoxLayout(self)
        lo.setContentsMargins(4, 0, 4, 0)
        lo.setSpacing(0)

        dot = QLabel('▌')
        dot.setStyleSheet(f'color:{color}; font-size:12px;')
        dot.setFixedWidth(16)
        lo.addWidget(dot)

        lbl = QLabel(name[:5])
        lbl.setFixedWidth(36)
        lbl.setStyleSheet('font-size:10px;')
        lo.addWidget(lbl)

        mono = QFont('Courier New', 8)
        self._lbls = []
        for w in (62, 62, 55, 55):
            l = QLabel('—')
            l.setFixedWidth(w)
            l.setFont(mono)
            l.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            l.setStyleSheet('font-size:9px; padding-right:3px;')
            lo.addWidget(l)
            self._lbls.append(l)

    def update(self, data: np.ndarray, unit: str):
        if len(data) == 0:
            self.clear()
            return
        mean = float(np.mean(data))
        rms  = float(np.sqrt(np.mean(data ** 2)))
        mn   = float(np.min(data))
        mx   = float(np.max(data))
        u    = f' {unit}' if unit else ''
        self._lbls[0].setText(f'{mean:.4g}{u}')
        self._lbls[1].setText(f'{rms:.4g}{u}')
        self._lbls[2].setText(f'{mn:.4g}')
        self._lbls[3].setText(f'{mx:.4g}')
        # Подсветить строку
        self.setStyleSheet('background:#fffbe6;')

    def clear(self):
        for l in self._lbls:
            l.setText('—')
        self.setStyleSheet('')
