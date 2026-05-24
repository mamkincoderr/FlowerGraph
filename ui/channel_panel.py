import numpy as np
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QCheckBox, QDoubleSpinBox, QPushButton, QComboBox,
    QScrollArea, QFrame, QSizePolicy
)
from PySide6.QtCore import Signal, Qt

from ui.plot_area import Y_DIV_SEQ, fmt_y_div


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
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 1, 2, 1)
        row.setSpacing(2)

        dot = QLabel('▬')
        dot.setStyleSheet(f'color: {color}; font-size: 13px;')
        dot.setFixedWidth(16)
        row.addWidget(dot)

        self._lbl_name = QLabel(name[:5])
        self._lbl_name.setFixedWidth(40)
        self._lbl_name.setToolTip(name)
        row.addWidget(self._lbl_name)

        cb_wrap = QWidget()
        cb_wrap.setFixedWidth(20)
        cb_inner = QHBoxLayout(cb_wrap)
        cb_inner.setContentsMargins(2, 0, 0, 0)
        cb_inner.setSpacing(0)
        self._cb = QCheckBox()
        self._cb.setChecked(True)
        self._cb.setToolTip('Показать/скрыть канал')
        self._cb.toggled.connect(lambda v: self.sig_visibility.emit(self._idx, v))
        cb_inner.addWidget(self._cb)
        row.addWidget(cb_wrap)

        # Y_DIV combo (цена деления по Y)
        self._cb_ydiv = QComboBox()
        self._cb_ydiv.setEditable(True)
        for v in Y_DIV_SEQ:
            self._cb_ydiv.addItem(fmt_y_div(v), v)
        self._cb_ydiv.setCurrentIndex(Y_DIV_SEQ.index(1.0))
        self._cb_ydiv.setFixedWidth(68)
        self._cb_ydiv.setToolTip('Цена деления по Y\n(шаг 1–2–5; или введите значение вручную)')
        self._cb_ydiv.currentIndexChanged.connect(self._on_ydiv_combo)
        self._cb_ydiv.lineEdit().editingFinished.connect(self._on_ydiv_edit)
        row.addWidget(self._cb_ydiv)

        self._sb_offset = QDoubleSpinBox()
        self._sb_offset.setRange(-1e9, 1e9)
        self._sb_offset.setSingleStep(0.1)
        self._sb_offset.setDecimals(3)
        self._sb_offset.setValue(0.0)
        self._sb_offset.setPrefix('+')
        self._sb_offset.setFixedWidth(72)
        self._sb_offset.setToolTip('Вертикальное смещение')
        self._sb_offset.valueChanged.connect(lambda v: self.sig_offset.emit(self._idx, v))
        row.addWidget(self._sb_offset)

        btn_auto = QPushButton('A')
        btn_auto.setFixedWidth(22)
        btn_auto.setToolTip('Авто-масштаб по текущим данным')
        btn_auto.clicked.connect(lambda: self.sig_auto.emit(self._idx))
        row.addWidget(btn_auto)

        btn_cal = QPushButton('⚙')
        btn_cal.setFixedWidth(22)
        btn_cal.setToolTip('Калибровка канала (единицы, коэффициент, смещение)')
        btn_cal.clicked.connect(lambda: self.sig_calib_requested.emit(self._idx))
        row.addWidget(btn_cal)

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
        self._lbl_name.setText(name[:4])
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
        self.setFixedWidth(370)
        self._rows: list[ChannelRow] = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 4, 2, 2)
        layout.setSpacing(2)

        # Заголовок столбцов — ширины совпадают с ChannelRow
        hdr = QWidget()
        hdr.setStyleSheet('background:#f0f0f0;')
        hrow = QHBoxLayout(hdr)
        hrow.setContentsMargins(2, 1, 2, 1)
        hrow.setSpacing(2)
        specs = [('', 16), ('Кан.', 40), ('В', 20), ('Y/дел', 68), ('Смещ.', 72), ('', 22), ('⚙', 22)]
        for txt, w in specs:
            l = QLabel(txt)
            l.setFixedWidth(w)
            l.setStyleSheet('font-size: 9px; color: #555; font-weight: bold;')
            l.setAlignment(Qt.AlignCenter)
            hrow.addWidget(l)
        hdr.setFixedHeight(18)
        layout.addWidget(hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet('color: #cccccc;')
        layout.addWidget(sep)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
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
