import math

import numpy as np
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QGridLayout, QLabel,
    QCheckBox, QDoubleSpinBox, QPushButton, QComboBox,
    QScrollArea, QFrame, QSizePolicy
)
from PySide6.QtCore import Signal, Qt, QPointF, QSize
from PySide6.QtGui import QPixmap, QColor, QIcon, QPainter, QPen

from ui.plot_area import Y_DIV_SEQ, fmt_y_div

# Цвет, Канал, галочка, Y/дел, смещение, A, калибровка.
# Поля имеют фиксированную сетку; у числового смещения нет встроенных стрелок.
_COLS = (18, 72, 32, 72, 100, 34, 34)
_ROW_H = 30


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
        self.setObjectName('channelRow')
        self.setFixedHeight(_ROW_H + 6)
        row = QGridLayout(self)
        row.setContentsMargins(8, 3, 8, 3)
        row.setHorizontalSpacing(5)
        row.setVerticalSpacing(0)
        row.setColumnMinimumWidth(0, _COLS[0])
        row.setColumnMinimumWidth(2, _COLS[2])
        row.setColumnMinimumWidth(3, _COLS[3])
        row.setColumnMinimumWidth(4, _COLS[4])
        row.setColumnMinimumWidth(5, _COLS[5])
        row.setColumnMinimumWidth(6, _COLS[6])
        row.setColumnStretch(1, 1)

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
        self._sb_offset.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self._sb_offset.setFixedWidth(_COLS[4])
        self._sb_offset.setFixedHeight(_ROW_H)
        self._sb_offset.setAlignment(Qt.AlignRight)
        self._sb_offset.setToolTip('Смещение по Y. Значение можно менять клавишами ↑/↓.')
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

        row.addWidget(dot, 0, 0)
        row.addWidget(self._lbl_name, 0, 1)
        row.addWidget(self._cb, 0, 2, alignment=Qt.AlignCenter)
        row.addWidget(self._cb_ydiv, 0, 3)
        row.addWidget(self._sb_offset, 0, 4)
        row.addWidget(btn_auto, 0, 5)
        row.addWidget(btn_cal, 0, 6)

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
        self.setMinimumWidth(sum(_COLS) + 6 * 5 + 16)
        self.setStyleSheet(
            'QWidget#channelHeader { background:#f2f5f9; }'
            'QWidget#channelRow { border-bottom:1px solid #e7ebf0; }'
            'QWidget#channelRow:hover { background:#f7faff; }'
        )
        self._rows: list[ChannelRow] = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 4, 2, 2)
        layout.setSpacing(2)

        # Заголовок столбцов — те же ширины и тот же правый зазор, что у строк.
        self._hdr = QWidget()
        self._hdr.setObjectName('channelHeader')
        self._hdr.setFixedHeight(_ROW_H + 6)
        self._hdr.setAutoFillBackground(True)
        self._hrow = QGridLayout(self._hdr)
        self._hrow.setContentsMargins(8, 3, 8, 3)
        self._hrow.setHorizontalSpacing(5)
        self._hrow.setVerticalSpacing(0)
        self._hrow.setColumnMinimumWidth(0, _COLS[0])
        self._hrow.setColumnMinimumWidth(2, _COLS[2])
        self._hrow.setColumnMinimumWidth(3, _COLS[3])
        self._hrow.setColumnMinimumWidth(4, _COLS[4])
        self._hrow.setColumnMinimumWidth(5, _COLS[5])
        self._hrow.setColumnMinimumWidth(6, _COLS[6])
        self._hrow.setColumnStretch(1, 1)
        headers = ('', 'Канал', 'Вкл.', 'Y/дел', 'Смещение', 'Авто', '⚙')
        aligns = (
            Qt.AlignCenter, Qt.AlignLeft | Qt.AlignVCenter, Qt.AlignCenter,
            Qt.AlignRight | Qt.AlignVCenter, Qt.AlignRight | Qt.AlignVCenter,
            Qt.AlignCenter, Qt.AlignCenter,
        )
        for col, (txt, width, align) in enumerate(zip(headers, _COLS, aligns)):
            label = QLabel(txt)
            if col == 1:
                label.setMinimumWidth(width)
                label.setSizePolicy(
                    QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
                )
            elif col != 4:
                label.setFixedWidth(width)
            label.setAlignment(align)
            if col == 3:
                label.setContentsMargins(0, 0, 20, 0)
            if col == 6:
                label.setPixmap(_gear_pixmap(13))
                label.setText('')
            self._hrow.addWidget(label, 0, col)
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
        self._hrow.setContentsMargins(8, 3, 8 + gap, 3)

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
