"""Статистика выделения: среднее, СКО, RMS, минимум, максимум, размах, число точек."""

import numpy as np
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QHeaderView, QLabel, QPushButton, QSizePolicy,
    QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.measure import fragment_stats
from ui.fonts import mono_font, ui_font

_COLUMNS = ('Канал', 'Ср.', 'СКО', 'RMS', 'Мин', 'Макс', 'Размах', 'N')


class StatsPanel(QWidget):
    collapsed_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._names: list[str] = []
        self._colors: list[str] = []
        self._collapsed = False
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._build_ui()

    def sizeHint(self):
        return QSize(640, 120)

    def _build_ui(self):
        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        hdr = QWidget()
        hdr.setObjectName('statsHeader')
        hdr.setAttribute(Qt.WA_StyledBackground, True)
        hdr.setStyleSheet('#statsHeader { background:#e8e8e8; }')
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(4, 2, 4, 2)
        self._btn = QPushButton('▼  Статистика')
        self._btn.setFlat(True)
        self._btn.setFont(ui_font(0))
        self._btn.setFocusPolicy(Qt.NoFocus)
        self._btn.clicked.connect(self.toggle)
        hl.addWidget(self._btn)
        hl.addStretch()
        self._btn_copy = QPushButton('Копировать')
        self._btn_copy.setFocusPolicy(Qt.NoFocus)
        self._btn_copy.setFont(ui_font(-1))
        self._btn_copy.clicked.connect(self.copy_to_clipboard)
        hl.addWidget(self._btn_copy)
        vl.addWidget(hdr)

        self._stack = QStackedWidget()
        self._empty = QLabel('Нет выделения')
        self._empty.setFont(ui_font(-1))
        self._empty.setStyleSheet('color:#666; padding:6px;')
        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(list(_COLUMNS))
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.NoSelection)
        self._table.setFocusPolicy(Qt.NoFocus)
        self._table.setFont(mono_font(-1))
        self._table.horizontalHeader().setFont(ui_font(-1))
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._stack.addWidget(self._empty)
        self._stack.addWidget(self._table)
        vl.addWidget(self._stack)
        self._body = self._stack

    def toggle(self):
        self.set_collapsed(not self._collapsed)

    def set_collapsed(self, collapsed: bool):
        self._collapsed = collapsed
        self._body.setVisible(not collapsed)
        self._btn.setText(('▶' if collapsed else '▼') + '  Статистика')
        self.collapsed_changed.emit(collapsed)

    def setup(self, names: list[str], colors: list[str]):
        self._names = list(names)
        self._colors = list(colors)
        self._table.setRowCount(0)
        self.show_empty()

    def show_empty(self):
        self._table.setRowCount(0)
        self._stack.setCurrentWidget(self._empty)

    def clear(self):
        self.show_empty()

    def update_stats(self, channel_data: list[np.ndarray | None], units: list[str],
                     visible: list[bool] | None = None):
        rows = []
        for i, data in enumerate(channel_data):
            if visible is not None and i < len(visible) and not visible[i]:
                continue
            if data is None:
                continue
            st = fragment_stats(np.asarray(data))
            if st is None:
                continue
            name = self._names[i] if i < len(self._names) else f'CH{i + 1}'
            unit = units[i] if i < len(units) else ''
            rows.append((i, name, unit, st))
        if not rows:
            self.show_empty()
            return
        unit0 = rows[0][2]
        headers = list(_COLUMNS)
        if unit0:
            for col in (1, 2, 3, 4, 5, 6):
                headers[col] = f'{_COLUMNS[col]} ({unit0})'
        self._table.setHorizontalHeaderLabels(headers)
        self._table.setRowCount(len(rows))
        for r, (idx, name, _unit, st) in enumerate(rows):
            vals = (
                name,
                f'{st["mean"]:.4g}',
                f'{st["std"]:.4g}',
                f'{st["rms"]:.4g}',
                f'{st["min"]:.4g}',
                f'{st["max"]:.4g}',
                f'{st["pp"]:.4g}',
                str(st['n']),
            )
            for c, text in enumerate(vals):
                item = QTableWidgetItem(text)
                if c == 0 and idx < len(self._colors):
                    item.setForeground(Qt.black)
                    item.setToolTip(self._colors[idx])
                self._table.setItem(r, c, item)
        self._stack.setCurrentWidget(self._table)

    def copy_to_clipboard(self):
        if self._table.rowCount() == 0:
            return
        headers = [
            self._table.horizontalHeaderItem(c).text()
            for c in range(self._table.columnCount())
        ]
        lines = ['\t'.join(headers)]
        for r in range(self._table.rowCount()):
            cells = []
            for c in range(self._table.columnCount()):
                item = self._table.item(r, c)
                cells.append(item.text() if item else '')
            lines.append('\t'.join(cells))
        QGuiApplication.clipboard().setText('\n'.join(lines))
