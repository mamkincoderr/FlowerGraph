"""
AmplitudeScalePanel — левая шкала амплитуды.

Показывает для каждого канала:
  [▌ цвет] [имя] [текущее значение под курсором]

Клик по строке канала → канал становится активным
(Ctrl+= / Ctrl+- работают только на активном канале).
"""

import math
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QFrame, QScrollArea, QSizePolicy, QPushButton
)
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QFont, QCursor


class _ChannelRow(QWidget):
    clicked    = Signal(int)   # channel index
    btn_up_clicked = Signal(int)
    btn_dn_clicked = Signal(int)

    def __init__(self, idx: int, name: str, color: str, parent=None):
        super().__init__(parent)
        self._idx = idx
        self._active = False
        self.setFixedHeight(24)
        self.setCursor(QCursor(Qt.PointingHandCursor))

        lo = QHBoxLayout(self)
        lo.setContentsMargins(3, 0, 3, 0)
        lo.setSpacing(2)

        # Цветная полоска
        self._dot = QLabel('▌')
        self._dot.setStyleSheet(f'color:{color}; font-size:14px;')
        self._dot.setFixedWidth(13)
        lo.addWidget(self._dot)

        # Имя канала
        self._name = QLabel(name)
        self._name.setFixedWidth(30)
        font = QFont()
        font.setPointSize(8)
        self._name.setFont(font)
        self._name.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lo.addWidget(self._name)

        # Текущее значение под курсором
        self._val = QLabel('——')
        self._val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        mono = QFont('Courier New', 8)
        self._val.setFont(mono)
        lo.addWidget(self._val, stretch=1)

        # Цена деления
        self._scale_lbl = QLabel('1.0/д')
        self._scale_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        font_small = QFont('Courier New', 7)
        self._scale_lbl.setFont(font_small)
        self._scale_lbl.setStyleSheet('color:#555; font-size:8px;')
        self._scale_lbl.setFixedWidth(45)
        lo.addWidget(self._scale_lbl)

        self._btn_up = QPushButton('▲')
        self._btn_up.setFixedSize(16, 12)
        self._btn_up.setStyleSheet('font-size:7px; padding:0;')
        self._btn_dn = QPushButton('▼')
        self._btn_dn.setFixedSize(16, 12)
        self._btn_dn.setStyleSheet('font-size:7px; padding:0;')
        btn_v = QVBoxLayout()
        btn_v.setSpacing(0)
        btn_v.setContentsMargins(0, 0, 0, 0)
        btn_v.addWidget(self._btn_up)
        btn_v.addWidget(self._btn_dn)
        lo.addLayout(btn_v)

        self._btn_up.clicked.connect(lambda: self.btn_up_clicked.emit(self._idx))
        self._btn_dn.clicked.connect(lambda: self.btn_dn_clicked.emit(self._idx))

        self._update_bg()

    def set_scale_label(self, scale: float, unit: str):
        unit_str = f' {unit}' if unit else ''
        self._scale_lbl.setText(f'{scale:g}{unit_str}/д')

    # ------------------------------------------------------------------

    def set_value(self, v: float):
        if math.isnan(v) or math.isinf(v):
            self._val.setText('——')
        else:
            self._val.setText(f'{v:+.4g}')

    def clear_value(self):
        self._val.setText('——')

    def set_name(self, name: str):
        self._name.setText(name)

    def set_active(self, active: bool):
        self._active = active
        self._update_bg()

    def _update_bg(self):
        if self._active:
            self.setStyleSheet('background:#d6eaff; border-left:2px solid #0070c0;')
        else:
            self.setStyleSheet('background:transparent; border-left:2px solid transparent;')

    def mousePressEvent(self, event):
        self.clicked.emit(self._idx)
        super().mousePressEvent(event)


# ===========================================================================

class AmplitudeScalePanel(QWidget):
    """Левая панель со значениями каналов (в стиле шкалы амплитуды PG)."""

    channel_activated = Signal(int)   # пользователь выбрал активный канал
    sig_scale_up      = Signal(int)   # уменьшить цену деления (увеличить масштаб)
    sig_scale_down    = Signal(int)   # увеличить цену деления

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(130)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Заголовок
        hdr = QLabel('Каналы')
        hdr.setStyleSheet(
            'background:#e8e8e8; font-size:9px; font-weight:bold;'
            'padding:2px 4px; border-bottom:1px solid #ccc;'
        )
        hdr.setFixedHeight(18)
        outer.addWidget(hdr)

        # Скроллируемая область с рядами каналов
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(self._scroll)

        self._rows: list[_ChannelRow] = []
        self._active_idx = 0
        self._container: QWidget | None = None

    # ------------------------------------------------------------------

    def setup(self, names: list[str], colors: list[str]):
        container = QWidget()
        vl = QVBoxLayout(container)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)
        self._rows.clear()

        for i, (name, color) in enumerate(zip(names, colors)):
            row = _ChannelRow(i, name, color)
            row.clicked.connect(self._on_row_clicked)
            row.btn_up_clicked.connect(self.sig_scale_up)
            row.btn_dn_clicked.connect(self.sig_scale_down)
            # Тонкий разделитель между строками
            sep = QFrame()
            sep.setFrameShape(QFrame.HLine)
            sep.setStyleSheet('color:#e0e0e0;')
            sep.setFixedHeight(1)
            vl.addWidget(sep)
            vl.addWidget(row)
            self._rows.append(row)

        vl.addStretch()
        self._container = container
        self._scroll.setWidget(container)

        if self._rows:
            self._active_idx = 0
            self._rows[0].set_active(True)

    def update_values(self, y_vals):
        """Обновить значения под курсором. y_vals — ndarray или None."""
        if y_vals is None:
            for r in self._rows:
                r.clear_value()
            return
        for i, row in enumerate(self._rows):
            if i < len(y_vals):
                row.set_value(float(y_vals[i]))
            else:
                row.clear_value()

    def clear_values(self):
        for r in self._rows:
            r.clear_value()

    def update_name(self, idx: int, name: str):
        if 0 <= idx < len(self._rows):
            self._rows[idx].set_name(name)

    @property
    def active_channel(self) -> int:
        return self._active_idx

    def set_active(self, idx: int):
        if 0 <= idx < len(self._rows):
            if self._active_idx < len(self._rows):
                self._rows[self._active_idx].set_active(False)
            self._active_idx = idx
            self._rows[idx].set_active(True)

    # ------------------------------------------------------------------

    def update_scale_label(self, idx: int, scale: float, unit: str):
        if 0 <= idx < len(self._rows):
            self._rows[idx].set_scale_label(scale, unit)

    def _on_row_clicked(self, idx: int):
        self.set_active(idx)
        self.channel_activated.emit(idx)
