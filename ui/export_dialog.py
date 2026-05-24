"""
Диалог экспорта данных (аналог PG «Сохранить блок...»).

Поддерживаемые форматы: CSV / TXT / TSV
Параметры (как в PowerGraph):
  - Источник: весь блок / видимый диапазон
  - Каналы: выбор подмножества
  - Прореживание: каждый N-й отсчёт
  - Разделитель: Tab / Запятая / Точка-с-запятой
  - Заголовок: имена каналов
  - Колонка времени: да/нет + единицы (с / мс / мкс)
"""

import numpy as np
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QGroupBox, QRadioButton, QCheckBox, QSpinBox,
    QComboBox, QLabel, QDialogButtonBox,
    QButtonGroup, QScrollArea, QWidget, QSizePolicy
)
from PySide6.QtCore import Qt

from core.session import Block


# ---------------------------------------------------------------------------

class ExportCsvDialog(QDialog):

    def __init__(self, block: Block,
                 view_range: tuple[float, float] | None = None,
                 parent=None):
        super().__init__(parent)
        self._block      = block
        self._view_range = view_range       # (t_min, t_max) текущего окна

        self.setWindowTitle('Экспорт данных в текстовый файл')
        self.setMinimumWidth(420)
        self._build_ui()

    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        # ── Источник данных ──────────────────────────────────────────
        grp_src = QGroupBox('Данные')
        src_v = QVBoxLayout(grp_src)
        self._bg_src = QButtonGroup(self)

        self._rb_all  = QRadioButton(
            f'Весь блок  ({self._block.n_samples:,} отсчётов,'
            f' {self._block.duration:.3f} с)'
        )
        self._rb_view = QRadioButton('Только видимый диапазон')
        self._rb_view.setEnabled(self._view_range is not None)

        self._bg_src.addButton(self._rb_all,  0)
        self._bg_src.addButton(self._rb_view, 1)
        self._rb_all.setChecked(True)
        src_v.addWidget(self._rb_all)
        src_v.addWidget(self._rb_view)

        if self._view_range:
            t0, t1 = self._view_range
            n_vis = int(np.searchsorted(self._block.times, t1)) \
                  - int(np.searchsorted(self._block.times, t0))
            lbl = QLabel(f'    ({n_vis:,} отсчётов,  {t1-t0:.4g} с)')
            lbl.setStyleSheet('color:#555; font-size:9px;')
            src_v.addWidget(lbl)

        root.addWidget(grp_src)

        # ── Каналы ───────────────────────────────────────────────────
        grp_ch = QGroupBox('Каналы')
        ch_v = QVBoxLayout(grp_ch)

        sel_h = QHBoxLayout()
        btn_all = QLabel('<a href="#">Все</a>')
        btn_all.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        btn_all.linkActivated.connect(lambda: self._set_all_channels(True))
        btn_none = QLabel('<a href="#">Ни одного</a>')
        btn_none.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        btn_none.linkActivated.connect(lambda: self._set_all_channels(False))
        sel_h.addWidget(QLabel('Выбрать:'))
        sel_h.addWidget(btn_all)
        sel_h.addWidget(btn_none)
        sel_h.addStretch()
        ch_v.addLayout(sel_h)

        self._ch_boxes: list[QCheckBox] = []
        for i, ch in enumerate(self._block.channels):
            cb = QCheckBox(f'CH{i+1}: {ch.name}' if ch.name != f'CH{i+1}' else f'CH{i+1}')
            cb.setChecked(True)
            ch_v.addWidget(cb)
            self._ch_boxes.append(cb)

        root.addWidget(grp_ch)

        # ── Прореживание ─────────────────────────────────────────────
        grp_dec = QGroupBox('Прореживание')
        dec_form = QFormLayout(grp_dec)

        dec_h = QHBoxLayout()
        self._sb_dec = QSpinBox()
        self._sb_dec.setRange(1, 10_000)
        self._sb_dec.setValue(1)
        self._sb_dec.setFixedWidth(80)
        self._sb_dec.valueChanged.connect(self._update_row_count)
        dec_h.addWidget(QLabel('Каждый'))
        dec_h.addWidget(self._sb_dec)
        dec_h.addWidget(QLabel('-й отсчёт  (1 = без прореживания)'))
        dec_h.addStretch()
        dec_form.addRow('', dec_h)

        self._lbl_rows = QLabel()
        self._lbl_rows.setStyleSheet('color:#555; font-size:9px;')
        dec_form.addRow('', self._lbl_rows)
        self._update_row_count()

        root.addWidget(grp_dec)

        # ── Формат ───────────────────────────────────────────────────
        grp_fmt = QGroupBox('Формат')
        fmt_form = QFormLayout(grp_fmt)

        # Десятичный разделитель
        dec_h = QHBoxLayout()
        self._bg_dec = QButtonGroup(self)
        self._rb_dot   = QRadioButton('Точка  1.234')
        self._rb_comma = QRadioButton('Запятая  1,234  (авто → разделитель ;)')
        self._rb_dot.setChecked(True)
        self._bg_dec.addButton(self._rb_dot,   0)
        self._bg_dec.addButton(self._rb_comma, 1)
        self._rb_comma.toggled.connect(self._on_decimal_changed)
        dec_h.addWidget(self._rb_dot)
        dec_h.addWidget(self._rb_comma)
        dec_h.addStretch()
        fmt_form.addRow('Десятичный:', dec_h)

        self._cb_sep = QComboBox()
        self._cb_sep.addItem('Запятая  (*.csv)',           ',')
        self._cb_sep.addItem('Табуляция  (*.txt, *.tsv)', '\t')
        self._cb_sep.addItem('Точка с запятой  (*.csv)',   ';')
        self._cb_sep.setCurrentIndex(0)
        fmt_form.addRow('Разделитель столбцов:', self._cb_sep)

        self._chk_header = QCheckBox('Строка заголовка (имена каналов)')
        self._chk_header.setChecked(True)
        fmt_form.addRow('', self._chk_header)

        self._chk_time = QCheckBox('Колонка времени')
        self._chk_time.setChecked(True)
        fmt_form.addRow('', self._chk_time)

        time_h = QHBoxLayout()
        self._cb_time_unit = QComboBox()
        self._cb_time_unit.addItem('Секунды (0.001234)',      's')
        self._cb_time_unit.addItem('Миллисекунды (1.234)',    'ms')
        self._cb_time_unit.addItem('Микросекунды (1234.0)',   'us')
        time_h.addWidget(self._cb_time_unit)
        time_h.addStretch()
        fmt_form.addRow('Формат времени:', time_h)
        self._chk_time.toggled.connect(self._cb_time_unit.setEnabled)

        root.addWidget(grp_fmt)

        # ── Кнопки ───────────────────────────────────────────────────
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText('Сохранить…')
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    # ------------------------------------------------------------------

    def _set_all_channels(self, state: bool):
        for cb in self._ch_boxes:
            cb.setChecked(state)

    def _on_decimal_changed(self, comma_checked: bool):
        # При выборе запятой как десятичного — переключить разделитель на ;
        if comma_checked:
            for i in range(self._cb_sep.count()):
                if self._cb_sep.itemData(i) == ';':
                    self._cb_sep.setCurrentIndex(i)
                    break

    def _update_row_count(self):
        step  = self._sb_dec.value()
        total = self._block.n_samples
        if self._view_range and self._rb_view.isChecked():
            t0, t1 = self._view_range
            n0 = int(np.searchsorted(self._block.times, t0))
            n1 = int(np.searchsorted(self._block.times, t1))
            total = n1 - n0
        rows = max(1, (total + step - 1) // step)
        self._lbl_rows.setText(
            f'Будет записано: {rows:,} строк'
            f'  ({total:,} / {step} = {rows:,})'
        )

    # ------------------------------------------------------------------

    def get_settings(self) -> dict:
        return {
            'range':        'view' if self._rb_view.isChecked() else 'all',
            'channels':     [cb.isChecked() for cb in self._ch_boxes],
            'decimation':   self._sb_dec.value(),
            'separator':    self._cb_sep.currentData(),
            'decimal':      ',' if self._rb_comma.isChecked() else '.',
            'header':       self._chk_header.isChecked(),
            'time_col':     self._chk_time.isChecked(),
            'time_unit':    self._cb_time_unit.currentData(),
        }


# ---------------------------------------------------------------------------
# Функция экспорта CSV
# ---------------------------------------------------------------------------

def export_csv(block: Block,
               filename: str,
               settings: dict,
               view_range: tuple[float, float] | None = None) -> int:
    """
    Экспорт блока в текстовый файл.
    Возвращает количество записанных строк.
    """
    t = block.times.astype(np.float64)
    v = block.values.astype(np.float64)

    # Диапазон
    if settings['range'] == 'view' and view_range:
        t0, t1   = view_range
        i0       = int(np.searchsorted(t, t0))
        i1       = int(np.searchsorted(t, t1)) + 1
        i0, i1   = max(0, i0), min(len(t), i1)
        t, v     = t[i0:i1], v[i0:i1]

    # Прореживание
    step = max(1, settings['decimation'])
    t = t[::step]
    v = v[::step]

    # Масштаб времени
    t_scale = {'s': 1.0, 'ms': 1e3, 'us': 1e6}.get(settings['time_unit'], 1.0)
    t_col   = t * t_scale

    # Выбранные каналы
    ch_mask = settings['channels']
    ch_idx  = [i for i, sel in enumerate(ch_mask) if sel and i < block.n_channels]

    # Сборка данных
    cols = []
    col_names = []
    if settings['time_col']:
        cols.append(t_col)
        unit_lbl = {'s': 'Time_s', 'ms': 'Time_ms', 'us': 'Time_us'}.get(
            settings['time_unit'], 'Time'
        )
        col_names.append(unit_lbl)
    for i in ch_idx:
        cols.append(v[:, i])
        col_names.append(block.channels[i].name if block.channels else f'CH{i+1}')

    if not cols:
        return 0

    data = np.column_stack(cols)
    sep  = settings['separator']

    header = sep.join(col_names) if settings['header'] else ''

    # Формат чисел: 8 значащих цифр
    fmt_t = '%.8g'
    fmt_v = '%.6g'
    fmts  = []
    if settings['time_col']:
        fmts.append(fmt_t)
    fmts.extend([fmt_v] * len(ch_idx))

    np.savetxt(
        filename, data,
        delimiter=sep,
        header=header,
        comments='',
        fmt=fmts,
        encoding='utf-8',
    )

    # Замена десятичной точки на запятую (постобработка текста)
    if settings.get('decimal', '.') == ',':
        import re
        path = Path(filename)
        text = path.read_text(encoding='utf-8')
        # Заменяем только точки внутри числовых токенов (не в разделителях)
        text = re.sub(r'(?<=\d)\.(?=\d)', ',', text)
        path.write_text(text, encoding='utf-8')

    return len(t)
