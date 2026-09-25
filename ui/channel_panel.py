"""Одна таблица каналов: видимость, имя, значение, цена деления, смещение."""

import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from core.channel_state import ChannelState
from core.measure import parse_y_text
from ui.fonts import mono_font, ui_font
from ui.plot_area import Y_DIV_SEQ, fmt_y_div


def _nofocus(btn: QPushButton):
    btn.setFocusPolicy(Qt.NoFocus)
    btn.setAutoDefault(False)
    return btn


class ChannelRow(QWidget):
    sig_visibility = Signal(int, bool)
    sig_scale = Signal(int, float)          # цена деления, физ. ед./дел
    sig_offset = Signal(int, float)
    sig_auto = Signal(int)
    sig_calib_requested = Signal(int)
    sig_activated = Signal(int)

    def __init__(self, idx: int, name: str, color: str, parent=None):
        super().__init__(parent)
        self._idx = idx
        self._unit = ''
        self._full_name = name
        self._active = False
        self.setObjectName('chRow')
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumHeight(28)
        self.setCursor(Qt.PointingHandCursor)
        self._build(name, color)
        self._update_bg()

    def _build(self, name: str, color: str):
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 1, 4, 1)
        row.setSpacing(4)

        self._dot = QLabel('▌')
        self._dot.setStyleSheet(f'color:{color};')
        self._dot.setFont(ui_font(1))
        self._dot.setFixedWidth(14)
        row.addWidget(self._dot)

        self._cb = QCheckBox()
        self._cb.setChecked(True)
        self._cb.setFocusPolicy(Qt.NoFocus)
        self._cb.setToolTip('Показать или скрыть канал')
        self._cb.toggled.connect(self._on_visible)
        row.addWidget(self._cb)

        self._lbl_name = QLabel(name)
        self._lbl_name.setFont(ui_font(-1))
        self._lbl_name.setMinimumWidth(48)
        self._lbl_name.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        row.addWidget(self._lbl_name, stretch=2)

        self._val = QLabel('—')
        self._val.setFont(mono_font(-1))
        self._val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._val.setMinimumWidth(64)
        row.addWidget(self._val, stretch=1)

        self._cb_ydiv = QComboBox()
        self._cb_ydiv.setEditable(True)
        self._cb_ydiv.setInsertPolicy(QComboBox.NoInsert)
        self._cb_ydiv.setFocusPolicy(Qt.ClickFocus)
        self._cb_ydiv.setMinimumWidth(72)
        self._cb_ydiv.setFont(ui_font(-1))
        self._cb_ydiv.setToolTip('Цена деления по Y, физические единицы на клетку')
        for v in Y_DIV_SEQ:
            self._cb_ydiv.addItem(fmt_y_div(v), v)
        self._cb_ydiv.setCurrentIndex(Y_DIV_SEQ.index(1.0))
        self._cb_ydiv.activated.connect(self._emit_ydiv)
        self._cb_ydiv.lineEdit().editingFinished.connect(self._emit_ydiv)
        row.addWidget(self._cb_ydiv)

        self._lbl_unit = QLabel('')
        self._lbl_unit.setFont(ui_font(-1))
        self._lbl_unit.setMinimumWidth(24)
        row.addWidget(self._lbl_unit)
        self.set_name(name)

        from PySide6.QtWidgets import QDoubleSpinBox
        self._sb_offset = QDoubleSpinBox()
        self._sb_offset.setRange(-1e9, 1e9)
        self._sb_offset.setDecimals(3)
        self._sb_offset.setSingleStep(0.1)
        self._sb_offset.setValue(0.0)
        self._sb_offset.setPrefix('+')
        self._sb_offset.setMinimumWidth(72)
        self._sb_offset.setFocusPolicy(Qt.ClickFocus)
        self._sb_offset.setToolTip('Вертикальное смещение, в делениях после масштаба')
        self._sb_offset.valueChanged.connect(lambda v: self.sig_offset.emit(self._idx, float(v)))
        row.addWidget(self._sb_offset)

        btn_auto = _nofocus(QPushButton('A'))
        btn_auto.setFixedWidth(26)
        btn_auto.setToolTip('Авто-масштаб по текущим данным')
        btn_auto.clicked.connect(lambda: self.sig_auto.emit(self._idx))
        row.addWidget(btn_auto)

        btn_cal = _nofocus(QPushButton('⚙'))
        btn_cal.setFixedWidth(26)
        btn_cal.setToolTip('Калибровка канала')
        btn_cal.clicked.connect(lambda: self.sig_calib_requested.emit(self._idx))
        row.addWidget(btn_cal)

    def _on_visible(self, visible: bool):
        self.sig_visibility.emit(self._idx, bool(visible))
        self.sig_activated.emit(self._idx)

    def _emit_ydiv(self):
        if self._cb_ydiv.signalsBlocked():
            return
        text = self._cb_ydiv.currentText()
        try:
            value = parse_y_text(text)
        except ValueError:
            data = self._cb_ydiv.currentData()
            if data is None:
                return
            value = float(data)
        if not math.isfinite(value) or value == 0.0:
            return
        self.sig_activated.emit(self._idx)
        self.sig_scale.emit(self._idx, float(value))

    def set_y_per_div(self, value: float):
        self._cb_ydiv.blockSignals(True)
        best = min(range(len(Y_DIV_SEQ)), key=lambda i: abs(Y_DIV_SEQ[i] - value))
        if abs(Y_DIV_SEQ[best] - value) / max(abs(value), 1e-15) < 0.01:
            self._cb_ydiv.setCurrentIndex(best)
        else:
            self._cb_ydiv.setCurrentText(fmt_y_div(value))
        self._cb_ydiv.blockSignals(False)

    def set_offset(self, value: float):
        self._sb_offset.blockSignals(True)
        self._sb_offset.setValue(value)
        self._sb_offset.blockSignals(False)

    def set_visible(self, visible: bool):
        self._cb.blockSignals(True)
        self._cb.setChecked(visible)
        self._cb.blockSignals(False)

    def set_unit(self, unit: str):
        self._unit = unit or ''
        self._lbl_unit.setText(self._unit)
        self._refresh_tip()

    def set_name(self, name: str):
        self._full_name = name
        width = max(48, self._lbl_name.width())
        shown = QFontMetrics(self._lbl_name.font()).elidedText(name, Qt.ElideRight, width)
        self._lbl_name.setText(shown)
        self._refresh_tip()

    def _refresh_tip(self):
        tip = self._full_name
        if self._unit:
            tip += f'\n[{self._unit}]'
        self._lbl_name.setToolTip(tip)
        self._lbl_unit.setToolTip(self._unit)

    def set_value(self, value: float):
        if value is None or not math.isfinite(value):
            self._val.setText('—')
        else:
            self._val.setText(f'{value:+.4g}')

    def clear_value(self):
        self._val.setText('—')

    def set_active(self, active: bool):
        self._active = active
        self._update_bg()

    def _update_bg(self):
        if self._active:
            self.setStyleSheet(
                '#chRow { background:#d6eaff; border-left:3px solid #0070c0; }'
            )
        else:
            self.setStyleSheet(
                '#chRow { background:transparent; border-left:3px solid transparent; }'
            )

    def mousePressEvent(self, event):
        self.sig_activated.emit(self._idx)
        super().mousePressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.set_name(self._full_name)


class ChannelPanel(QWidget):
    sig_visibility = Signal(int, bool)
    sig_scale = Signal(int, float)
    sig_offset = Signal(int, float)
    sig_auto = Signal(int)
    sig_calib_requested = Signal(int)
    sig_activated = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(280)
        self._rows: list[ChannelRow] = []
        self._active_idx = 0
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        hdr = QWidget()
        hdr.setObjectName('chHeader')
        hdr.setAttribute(Qt.WA_StyledBackground, True)
        hdr.setStyleSheet('#chHeader { background:#e8e8e8; }')
        hrow = QHBoxLayout(hdr)
        hrow.setContentsMargins(4, 2, 4, 2)
        hrow.setSpacing(4)
        for text, stretch in (
            ('', 0), ('', 0), ('Имя', 2), ('Значение', 1),
            ('Ед./дел', 0), ('Ед.', 0), ('Смещ.', 0),
        ):
            lab = QLabel(text)
            lab.setFont(ui_font(-1))
            if stretch:
                hrow.addWidget(lab, stretch=stretch)
            else:
                hrow.addWidget(lab)
        layout.addWidget(hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        layout.addWidget(sep)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setFrameShape(QFrame.NoFrame)
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
            row.sig_activated.connect(self.sig_activated)
            vl.addWidget(row)
            self._rows.append(row)
        vl.addStretch()
        self._scroll.setWidget(container)
        self._active_idx = 0
        if self._rows:
            self._rows[0].set_active(True)

    def apply_state(self, state: ChannelState):
        idx = state.index
        if not (0 <= idx < len(self._rows)):
            return
        row = self._rows[idx]
        row.set_name(state.name)
        row.set_unit(state.unit)
        row.set_visible(state.visible)
        row.set_y_per_div(state.y_per_div)
        row.set_offset(state.offset)

    def update_scale_offset(self, idx: int, scale: float, offset: float):
        """scale — визуальный множитель графика. В комбо показываем цену деления."""
        from core.measure import y_per_div_from_scale
        if 0 <= idx < len(self._rows):
            self._rows[idx].set_y_per_div(y_per_div_from_scale(scale))
            self._rows[idx].set_offset(offset)

    def update_unit(self, idx: int, unit: str):
        if 0 <= idx < len(self._rows):
            self._rows[idx].set_unit(unit)

    def update_name(self, idx: int, name: str):
        if 0 <= idx < len(self._rows):
            self._rows[idx].set_name(name)

    def update_values(self, y_vals):
        if y_vals is None:
            self.clear_values()
            return
        for i, row in enumerate(self._rows):
            if i < len(y_vals):
                row.set_value(float(y_vals[i]))
            else:
                row.clear_value()

    def clear_values(self):
        for row in self._rows:
            row.clear_value()

    def set_active(self, idx: int):
        if not (0 <= idx < len(self._rows)):
            return
        if 0 <= self._active_idx < len(self._rows):
            self._rows[self._active_idx].set_active(False)
        self._active_idx = idx
        self._rows[idx].set_active(True)

    @property
    def active_channel(self) -> int:
        return self._active_idx
