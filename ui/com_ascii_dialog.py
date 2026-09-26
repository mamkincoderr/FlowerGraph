"""
Диалог настройки COM-ASCII источника данных.
Включает предпросмотр входящих данных.
"""

import time
import serial
import serial.tools.list_ports

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QComboBox, QSpinBox, QDialogButtonBox, QGroupBox,
    QTextEdit, QLabel, QPushButton, QCheckBox, QSizePolicy,
    QFrame
)
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont, QColor, QFontMetrics

from plugins.com_ascii_source import ComAsciiConfig


# ---------------------------------------------------------------------------
# Таблица скоростей
#
# Точность для CH32V303 @ APB2 = 144 МГц (USART1):
#   'exact'  — ошибка   0%     (дробный делитель компенсирует точно)
#   'good'   — ошибка < 1%     (0.16% у ряда 921600/1.8432М)
#   'limit'  — ошибка < 2.5%  (7.3728 М: 2.34%)
#   'bad'    — ошибка ≥ 2.5%
#
# Ряды:
#   Стандартный RS-232: 50…230400 — все 0% при 144 МГц
#   Ряд 1.8432 МГц × 2^n: 460800, 921600, 1843200, 3686400 — 0.16%
#   Высокоскоростные: делители от 144 МГц — точно
#   OVS8: ≥ 10 МБит/с требуют USART_OverSampling8Cmd() в прошивке
# ---------------------------------------------------------------------------

BAUD_TABLE = [
    # (baud,         display,                       rel,     note)
    # ─── стандартный ряд RS-232 ───────────────────────────────────
    (50,           '50',                           'exact',  ''),
    (75,           '75',                           'exact',  ''),
    (110,          '110',                          'exact',  ''),
    (134,          '134',                          'exact',  ''),
    (150,          '150',                          'exact',  ''),
    (200,          '200',                          'exact',  ''),
    (300,          '300',                          'exact',  ''),
    (600,          '600',                          'exact',  ''),
    (1_200,        '1 200',                        'exact',  ''),
    (1_800,        '1 800',                        'exact',  ''),
    (2_400,        '2 400',                        'exact',  ''),
    (4_800,        '4 800',                        'exact',  ''),
    (9_600,        '9 600',                        'exact',  ''),
    (14_400,       '14 400',                       'exact',  ''),
    (19_200,       '19 200',                       'exact',  ''),
    (28_800,       '28 800',                       'exact',  ''),
    (38_400,       '38 400',                       'exact',  ''),
    (57_600,       '57 600',                       'exact',  ''),
    (76_800,       '76 800',                       'exact',  ''),
    (115_200,      '115 200',                      'exact',  ''),
    (230_400,      '230 400',                      'exact',  ''),
    # ─── ряд 1.8432 МГц × 2ⁿ ────────────────────────────────────
    (460_800,      '460 800',                      'good',   '0.16%'),
    (921_600,      '921 600',                      'good',   '0.16%'),
    (1_843_200,    '1 843 200',                    'good',   '0.16%'),
    (3_686_400,    '3 686 400',                    'good',   '0.16%'),
    (7_372_800,    '7 372 800',                    'limit',  '2.34%'),
]

_REL_LABEL = {
    'exact': '✓ 0%',
    'good':  '≈',
    'limit': '~',
    'bad':   '✗',
}
_REL_COLOR = {
    'exact': '#006600',
    'good':  '#0055cc',
    'limit': '#cc7700',
    'bad':   '#cc0000',
}


class ComAsciiDialog(QDialog):
    """Диалог настройки и предпросмотра COM-ASCII источника."""

    def __init__(self, config: ComAsciiConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Источник данных — COM-ASCII')
        self.setMinimumWidth(870)
        self.setMinimumHeight(460)

        self._config     = config
        self._preview_port: serial.Serial | None = None
        self._preview_buf = bytearray()
        self._preview_timer = QTimer()
        self._preview_timer.timeout.connect(self._read_preview)

        self._build_ui()
        self._load_config(config)

    # ------------------------------------------------------------------
    # Построение интерфейса
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(8, 8, 8, 8)

        # ── Параметры порта ──────────────────────────────────────────
        grp = QGroupBox('Параметры порта')
        form = QFormLayout(grp)
        form.setSpacing(5)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        # Порт
        port_row = QHBoxLayout()
        port_row.setSpacing(4)
        self._cb_port = QComboBox()
        self._cb_port.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        port_row.addWidget(self._cb_port)
        btn_ref = QPushButton('↺')
        btn_ref.setFixedSize(26, 26)
        btn_ref.setToolTip('Обновить список портов')
        btn_ref.clicked.connect(self._refresh_ports)
        port_row.addWidget(btn_ref)
        form.addRow('Порт:', port_row)

        # Скорость — выпадающий список
        self._cb_baud = QComboBox()
        self._cb_baud.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        for baud, display, rel, note in BAUD_TABLE:
            suffix = f'  ({note})' if note else ''
            self._cb_baud.addItem(f'{display}{suffix}', baud)
        self._cb_baud.currentIndexChanged.connect(self._on_baud_changed)
        form.addRow('Скорость:', self._cb_baud)

        # Информация о точности
        self._lbl_baud_info = QLabel()
        self._lbl_baud_info.setWordWrap(False)
        f8 = QFont(); f8.setPointSize(8)
        self._lbl_baud_info.setFont(f8)
        form.addRow('', self._lbl_baud_info)

        # Ручной ввод скорости
        custom_row = QHBoxLayout()
        custom_row.setSpacing(6)
        self._chk_custom = QCheckBox('Задать вручную:')
        self._chk_custom.setFixedWidth(130)
        self._chk_custom.toggled.connect(self._on_custom_toggled)
        self._sb_custom = QSpinBox()
        self._sb_custom.setRange(50, 20_000_000)
        self._sb_custom.setSingleStep(100)
        self._sb_custom.setValue(460_800)
        self._sb_custom.setEnabled(False)
        self._sb_custom.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._sb_custom.valueChanged.connect(self._on_custom_value_changed)
        self._lbl_custom_info = QLabel()
        self._lbl_custom_info.setFont(f8)
        custom_row.addWidget(self._chk_custom)
        custom_row.addWidget(self._sb_custom)
        custom_row.addWidget(self._lbl_custom_info)
        form.addRow('', custom_row)

        # Каналы
        ch_row = QHBoxLayout()
        ch_row.setSpacing(6)
        self._sb_ch = QSpinBox()
        self._sb_ch.setRange(0, 32)
        self._sb_ch.setSpecialValueText('Авто')
        self._sb_ch.setFixedWidth(70)
        ch_row.addWidget(self._sb_ch)
        ch_row.addWidget(QLabel('(0 = авто-определение по первому пакету)'))
        ch_row.addStretch()
        form.addRow('Каналов:', ch_row)

        root.addWidget(grp)

        # ── Предпросмотр ──────────────────────────────────────────────
        grp2 = QGroupBox('Предпросмотр')
        pv = QVBoxLayout(grp2)
        pv.setSpacing(4)

        ctrl = QHBoxLayout()
        self._btn_prev = QPushButton('▶  Открыть порт')
        self._btn_prev.setFixedWidth(130)
        self._btn_prev.clicked.connect(self._toggle_preview)
        ctrl.addWidget(self._btn_prev)

        self._lbl_prev_st = QLabel('Не подключено')
        self._lbl_prev_st.setStyleSheet('color:#888;')
        ctrl.addWidget(self._lbl_prev_st, stretch=1)

        btn_clr = QPushButton('Очистить')
        btn_clr.setFixedWidth(80)
        ctrl.addWidget(btn_clr)
        pv.addLayout(ctrl)

        self._prev_txt = QTextEdit()
        self._prev_txt.setReadOnly(True)
        self._prev_txt.setMinimumHeight(140)
        mono = QFont('Courier New')
        mono.setPointSize(8)
        self._prev_txt.setFont(mono)
        self._prev_txt.setStyleSheet('background:#1c1c1c; color:#d4d4d4; border:1px solid #555;')
        btn_clr.clicked.connect(self._prev_txt.clear)
        pv.addWidget(self._prev_txt)

        root.addWidget(grp2, stretch=1)

        # ── Кнопки ────────────────────────────────────────────────────
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        self._refresh_ports()

    # ------------------------------------------------------------------
    # Загрузка конфига
    # ------------------------------------------------------------------

    def _load_config(self, cfg: ComAsciiConfig):
        # Порт
        for i in range(self._cb_port.count()):
            if self._cb_port.itemData(i) == cfg.port:
                self._cb_port.setCurrentIndex(i)
                break

        # Скорость — ищем в таблице
        found = False
        for i in range(self._cb_baud.count()):
            if self._cb_baud.itemData(i) == cfg.baudrate:
                self._cb_baud.setCurrentIndex(i)
                found = True
                break
        if not found:
            # Нестандартная скорость — включаем ручной ввод
            self._chk_custom.setChecked(True)
            self._sb_custom.setValue(cfg.baudrate)

        self._sb_ch.setValue(cfg.n_channels)
        self._update_baud_info(cfg.baudrate)

    # ------------------------------------------------------------------
    # Скорость
    # ------------------------------------------------------------------

    def _on_baud_changed(self):
        if not self._chk_custom.isChecked():
            baud = self._cb_baud.currentData()
            if baud:
                self._update_baud_info(baud)

    def _on_custom_toggled(self, checked: bool):
        self._sb_custom.setEnabled(checked)
        self._cb_baud.setEnabled(not checked)
        if checked:
            self._update_baud_info(self._sb_custom.value())
        else:
            baud = self._cb_baud.currentData()
            if baud:
                self._update_baud_info(baud)

    def _on_custom_value_changed(self, val: int):
        if self._chk_custom.isChecked():
            self._update_baud_info(val)
            # Подсказка о точности
            rate = self._compute_accuracy(val)
            self._lbl_custom_info.setText(rate)

    def _compute_accuracy(self, target: int) -> str:
        PCLK = 144_000_000
        best_err = 999.0
        best_ovs = 16
        for ovs in (16, 8):
            fd = 16 if ovs == 16 else 8
            d = PCLK / (ovs * target)
            if d < 1:
                continue
            m = int(d)
            f = round((d - m) * fd)
            if f >= fd:
                m += 1; f = 0
            da = m + f / fd
            act = PCLK / (ovs * da)
            err = abs(act - target) / target * 100
            if err < best_err:
                best_err = err
                best_ovs = ovs
        if best_err > 100:
            return '— (не поддерживается)'
        ovs_note = '  [нужен OVS8]' if best_ovs == 8 else ''
        return f'CH32V303: ≈{best_err:.2f}%{ovs_note}'

    def _update_baud_info(self, baud: int):
        # Ищем в таблице
        for b, _, rel, note in BAUD_TABLE:
            if b == baud:
                n_ch_est = self._sb_ch.value() or 4
                pkt_rate = int(baud / ((n_ch_est * 8 + 1) * 10))
                err_str  = f' ({note})' if note else ' (0%)'
                ovs_note = '  [OVS8]' if baud >= 10_000_000 else ''
                color    = _REL_COLOR.get(rel, '#888')
                self._lbl_baud_info.setText(
                    f'<span style="color:{color}">CH32V303 @ 144МГц:{err_str}{ovs_note}'
                    f'</span>  —  ~{pkt_rate:,} пакетов/с (4 канала)'
                )
                return
        # Нестандартная скорость
        n_ch_est = self._sb_ch.value() or 4
        pkt_rate = int(baud / ((n_ch_est * 8 + 1) * 10))
        acc = self._compute_accuracy(baud)
        self._lbl_baud_info.setText(
            f'{acc}  —  ~{pkt_rate:,} пакетов/с'
        )

    def current_baudrate(self) -> int:
        if self._chk_custom.isChecked():
            return self._sb_custom.value()
        return self._cb_baud.currentData() or 460_800

    # ------------------------------------------------------------------
    # Порты
    # ------------------------------------------------------------------

    def _refresh_ports(self):
        prev = self._cb_port.currentData()
        self._cb_port.clear()
        ports = sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)
        for p in ports:
            desc = f'{p.device}  —  {p.description}'
            self._cb_port.addItem(desc, p.device)
        if not ports:
            self._cb_port.addItem('(нет доступных портов)', None)
        # Восстанавливаем выбор
        restored = False
        for i in range(self._cb_port.count()):
            if self._cb_port.itemData(i) == (prev or self._config.port):
                self._cb_port.setCurrentIndex(i)
                restored = True
                break
        if not restored and self._cb_port.count() > 0:
            self._cb_port.setCurrentIndex(0)

    # ------------------------------------------------------------------
    # Предпросмотр
    # ------------------------------------------------------------------

    def _toggle_preview(self):
        if self._preview_port and self._preview_port.is_open:
            self._stop_preview()
        else:
            self._start_preview()

    def _start_preview(self):
        port = self._cb_port.currentData()
        baud = self.current_baudrate()
        if not port:
            self._prev_log('Порт не выбран', '#f66')
            return
        try:
            self._preview_port = serial.Serial(
                port=port, baudrate=baud,
                bytesize=8, parity='N', stopbits=1, timeout=0.05,
            )
            self._preview_buf.clear()
            self._preview_timer.start(40)
            self._btn_prev.setText('■  Закрыть порт')
            self._lbl_prev_st.setText(f'{port}  @  {baud:,} бод')
            self._lbl_prev_st.setStyleSheet('color:#2ecc71; font-weight:bold;')
            self._prev_log(f'Подключено: {port} @ {baud:,}', '#2ecc71')
        except serial.SerialException as e:
            self._prev_log(f'Ошибка: {e}', '#f66')

    def _stop_preview(self):
        self._preview_timer.stop()
        if self._preview_port:
            try:
                self._preview_port.close()
            except Exception:
                pass
            self._preview_port = None
        self._btn_prev.setText('▶  Открыть порт')
        self._lbl_prev_st.setText('Не подключено')
        self._lbl_prev_st.setStyleSheet('color:#888;')

    def _read_preview(self):
        if not self._preview_port or not self._preview_port.is_open:
            return
        try:
            n = self._preview_port.in_waiting
            if n:
                self._preview_buf.extend(self._preview_port.read(n))
                self._flush_preview()
        except serial.SerialException as e:
            self._prev_log(f'Ошибка: {e}', '#f66')
            self._stop_preview()

    def _flush_preview(self):
        while True:
            cr = self._preview_buf.find(0x0D)
            if cr < 0:
                if len(self._preview_buf) > 512:
                    self._preview_buf.clear()
                break
            line = bytes(self._preview_buf[:cr])
            skip = cr + 1
            if skip < len(self._preview_buf) and self._preview_buf[skip] == 0x0A:
                skip += 1
            del self._preview_buf[:skip]
            if line:
                self._show_line(line)

    def _show_line(self, raw: bytes):
        n    = len(raw)
        n_ch = n // 8 if n % 8 == 0 else 0
        asc  = ''.join(chr(b) if 32 <= b < 127 else '·' for b in raw)

        if n_ch > 0:
            vals, ok = [], True
            for i in range(n_ch):
                seg = raw[i*8:(i+1)*8]
                try:
                    sign = seg[0]
                    val  = float(bytes([seg[1],seg[2],seg[3],seg[4],seg[5],seg[6]]))
                    if sign == ord('-'):
                        val = -val
                    vals.append(f'CH{i+1}={val:9.1f}')
                except Exception:
                    ok = False; break
            if ok:
                parsed = '  '.join(vals)
                self._prev_log(f'{asc}   →   {parsed}', '#9cdcfe')
                return
        self._prev_log(asc, '#ce9178')

    def _prev_log(self, text: str, color: str = '#d4d4d4'):
        from PySide6.QtGui import QTextCursor
        c = self._prev_txt.textCursor()
        c.movePosition(QTextCursor.End)
        self._prev_txt.setTextCursor(c)
        self._prev_txt.setTextColor(QColor(color))
        self._prev_txt.insertPlainText(text + '\n')
        self._prev_txt.ensureCursorVisible()
        doc = self._prev_txt.document()
        while doc.blockCount() > 120:
            c = self._prev_txt.textCursor()
            c.movePosition(QTextCursor.Start)
            c.select(QTextCursor.BlockUnderCursor)
            c.removeSelectedText()
            c.deleteChar()

    # ------------------------------------------------------------------
    # OK / Cancel
    # ------------------------------------------------------------------

    def _on_ok(self):
        self._stop_preview()
        self.accept()

    def get_config(self) -> ComAsciiConfig:
        return ComAsciiConfig(
            port       = self._cb_port.currentData() or 'COM4',
            baudrate   = self.current_baudrate(),
            n_channels = self._sb_ch.value(),
        )

    def reject(self):
        self._stop_preview()
        super().reject()

    def closeEvent(self, event):
        self._stop_preview()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Утилиты
    # ------------------------------------------------------------------

    @staticmethod
    def supported_bauds() -> list[int]:
        return [b for b, _, _, _ in BAUD_TABLE]
