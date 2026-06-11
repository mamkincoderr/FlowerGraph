"""
COM-COBS источник данных — бинарный протокол с COBS-кодированием.

Структура пакета (конфигурируется в диалоге):

  [COUNT : 1 байт]?  [CH0 … CHN : bps байт каждый]  [CRC_L CRC_H : 2 байта]?
   has_count=True                                       use_crc=True

  bps = 2 (int16/uint16) | 4 (int32/uint32/float32)
  COBS-кодирование + финальный 0x00 (делимитер кадра)

  CRC-16/CCITT: poly=0x1021, init=0xFFFF — по [COUNT?][данные]
  COUNT: uint8, wrapping 0→255→0, детектор потерь пакетов
"""

import math
import queue
import struct
import threading
import time
from dataclasses import dataclass, field

import numpy as np
import serial
from PySide6.QtCore import QTimer, Qt, QRect
from PySide6.QtGui  import QPainter, QPen, QColor, QFont, QBrush
from PySide6.QtWidgets import (
    QWidget, QGroupBox, QVBoxLayout, QHBoxLayout, QFormLayout,
    QCheckBox, QComboBox, QLabel, QSizePolicy,
)

from plugins.base_source import BaseSource
from ui.com_ascii_dialog import ComAsciiDialog
from plugins.com_ascii_source import ComAsciiConfig


# ---------------------------------------------------------------------------
# Вспомогательные функции — типы данных
# ---------------------------------------------------------------------------

DATA_FORMATS = ['int16', 'uint16', 'int32', 'uint32', 'float32']

_FMT_LABELS = {
    'int16':   'int16   · 2 б · ±32 767',
    'uint16':  'uint16  · 2 б · 0…65 535',
    'int32':   'int32   · 4 б · ±2 147 M',
    'uint32':  'uint32  · 4 б · 0…4 294 M',
    'float32': 'float32 · 4 б · IEEE 754',
}

_FMT_SHORT = {
    'int16': 'i16', 'uint16': 'u16',
    'int32': 'i32', 'uint32': 'u32', 'float32': 'f32',
}

_STRUCT_FMT = {
    'int16': '<h', 'uint16': '<H',
    'int32': '<i', 'uint32': '<I', 'float32': '<f',
}


def _bps(fmt: str) -> int:
    """Байт на сэмпл (bytes per sample)."""
    return 4 if fmt in ('int32', 'uint32', 'float32') else 2


# ---------------------------------------------------------------------------
# CRC-16/CCITT (poly 0x1021, init 0xFFFF)
# ---------------------------------------------------------------------------

def _crc16(data: bytes | bytearray) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
        crc &= 0xFFFF
    return crc


# ---------------------------------------------------------------------------
# COBS decode
# ---------------------------------------------------------------------------

def _cobs_decode(data: bytes) -> bytes | None:
    """COBS-декодирование блока без финального 0x00."""
    result = bytearray()
    idx = 0
    n   = len(data)
    while idx < n:
        code = data[idx]
        if code == 0:
            return None
        idx += 1
        end  = idx + code - 1
        if end > n:
            return None
        result.extend(data[idx:end])
        idx = end
        if code != 0xFF and idx < n:
            result.append(0x00)
    return bytes(result)


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

@dataclass
class ComCobsConfig:
    port:          str             = 'COM4'
    baudrate:      int             = 460800
    n_channels:    int             = 0           # 0 = авто
    has_count:     bool            = True        # байт COUNT присутствует
    use_crc:       bool            = True        # CRC-16 в конце пакета
    data_format:   str             = 'int16'     # тип данных каналов
    channel_names: list[str] | None = field(default=None)

    def to_ascii_config(self) -> ComAsciiConfig:
        return ComAsciiConfig(
            port=self.port, baudrate=self.baudrate,
            n_channels=self.n_channels, channel_names=self.channel_names,
        )

    @classmethod
    def from_ascii_config(cls, cfg: ComAsciiConfig) -> 'ComCobsConfig':
        return cls(port=cfg.port, baudrate=cfg.baudrate, n_channels=cfg.n_channels)

    def to_dict(self) -> dict:
        return {
            'port': self.port, 'baudrate': self.baudrate,
            'n_channels': self.n_channels,
            'has_count': self.has_count, 'use_crc': self.use_crc,
            'data_format': self.data_format,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'ComCobsConfig':
        return cls(
            port=d.get('port', 'COM4'),
            baudrate=d.get('baudrate', 460800),
            n_channels=d.get('n_channels', 0),
            has_count=d.get('has_count', True),
            use_crc=d.get('use_crc', True),
            data_format=d.get('data_format', 'int16'),
        )


# ---------------------------------------------------------------------------
# PacketDiagram — визуальная диаграмма пакета
# ---------------------------------------------------------------------------

class PacketDiagram(QWidget):
    """Динамическая цветная диаграмма структуры COBS/mCOBS пакета."""

    _BOX_H   = 54     # высота поля
    _MIN_W   = 36     # минимальная ширина поля, px
    _MARGIN  = 8
    _BG      = QColor('#1a1c2a')

    # Цвета полей
    _C_COUNT = QColor('#3d5fa8')
    _C_CH    = (QColor('#2e7d4f'), QColor('#256640'))
    _C_CRC   = QColor('#9e3030')
    _C_DELIM = QColor('#404050')
    _C_BATCH = QColor('#2d6b8a')

    def __init__(self, parent=None):
        super().__init__(parent)
        self._has_count   = True
        self._use_crc     = True
        self._data_format = 'int16'
        self._n_ch        = 4
        self._batch       = 1
        self.setMinimumHeight(80)
        self.setMaximumHeight(80)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_params(self, has_count: bool, use_crc: bool,
                   data_format: str, n_ch: int, batch: int = 1):
        self._has_count   = has_count
        self._use_crc     = use_crc
        self._data_format = data_format if data_format in DATA_FORMATS else 'int16'
        self._n_ch        = max(n_ch, 1)
        self._batch       = max(batch, 1)
        self.update()

    # ------------------------------------------------------------------
    # Построение списка полей
    # ------------------------------------------------------------------

    def _build_fields(self) -> list[tuple[str, str, int, QColor]]:
        """Возвращает список (name, sub, bytes, color)."""
        bps = _bps(self._data_format)
        fsht = _FMT_SHORT[self._data_format]
        fields = []

        if self._has_count:
            fields.append(('CNT', 'u8', 1, self._C_COUNT))

        n, b = self._n_ch, self._batch
        if b * n <= 12:
            for k in range(b):
                for i in range(n):
                    lbl = f'CH{i+1}' if b == 1 else f'S{k}·{i+1}'
                    fields.append((lbl, fsht, bps, self._C_CH[i % 2]))
        else:
            total = b * n * bps
            lbl = f'{b}×{n}' if b > 1 else f'{n} кан.'
            fields.append((lbl, f'{fsht}\n{total}б', total, self._C_BATCH))

        if self._use_crc:
            fields.append(('CRC', 'u16', 2, self._C_CRC))

        fields.append(('0x00', 'delim', 1, self._C_DELIM))
        return fields

    # ------------------------------------------------------------------
    # Рендеринг
    # ------------------------------------------------------------------

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)

        W, H = self.width(), self.height()
        MG   = self._MARGIN
        BH   = self._BOX_H
        BY   = (H - BH - 14) // 2

        p.fillRect(self.rect(), self._BG)

        fields = self._build_fields()
        total_b = sum(f[2] for f in fields)
        avail   = W - 2 * MG

        # Ширины: пропорционально байтам, но не меньше _MIN_W
        raw_ws = [max(round(f[2] / total_b * avail), self._MIN_W) for f in fields]
        excess = sum(raw_ws) - avail
        if excess > 0:
            # Урезаем пропорционально, начиная с самых широких
            for _ in range(excess):
                idx = max(range(len(raw_ws)), key=lambda i: raw_ws[i])
                raw_ws[idx] -= 1

        fn_bold = QFont('Consolas', 8, QFont.Bold)
        fn_small = QFont('Consolas', 7)

        x = MG
        for (name, sub, size, color), fw in zip(fields, raw_ws):
            # Фон и рамка
            p.fillRect(x, BY, fw - 1, BH, color)
            p.setPen(QPen(QColor('#111'), 1))
            p.drawRect(x, BY, fw - 1, BH)

            inner = QRect(x + 2, BY + 2, fw - 4, BH - 4)

            # Название (жирный, верх)
            p.setFont(fn_bold)
            p.setPen(QColor('#ffffff'))
            p.drawText(inner, Qt.AlignTop | Qt.AlignHCenter, name)

            # Тип (мелкий, центр)
            if '\n' in sub:
                line1, line2 = sub.split('\n', 1)
                mid_r = QRect(inner.x(), inner.y() + inner.height()//2 - 9,
                              inner.width(), 10)
                mid_r2 = QRect(inner.x(), inner.y() + inner.height()//2 + 1,
                               inner.width(), 10)
                p.setFont(fn_small)
                p.setPen(QColor('#aaddcc'))
                p.drawText(mid_r, Qt.AlignHCenter, line1)
                p.drawText(mid_r2, Qt.AlignHCenter, line2)
            else:
                p.setFont(fn_small)
                p.setPen(QColor('#aaddcc'))
                p.drawText(inner, Qt.AlignVCenter | Qt.AlignHCenter, sub)

            # Байт (мелкий, низ)
            p.setPen(QColor('#cccccc'))
            p.drawText(inner, Qt.AlignBottom | Qt.AlignHCenter, f'{size}б')

            x += fw

        # Нижняя строка — итог
        raw_total = total_b - 1   # без 0x00
        enc_approx = raw_total + 2
        p.setFont(fn_small)
        p.setPen(QColor('#556'))
        p.drawText(MG, BY + BH + 12, f'raw: {raw_total} б   COBS+0x00: ~{enc_approx} б')

        p.end()


# ---------------------------------------------------------------------------
# Источник
# ---------------------------------------------------------------------------

class ComCobsSource(BaseSource):
    _DRAIN_MS = 15
    _RECAL_AT = 500

    def __init__(self, config: ComCobsConfig | None = None):
        super().__init__()
        self._config = config or ComCobsConfig()
        self._queue:  queue.Queue             = queue.Queue()
        self._port:   serial.Serial | None    = None
        self._thread: threading.Thread | None = None

        self._pkt_ok       = 0
        self._pkt_err_cobs = 0
        self._pkt_err_crc  = 0
        self._pkt_lost     = 0
        self._t_start      = 0.0
        self._sample_count = 0

        self._n_ch_detected = 0
        self._last_count    = -1

        self._rate_est = 0.0
        self._t_base   = 0.0

        self._reconnect_delay = 2.0
        self._reconnect_count = 0

        self._drain_timer = QTimer()
        self._drain_timer.timeout.connect(self._drain_queue)

    # ------------------------------------------------------------------

    def get_name(self) -> str:
        return f'COM-COBS ({self._config.port} {self._config.baudrate})'

    def get_channel_count(self) -> int:
        return self._n_ch_detected or self._config.n_channels or 4

    def get_channel_names(self) -> list[str]:
        n = self.get_channel_count()
        if self._config.channel_names and len(self._config.channel_names) >= n:
            return self._config.channel_names[:n]
        return [f'CH{i+1}' for i in range(n)]

    def get_config_widget(self):
        return None

    def start(self):
        try:
            self._port = serial.Serial(
                port=self._config.port, baudrate=self._config.baudrate,
                bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE, timeout=0.05,
            )
        except serial.SerialException as e:
            self._emit_error(f'Не удалось открыть {self._config.port}: {e}')
            return

        self._running       = True
        self._t_start       = time.perf_counter()
        self._sample_count  = 0
        self._pkt_ok        = 0
        self._pkt_err_cobs  = 0
        self._pkt_err_crc   = 0
        self._pkt_lost      = 0
        self._n_ch_detected = self._config.n_channels
        self._last_count    = -1
        self._rate_est      = self._rate_from_baud()
        self._t_base        = 0.0

        while not self._queue.empty():
            try: self._queue.get_nowait()
            except queue.Empty: break

        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        self._drain_timer.start(self._DRAIN_MS)

    def stop(self):
        self._running = False
        self._drain_timer.stop()
        if self._thread:
            self._thread.join(timeout=0.5)
            self._thread = None
        if self._port and self._port.is_open:
            try: self._port.close()
            except Exception: pass
        self._port = None
        self._drain_queue()

    def effective_sample_rate(self) -> int:
        return int(self._rate_est) if self._rate_est > 0 else 1000

    # ------------------------------------------------------------------

    def _rate_from_baud(self) -> float:
        n_ch    = self._n_ch_detected or self._config.n_channels or 4
        hdr     = 1 if self._config.has_count else 0
        ftr     = 2 if self._config.use_crc   else 0
        raw_len = hdr + ftr + n_ch * _bps(self._config.data_format)
        enc_len = raw_len + 2
        return self._config.baudrate / (enc_len * 10.0)

    def _calibrate_rate(self):
        elapsed = time.perf_counter() - self._t_start
        if elapsed < 1.0 or self._sample_count < 500:
            return
        measured = self._sample_count / elapsed
        new_rate = self._rate_est + 0.15 * (measured - self._rate_est)
        self._t_base  += self._sample_count * (1.0/self._rate_est - 1.0/new_rate)
        self._rate_est = new_rate

    # ------------------------------------------------------------------

    def _read_loop(self):
        buf = bytearray()
        while self._running:
            if self._port is None or not self._port.is_open:
                time.sleep(self._reconnect_delay)
                if not self._running:
                    break
                self._reconnect_count += 1
                self._emit_error(
                    f'Попытка реконнекта #{self._reconnect_count} ({self._config.port})…')
                try:
                    self._port = serial.Serial(
                        port=self._config.port, baudrate=self._config.baudrate,
                        bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                        stopbits=serial.STOPBITS_ONE, timeout=0.05,
                    )
                    buf.clear()
                    self._emit_error(f'Реконнект успешен ({self._config.port})')
                except serial.SerialException:
                    pass
                continue
            try:
                waiting = self._port.in_waiting
                if waiting:
                    buf.extend(self._port.read(waiting))
                    self._process_buf(buf)
                else:
                    time.sleep(0.001)
            except serial.SerialException as e:
                self._emit_error(f'Потеря связи: {e}')
                buf.clear()
                try: self._port.close()
                except Exception: pass
                self._port = None

    def _process_buf(self, buf: bytearray):
        while True:
            delim = buf.find(0x00)
            if delim < 0:
                if len(buf) > 512:
                    buf.clear()
                break
            cobs_data = bytes(buf[:delim])
            del buf[:delim + 1]
            if cobs_data:
                self._parse_packet(cobs_data)

    # ------------------------------------------------------------------

    def _parse_packet(self, cobs_data: bytes):
        raw = _cobs_decode(cobs_data)
        if raw is None:
            self._pkt_err_cobs += 1
            return

        rlen = len(raw)
        bps_val = _bps(self._config.data_format)
        hdr = 1 if self._config.has_count else 0
        ftr = 2 if self._config.use_crc   else 0

        payload_len = rlen - hdr - ftr
        if payload_len < bps_val or payload_len % bps_val != 0:
            self._pkt_err_cobs += 1
            return

        n_ch = payload_len // bps_val

        if self._n_ch_detected == 0:
            self._n_ch_detected = n_ch
            self._rate_est = self._rate_from_baud()

        if n_ch != self._n_ch_detected:
            self._pkt_err_cobs += 1
            return

        # CRC
        if self._config.use_crc:
            crc_rx   = struct.unpack_from('<H', raw, hdr + payload_len)[0]
            crc_calc = _crc16(raw[:hdr + payload_len])
            if crc_rx != crc_calc:
                self._pkt_err_crc += 1
                return

        # COUNT / потери
        if self._config.has_count:
            count = raw[0]
            if self._last_count >= 0:
                expected = (self._last_count + 1) & 0xFF
                if count != expected:
                    lost = (count - expected) & 0xFF
                    self._pkt_lost += lost
                    if lost:
                        self._emit_error(
                            f'COBS: потеряно {lost} пакетов '
                            f'(ожидался {expected}, получен {count})')
            self._last_count = count

        # Данные
        sfmt = _STRUCT_FMT[self._config.data_format]
        values = np.empty((1, n_ch), dtype=np.float32)
        for i in range(n_ch):
            v = struct.unpack_from(sfmt, raw, hdr + i * bps_val)[0]
            if self._config.data_format == 'float32' and not math.isfinite(v):
                v = 0.0
            values[0, i] = float(v)

        self._sample_count += 1
        t = self._t_base + self._sample_count / self._rate_est
        if self._sample_count % self._RECAL_AT == 0:
            self._calibrate_rate()

        self._queue.put((np.array([t], dtype=np.float64), values))
        self._pkt_ok += 1

    # ------------------------------------------------------------------

    def _drain_queue(self):
        while True:
            try:
                times, values = self._queue.get_nowait()
                self._emit(times, values)
            except queue.Empty:
                break

    @property
    def stats(self) -> dict:
        elapsed = max(0.001, time.perf_counter() - self._t_start)
        return {
            'port': self._config.port, 'baudrate': self._config.baudrate,
            'n_ch': self._n_ch_detected,
            'pkt_ok': self._pkt_ok,
            'pkt_err_cobs': self._pkt_err_cobs, 'pkt_err_crc': self._pkt_err_crc,
            'pkt_lost': self._pkt_lost,
            'pkt_rate': int(self._pkt_ok / elapsed),
            'rate_est': int(self._rate_est), 'elapsed': elapsed,
        }


# ---------------------------------------------------------------------------
# Диалог настройки
# ---------------------------------------------------------------------------

class ComCobsDialog(ComAsciiDialog):

    def __init__(self, config: ComCobsConfig | None = None, parent=None):
        self._cobs_config = config or ComCobsConfig()
        super().__init__(self._cobs_config.to_ascii_config(), parent=parent)
        self.setWindowTitle('Настройка COM-COBS источника')
        self._add_cobs_widgets()

    # ------------------------------------------------------------------
    # Построение UI — добавляем протокольные настройки + диаграмму
    # ------------------------------------------------------------------

    def _add_cobs_widgets(self):
        # --- 1. Найти GroupBox «Параметры порта» и добавить в его форму ---
        for child in self.children():
            if isinstance(child, QGroupBox) and 'орт' in (child.title() or ''):
                form = child.layout()
                if not isinstance(form, QFormLayout):
                    continue
                self._inject_proto_rows(form)
                break

        # --- 2. Вставить GroupBox «Структура пакета» между портом и превью ---
        grp_diag = QGroupBox('Структура пакета')
        grp_diag.setStyleSheet(
            'QGroupBox { font-weight: bold; color: #555; }'
            'QGroupBox::title { subcontrol-origin: margin; left: 8px; }'
        )
        vl = QVBoxLayout(grp_diag)
        vl.setContentsMargins(6, 14, 6, 4)

        self._pkt_diagram = PacketDiagram()
        vl.addWidget(self._pkt_diagram)

        root = self.layout()
        root.insertWidget(1, grp_diag)   # 0=порт, 1=диаграмма, 2=превью, 3=кнопки

        # --- 3. Первичная отрисовка диаграммы ---
        self._update_diagram()

    def _inject_proto_rows(self, form: QFormLayout):
        """Добавляет строки COUNT / тип данных / CRC в форму параметров порта."""

        # ── COUNT ──────────────────────────────────────────────────────
        self._chk_count = QCheckBox('Байт COUNT (uint8, детектор потерь)')
        self._chk_count.setChecked(self._cobs_config.has_count)
        self._chk_count.setToolTip(
            'Первый байт пакета — wrapping-счётчик 0…255.\n'
            'Позволяет FlowerGraph определять потерянные пакеты.\n'
            'Оба конца (MCU и PC) должны совпадать!'
        )
        self._chk_count.toggled.connect(self._update_diagram)
        form.addRow('', self._chk_count)

        # ── Тип данных ─────────────────────────────────────────────────
        fmt_row = QHBoxLayout()
        self._cb_format = QComboBox()
        self._cb_format.setFixedWidth(220)
        for k in DATA_FORMATS:
            self._cb_format.addItem(_FMT_LABELS[k], k)
        cur_idx = DATA_FORMATS.index(self._cobs_config.data_format) \
            if self._cobs_config.data_format in DATA_FORMATS else 0
        self._cb_format.setCurrentIndex(cur_idx)
        self._cb_format.setToolTip(
            'int16/uint16 — 2 байта/канал (классический формат)\n'
            'int32/uint32 — 4 байта/канал (расширенный диапазон)\n'
            'float32      — 4 байта/канал (физические величины, IEEE 754)'
        )
        self._cb_format.currentIndexChanged.connect(self._update_diagram)
        fmt_row.addWidget(self._cb_format)
        fmt_row.addStretch()
        form.addRow('Тип данных:', fmt_row)

        # ── CRC ────────────────────────────────────────────────────────
        self._chk_crc = QCheckBox('CRC-16/CCITT (poly=0x1021, init=0xFFFF)')
        self._chk_crc.setChecked(self._cobs_config.use_crc)
        self._chk_crc.setToolTip(
            'CRC-16/CCITT считается по [COUNT?][данные].\n'
            'При отключении — 2 байта CRC в пакете отсутствуют.\n'
            'Оба конца (MCU и PC) должны совпадать!'
        )
        self._chk_crc.toggled.connect(self._update_diagram)
        form.addRow('', self._chk_crc)

        # Каналы тоже влияют на диаграмму
        self._sb_ch.valueChanged.connect(self._update_diagram)

    # ------------------------------------------------------------------
    # Обновление диаграммы
    # ------------------------------------------------------------------

    def _update_diagram(self):
        if not hasattr(self, '_pkt_diagram'):
            return
        n_ch  = self._sb_ch.value() or 4
        batch = getattr(self, '_sb_batch', None)
        self._pkt_diagram.set_params(
            has_count   = self._chk_count.isChecked() if hasattr(self, '_chk_count') else True,
            use_crc     = self._chk_crc.isChecked()   if hasattr(self, '_chk_crc')   else True,
            data_format = self._cb_format.currentData() if hasattr(self, '_cb_format') else 'int16',
            n_ch        = n_ch,
            batch       = batch.value() if batch else 1,
        )

    # ------------------------------------------------------------------
    # Сбор конфига
    # ------------------------------------------------------------------

    def get_cobs_config(self) -> ComCobsConfig:
        base = self.get_config()
        return ComCobsConfig(
            port        = base.port,
            baudrate    = base.baudrate,
            n_channels  = base.n_channels,
            has_count   = self._chk_count.isChecked() if hasattr(self, '_chk_count') else True,
            use_crc     = self._chk_crc.isChecked()   if hasattr(self, '_chk_crc')   else True,
            data_format = self._cb_format.currentData() if hasattr(self, '_cb_format') else 'int16',
        )

    # ------------------------------------------------------------------
    # Предпросмотр
    # ------------------------------------------------------------------

    def _flush_preview(self):
        while True:
            delim = self._preview_buf.find(0x00)
            if delim < 0:
                if len(self._preview_buf) > 512:
                    self._preview_buf.clear()
                break
            cobs_data = bytes(self._preview_buf[:delim])
            del self._preview_buf[:delim + 1]
            if cobs_data:
                self._show_cobs_packet(cobs_data)

    def _show_cobs_packet(self, cobs_data: bytes):
        hex_str = ' '.join(f'{b:02X}' for b in cobs_data)

        has_count = self._chk_count.isChecked() if hasattr(self, '_chk_count') else True
        use_crc   = self._chk_crc.isChecked()   if hasattr(self, '_chk_crc')   else True
        data_fmt  = self._cb_format.currentData() if hasattr(self, '_cb_format') else 'int16'
        bps_val   = _bps(data_fmt)
        sfmt      = _STRUCT_FMT[data_fmt]
        fsht      = _FMT_SHORT[data_fmt]

        raw = _cobs_decode(cobs_data)
        if raw is None:
            self._prev_log(f'RAW: {hex_str}  →  [COBS ошибка структуры]', '#f66')
            return

        rlen = len(raw)
        hdr  = 1 if has_count else 0
        ftr  = 2 if use_crc   else 0
        payload_len = rlen - hdr - ftr

        if payload_len < bps_val or payload_len % bps_val != 0:
            raw_hex = ' '.join(f'{b:02X}' for b in raw)
            self._prev_log(
                f'RAW: {hex_str}\nDEC: {raw_hex}  →  [длина {rlen}б не соответствует '
                f'формату {fsht}, hdr={hdr}, ftr={ftr}]', '#ce9178')
            return

        n_ch = payload_len // bps_val

        if use_crc:
            crc_rx  = struct.unpack_from('<H', raw, hdr + payload_len)[0]
            crc_ok  = _crc16(raw[:hdr + payload_len]) == crc_rx
            crc_str = 'CRC=OK' if crc_ok else f'CRC=ERR(rx={crc_rx:04X})'
            color   = '#9cdcfe' if crc_ok else '#f66'
        else:
            crc_ok, crc_str, color = True, 'no CRC', '#9cdcfe'

        cnt_str = f'CNT={raw[0]:3d}  ' if has_count else ''
        vals    = [struct.unpack_from(sfmt, raw, hdr + i * bps_val)[0] for i in range(n_ch)]

        if data_fmt == 'float32':
            vals_str = '  '.join(f'CH{i+1}={v:10.4f}' for i, v in enumerate(vals))
        else:
            vals_str = '  '.join(f'CH{i+1}={v:8d}' for i, v in enumerate(vals))

        raw_hex = ' '.join(f'{b:02X}' for b in raw)
        self._prev_log(
            f'RAW [{len(cobs_data):2d}б]: {hex_str}\n'
            f'DEC [{rlen:2d}б]: {raw_hex}\n'
            f'     {cnt_str}{vals_str}  [{fsht}]  {crc_str}',
            color,
        )
