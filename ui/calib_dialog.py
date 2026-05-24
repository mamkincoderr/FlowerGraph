"""
ChannelCalibDialog — диалог физической калибровки канала.

Поддерживает:
  • Переименование канала
  • Единица измерения (мВ, Па, °C и т.д.)
  • Коэффициент A и смещение B:  Y_phys = A · raw + B
  • Помощник «по двум точкам»: вводятся (raw1, phys1) и (raw2, phys2),
    вычисляется A и B автоматически.
"""

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QVBoxLayout, QLabel, QLineEdit, QComboBox,
    QDoubleSpinBox, QPushButton, QMessageBox, QWidget
)
from PySide6.QtCore import Qt

# Единицы измерения (как в PowerGraph + пользовательские)
# Поле редактируемое: любое значение можно ввести вручную
PG_UNITS: list[str] = [
    '',
    'В', 'мВ', 'мкВ', 'кВ',
    'А', 'мА', 'мкА',
    'Вт', 'кВт', 'В·А',
    'Ом', 'кОм', 'МОм',
    'Гц', 'кГц', 'МГц',
    '°C', 'К',
    'Па', 'кПа', 'МПа', 'бар',
    'Н', 'Н·м',
    'м/с', 'об/мин',
    'рад', '°',
    '%', 'дБ',
    'с', 'мс', 'мкс',
]


class ChannelCalibDialog(QDialog):
    def __init__(self, idx: int, name: str, unit: str,
                 coeff: float, offset: float, parent=None):
        super().__init__(parent)
        self._idx = idx
        self.setWindowTitle(f'Калибровка канала {name}')
        self.setMinimumWidth(360)
        self._build_ui(name, unit, coeff, offset)

    def _build_ui(self, name: str, unit: str, coeff: float, offset: float):
        vl = QVBoxLayout(self)
        vl.setSpacing(8)

        # --- Имя и единица ---
        grp_id = QGroupBox('Идентификация')
        fl = QFormLayout(grp_id)
        self._ed_name = QLineEdit(name)
        self._cb_unit = QComboBox()
        self._cb_unit.setEditable(True)
        for u in PG_UNITS:
            self._cb_unit.addItem(u)
        idx = self._cb_unit.findText(unit)
        if idx >= 0:
            self._cb_unit.setCurrentIndex(idx)
        else:
            self._cb_unit.setCurrentText(unit)
        fl.addRow('Имя канала:', self._ed_name)
        fl.addRow('Единица:', self._cb_unit)
        vl.addWidget(grp_id)

        # --- Прямая калибровка A, B ---
        grp_ab = QGroupBox('Y_phys = A · raw + B')
        fl2 = QFormLayout(grp_ab)
        self._sb_coeff  = _dsb(coeff,  -1e12, 1e12, 8)
        self._sb_offset = _dsb(offset, -1e12, 1e12, 6)
        fl2.addRow('Коэффициент A:', self._sb_coeff)
        fl2.addRow('Смещение B:',    self._sb_offset)

        # Предварительный просмотр
        self._lbl_preview = QLabel()
        self._lbl_preview.setStyleSheet('color:#555; font-size:10px;')
        fl2.addRow('', self._lbl_preview)
        self._sb_coeff.valueChanged.connect(self._update_preview)
        self._sb_offset.valueChanged.connect(self._update_preview)
        vl.addWidget(grp_ab)

        # --- Калибровка по двум точкам ---
        grp_2pt = QGroupBox('Калибровка по двум точкам')
        grp_2pt.setCheckable(True)
        grp_2pt.setChecked(False)
        self._grp_2pt = grp_2pt
        gl = QVBoxLayout(grp_2pt)
        gl.setSpacing(4)

        def _row(label):
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(QLabel(label))
            raw_sb = _dsb(0, -1e15, 1e15, 4)
            phy_sb = _dsb(0, -1e15, 1e15, 4)
            h.addWidget(QLabel('raw ='))
            h.addWidget(raw_sb)
            h.addWidget(QLabel('→ phys ='))
            h.addWidget(phy_sb)
            return w, raw_sb, phy_sb

        row1, self._r1, self._p1 = _row('Точка 1:')
        row2, self._r2, self._p2 = _row('Точка 2:')
        gl.addWidget(row1)
        gl.addWidget(row2)

        btn_calc = QPushButton('Вычислить A и B')
        btn_calc.clicked.connect(self._calc_2pt)
        gl.addWidget(btn_calc)
        vl.addWidget(grp_2pt)

        # --- Кнопки OK / Отмена / Сброс ---
        btn_reset = QPushButton('Сброс (A=1, B=0)')
        btn_reset.clicked.connect(self._reset_calib)

        bbox = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bbox.accepted.connect(self.accept)
        bbox.rejected.connect(self.reject)

        h_bot = QHBoxLayout()
        h_bot.addWidget(btn_reset)
        h_bot.addStretch()
        h_bot.addWidget(bbox)
        vl.addLayout(h_bot)

        self._update_preview()

    def _update_preview(self):
        A = self._sb_coeff.value()
        B = self._sb_offset.value()
        unit = self._cb_unit.currentText().strip() or '?'
        self._lbl_preview.setText(
            f'raw=0 → {B:.4g} {unit};  raw=1000 → {1000*A+B:.4g} {unit}'
        )

    def _calc_2pt(self):
        r1 = self._r1.value();  p1 = self._p1.value()
        r2 = self._r2.value();  p2 = self._p2.value()
        if abs(r2 - r1) < 1e-15:
            QMessageBox.warning(self, 'Ошибка',
                                'raw-значения точек должны отличаться.')
            return
        A = (p2 - p1) / (r2 - r1)
        B = p1 - A * r1
        self._sb_coeff.setValue(A)
        self._sb_offset.setValue(B)

    def _reset_calib(self):
        self._sb_coeff.setValue(1.0)
        self._sb_offset.setValue(0.0)

    # --- Результат ---

    def get_name(self) -> str:
        return self._ed_name.text().strip()

    def get_unit(self) -> str:
        return self._cb_unit.currentText().strip()

    def get_coeff(self) -> float:
        return self._sb_coeff.value()

    def get_offset(self) -> float:
        return self._sb_offset.value()


def _dsb(value: float, lo: float, hi: float, decimals: int) -> QDoubleSpinBox:
    sb = QDoubleSpinBox()
    sb.setRange(lo, hi)
    sb.setDecimals(decimals)
    sb.setValue(value)
    sb.setSingleStep(0.1)
    sb.setFixedWidth(110)
    return sb
