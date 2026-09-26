import math

import numpy as np
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QCheckBox, QDoubleSpinBox, QPushButton, QComboBox,
    QScrollArea, QFrame, QSizePolicy
)
from PySide6.QtCore import Signal, Qt, QPointF, QSize
from PySide6.QtGui import QPixmap, QColor, QIcon, QPainter, QPen

from ui.plot_area import Y_DIV_SEQ, fmt_y_div

# Цвет, Канал, галочка, Y/дел, смещение, A, калибровка.
# Y/дел чуть шире 46: слово и цифра садятся левее штатной стрелки, не обрезаясь.
_COLS = (18, 64, 28, 54, 88, 28, 28)
_ARROW_W = 16
_ROW_H = 26


def _gear_pixmap(px: int = 15) -> QPixmap:
    """Чёрная шестерёнка. Символ ⚙ в системном шрифте рисуется бледным кружком."""
    img = QPixmap(px, px)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor('#222222'))
    pen.setWidthF(1.3)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    c = px / 2.0
    painter.drawEllipse(QPointF(c, c), px * 0.16, px * 0.16)
    painter.drawEllipse(QPointF(c, c), px * 0.30, px * 0.30)
    for i in range(8):
        a = math.radians(i * 45)
        painter.drawLine(
            QPointF(c + math.cos(a) * px * 0.32, c + math.sin(a) * px * 0.32),
            QPointF(c + math.cos(a) * px * 0.46, c + math.sin(a) * px * 0.46),
        )
    painter.end()
    return img


def _vgrid() -> QFrame:
    line = QFrame()
    line.setFixedWidth(1)
    line.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
    line.setStyleSheet('background: #c8c8c8;')
    return line


class ChannelRow(QWidget):
    sig_visibility      = Signal(int, bool)
    sig_scale           = Signal(int, float)   # idx, y_div (scale multiplier)
    sig_offset          = Signal(int, float)
    sig_auto            = Signal(int)
    sig_calib_requested = Signal(int)          # пользователь нажал ⚙ для канала idx

    def __init__(self, idx: int, name: str, color: str, parent=None):
        super().__init__(parent)
        self._idx = idx
        self._unit = ''
        self._build(name, color)

    def _build(self, name: str, color: str):
        self.setFixedHeight(_ROW_H + 4)
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(0)

        swatch = QPixmap(10, 10)
        swatch.fill(QColor(color))
        dot = QLabel()
        dot.setPixmap(swatch)
        dot.setFixedWidth(_COLS[0])
        dot.setAlignment(Qt.AlignCenter)

        self._lbl_name = QLabel(name)
        self._lbl_name.setMinimumWidth(_COLS[1])
        self._lbl_name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._lbl_name.setToolTip(name)

        self._cb = QCheckBox()
        self._cb.setChecked(True)
        self._cb.setFixedWidth(_COLS[2])
        self._cb.setFixedHeight(_ROW_H)
        self._cb.setStyleSheet(
            'QCheckBox { padding-left: 7px; spacing: 0; }'
            'QCheckBox::indicator { width: 13px; height: 13px; }'
        )
        self._cb.setToolTip('Показать или скрыть канал')
        self._cb.toggled.connect(lambda v: self.sig_visibility.emit(self._idx, v))

        self._cb_ydiv = QComboBox()
        self._cb_ydiv.setEditable(True)
        self._cb_ydiv.setMinimumContentsLength(1)
        self._cb_ydiv.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        for v in Y_DIV_SEQ:
            self._cb_ydiv.addItem(fmt_y_div(v), v)
        self._cb_ydiv.setCurrentIndex(Y_DIV_SEQ.index(1.0))
        self._cb_ydiv.setFixedWidth(_COLS[3])
        self._cb_ydiv.setFixedHeight(_ROW_H)
        self._cb_ydiv.setToolTip('Цена деления по Y')
        edit = self._cb_ydiv.lineEdit()
        edit.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        edit.setMinimumWidth(0)
        self._cb_ydiv.currentIndexChanged.connect(self._on_ydiv_combo)
        edit.editingFinished.connect(self._on_ydiv_edit)

        self._sb_offset = QDoubleSpinBox()
        self._sb_offset.setRange(-1e9, 1e9)
        self._sb_offset.setSingleStep(0.1)
        self._sb_offset.setDecimals(3)
        self._sb_offset.setValue(0.0)
        self._sb_offset.setFixedWidth(_COLS[4])
        self._sb_offset.setFixedHeight(_ROW_H)
        self._sb_offset.setAlignment(Qt.AlignRight)
        self._sb_offset.setToolTip('Вертикальное смещение')
        self._sb_offset.valueChanged.connect(lambda v: self.sig_offset.emit(self._idx, v))

        btn_auto = QPushButton('A')
        btn_auto.setFixedWidth(_COLS[5])
        btn_auto.setFixedHeight(_ROW_H)
        btn_auto.setStyleSheet('QPushButton { padding: 0; color: #222; }')
        btn_auto.setToolTip('Вписать канал в текущий экран')
        btn_auto.clicked.connect(lambda: self.sig_auto.emit(self._idx))

        btn_cal = QPushButton()
        btn_cal.setIcon(QIcon(_gear_pixmap()))
        btn_cal.setIconSize(QSize(15, 15))
        btn_cal.setFixedWidth(_COLS[6])
        btn_cal.setFixedHeight(_ROW_H)
        btn_cal.setStyleSheet('QPushButton { padding: 0; }')
        btn_cal.setToolTip('Калибровка канала')
        btn_cal.clicked.connect(lambda: self.sig_calib_requested.emit(self._idx))

        for widget in (dot, self._lbl_name, self._cb, self._cb_ydiv,
                       self._sb_offset, btn_auto, btn_cal):
            row.addWidget(_vgrid())
            row.addWidget(widget)
        row.addWidget(_vgrid())

    # --- обработчики Y_DIV ---

    def _on_ydiv_combo(self, idx):
        v = self._cb_ydiv.currentData()
        if v is not None:
            self.sig_scale.emit(self._idx, float(v))

    def _on_ydiv_edit(self):
        try:
            v = float(self._cb_ydiv.currentText().replace(',', '.').replace('k', 'e3').replace('m', 'e-3'))
            self.sig_scale.emit(self._idx, v)
        except ValueError:
            pass

    # --- обновление из кода без эмиссии сигналов ---

    def set_scale(self, v: float):
        self._cb_ydiv.blockSignals(True)
        # Поиск ближайшего в Y_DIV_SEQ
        try:
            best = min(range(len(Y_DIV_SEQ)), key=lambda i: abs(Y_DIV_SEQ[i] - v))
            if abs(Y_DIV_SEQ[best] - v) / max(abs(v), 1e-15) < 0.01:
                self._cb_ydiv.setCurrentIndex(best)
            else:
                self._cb_ydiv.setCurrentText(fmt_y_div(v))
        except Exception:
            pass
        self._cb_ydiv.blockSignals(False)

    def set_offset(self, v: float):
        self._sb_offset.blockSignals(True)
        self._sb_offset.setValue(v)
        self._sb_offset.blockSignals(False)

    def set_visible(self, v: bool):
        self._cb.blockSignals(True)
        self._cb.setChecked(v)
        self._cb.blockSignals(False)

    def set_unit(self, unit: str):
        self._unit = unit
        base = self._lbl_name.toolTip()
        name = base.split('\n')[0]
        if unit:
            self._lbl_name.setToolTip(f'{name}\n[{unit}]')

    def set_name(self, name: str):
        self._lbl_name.setText(name)
        self._lbl_name.setToolTip(name + (f'\n[{self._unit}]' if self._unit else ''))

    def reset(self):
        self.set_scale(1.0)
        self.set_offset(0.0)
        self.set_visible(True)


class ChannelPanel(QWidget):
    sig_visibility      = Signal(int, bool)
    sig_scale           = Signal(int, float)
    sig_offset          = Signal(int, float)
    sig_auto            = Signal(int)
    sig_calib_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(sum(_COLS) + 8 + 16)
        self._rows: list[ChannelRow] = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 4, 2, 2)
        layout.setSpacing(2)

        # Заголовок столбцов — те же ширины и тот же правый зазор, что у строк.
        self._hdr = QWidget()
        self._hdr.setFixedHeight(_ROW_H + 4)
        self._hdr.setAutoFillBackground(True)
        self._hrow = QHBoxLayout(self._hdr)
        self._hrow.setContentsMargins(4, 2, 4, 2)
        self._hrow.setSpacing(0)
        headers = ('', 'Канал', '✓', 'Y/дел', 'Смещ.', 'A', '⚙')
        aligns = (
            Qt.AlignCenter, Qt.AlignLeft | Qt.AlignVCenter, Qt.AlignCenter,
            Qt.AlignRight | Qt.AlignVCenter, Qt.AlignRight | Qt.AlignVCenter,
            Qt.AlignCenter, Qt.AlignCenter,
        )
        # Y/дел и Смещ. заканчиваются над числом, а не над стрелкой поля.
        right_pad = (0, 0, 0, _ARROW_W, _ARROW_W, 0, 0)
        for txt, width, align, pad in zip(headers, _COLS, aligns, right_pad):
            self._hrow.addWidget(_vgrid())
            label = QLabel(txt)
            if txt == 'Канал':
                label.setMinimumWidth(width)
                label.setSizePolicy(
                    QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
                )
            else:
                label.setFixedWidth(width)
            label.setAlignment(align)
            if txt == '⚙':
                label.setPixmap(_gear_pixmap(13))
                label.setText('')
            if pad:
                label.setContentsMargins(0, 0, pad, 0)
            self._hrow.addWidget(label)
        self._hrow.addWidget(_vgrid())
        layout.addWidget(self._hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet('color: #cccccc;')
        layout.addWidget(sep)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.verticalScrollBar().rangeChanged.connect(self._sync_header_scrollbar)
        layout.addWidget(self._scroll)

    def setup(self, names: list[str], colors: list[str]):
        container = QWidget()
        vl = QVBoxLayout(container)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)
        self._rows.clear()

        for i, (name, color) in enumerate(zip(names, colors)):
            row = ChannelRow(i, name, color)
            row.sig_visibility.connect(self.sig_visibility)
            row.sig_scale.connect(self.sig_scale)
            row.sig_offset.connect(self.sig_offset)
            row.sig_auto.connect(self.sig_auto)
            row.sig_calib_requested.connect(self.sig_calib_requested)
            vl.addWidget(row)
            self._rows.append(row)

        vl.addStretch()
        self._scroll.setWidget(container)
        self._sync_header_scrollbar()

    def _sync_header_scrollbar(self, *_):
        """Полоса прокрутки сужает строки. Шапка получает тот же правый отступ."""
        sb = self._scroll.verticalScrollBar()
        gap = sb.sizeHint().width() if sb.maximum() > sb.minimum() else 0
        self._hrow.setContentsMargins(4, 2, 4 + gap, 2)

    def update_scale_offset(self, idx: int, scale: float, offset: float):
        if 0 <= idx < len(self._rows):
            self._rows[idx].set_scale(scale)
            self._rows[idx].set_offset(offset)

    def update_unit(self, idx: int, unit: str):
        if 0 <= idx < len(self._rows):
            self._rows[idx].set_unit(unit)

    def update_name(self, idx: int, name: str):
        if 0 <= idx < len(self._rows):
            self._rows[idx].set_name(name)
