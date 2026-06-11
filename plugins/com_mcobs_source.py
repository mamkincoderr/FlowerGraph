"""
COM-mCOBS источник данных — COBS с батчингом нескольких выборок в одном пакете.

Структура пакета (конфигурируется в диалоге):

  [COUNT:1]?  [Выб0_CH0…Выб0_CHN]  …  [ВыбK_CH0…ВыбK_CHN]  [CRC_L CRC_H]?
   has_count   ←── BATCH × N_CH × bps байт ──────────────────►  use_crc

  bps = 2 (int16/uint16) | 4 (int32/uint32/float32)

Пропускная способность (N=4, 460800 бод, int16, COUNT+CRC):
  BATCH=1  → ~2 862 выб/с   BATCH=4  → ~4 983 выб/с ★
  BATCH=2  → ~4 389 выб/с   BATCH=8  → ~5 343 выб/с
"""

import math
import queue
import struct
import threading
import time
from dataclasses import dataclass, field

import numpy as np
import serial
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFormLayout, QSpinBox, QHBoxLayout, QLabel

from plugins.base_source import BaseSource
from plugins.com_cobs_source import (
    ComCobsConfig, ComCobsDialog, _crc16, _cobs_decode,
    DATA_FORMATS, _FMT_LABELS, _FMT_SHORT, _STRUCT_FMT, _bps,
)
from plugins.com_ascii_source import ComAsciiConfig


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

@dataclass
class ComMCobsConfig:
    port:          str             = 'COM4'
    baudrate:      int             = 460800
    n_channels:    int             = 0           # 0 = авто
    batch_size:    int             = 4           # N_COBS_BATCH в прошивке
    has_count:     bool            = True        # байт COUNT присутствует
    use_crc:       bool            = True        # CRC-16 в конце пакета
    data_format:   str             = 'int16'     # тип данных каналов
    channel_names: list[str] | None = field(default=None)

    def to_cobs_config(self) -> ComCobsConfig:
        return ComCobsConfig(
            port=self.port, baudrate=self.baudrate,
            n_channels=self.n_channels,
            has_count=self.has_count, use_crc=self.use_crc,
            data_format=self.data_format,
            channel_names=self.channel_names,
        )

    def to_ascii_config(self) -> ComAsciiConfig:
        return ComAsciiConfig(
            port=self.port, baudrate=self.baudrate,
            n_channels=self.n_channels, channel_names=self.channel_names,
        )

    def to_dict(self) -> dict:
        return {
            'port': self.port, 'baudrate': self.baudrate,
            'n_channels': self.n_channels, 'batch_size': self.batch_size,
            'has_count': self.has_count, 'use_crc': self.use_crc,
            'data_format': self.data_format,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'ComMCobsConfig':
        return cls(
            port=d.get('port', 'COM4'),
            baudrate=d.get('baudrate', 460800),
            n_channels=d.get('n_channels', 0),
            batch_size=d.get('batch_size', 4),
            has_count=d.get('has_count', True),
            use_crc=d.get('use_crc', True),
            data_format=d.get('data_format', 'int16'),
        )


# ---------------------------------------------------------------------------
# Источник
# ---------------------------------------------------------------------------

class ComMCobsSource(BaseSource):
    _DRAIN_MS = 15
    _RECAL_AT = 500

    def __init__(self, config: ComMCobsConfig | None = None):
        super().__init__()
        self._config = config or ComMCobsConfig()
        self._queue:  queue.Queue             = queue.Queue()
        self._port:   serial.Serial | None    = None
        self._thread: threading.Thread | None = None

        self._pkt_ok       = 0
        self._pkt_err_cobs = 0
        self._pkt_err_crc  = 0
        self._pkt_lost     = 0
        self._t_start      = 0.0
        self._sample_count = 0

        self._n_ch_detected  = 0
        self._batch_detected = 0
        self._last_count     = -1

        self._rate_est = 0.0
        self._t_base   = 0.0

        self._reconnect_delay = 2.0
        self._reconnect_count = 0

        self._drain_timer = QTimer()
        self._drain_timer.timeout.connect(self._drain_queue)

    # ------------------------------------------------------------------

    def get_name(self) -> str:
        b = self._batch_detected or self._config.batch_size
        return f'COM-mCOBS ({self._config.port} {self._config.baudrate} ×{b})'

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

        self._running        = True
        self._t_start        = 0.0
        self._sample_count   = 0
        self._pkt_ok         = 0
        self._pkt_err_cobs   = 0
        self._pkt_err_crc    = 0
        self._pkt_lost       = 0
        self._n_ch_detected  = self._config.n_channels
        self._batch_detected = 0
        self._last_count     = -1
        self._rate_est       = self._rate_from_baud()
        self._t_base         = 0.0

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
        n_ch  = self._n_ch_detected or self._config.n_channels or 4
        batch = self._batch_detected or self._config.batch_size
        hdr   = 1 if self._config.has_count else 0
        ftr   = 2 if self._config.use_crc   else 0
        bps_v = _bps(self._config.data_format)
        raw_len  = hdr + ftr + batch * n_ch * bps_v
        enc_len  = raw_len + 2
        pkt_rate = self._config.baudrate / (enc_len * 10.0)
        return pkt_rate * batch

    def _calibrate_rate(self):
        elapsed = time.perf_counter() - self._t_start
        if elapsed < 1.0 or self._sample_count < 500:
            return
        measured  = self._sample_count / elapsed
        new_rate  = self._rate_est + 0.15 * (measured - self._rate_est)
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
                if len(buf) > 1024:
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

        rlen    = len(raw)
        bps_val = _bps(self._config.data_format)
        hdr     = 1 if self._config.has_count else 0
        ftr     = 2 if self._config.use_crc   else 0

        payload_len = rlen - hdr - ftr
        if payload_len < bps_val or payload_len % bps_val != 0:
            self._pkt_err_cobs += 1
            return

        # Авто-определение n_ch и batch из первого пакета
        if self._n_ch_detected == 0:
            n_ch = self._config.n_channels or 4
            if payload_len % (n_ch * bps_val) == 0:
                batch = payload_len // (n_ch * bps_val)
                self._n_ch_detected  = n_ch
                self._batch_detected = batch
                self._rate_est = self._rate_from_baud()
            else:
                self._pkt_err_cobs += 1
                return

        n_ch  = self._n_ch_detected
        batch = self._batch_detected or self._config.batch_size

        if payload_len != batch * n_ch * bps_val:
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
                    self._emit_error(
                        f'mCOBS: потеряно {lost} пакетов '
                        f'(ожидался {expected}, получен {count})')
            self._last_count = count

        # Данные
        sfmt    = _STRUCT_FMT[self._config.data_format]
        is_f32  = self._config.data_format == 'float32'
        values_batch = np.empty((batch, n_ch), dtype=np.float32)
        for k in range(batch):
            for i in range(n_ch):
                offset = hdr + (k * n_ch + i) * bps_val
                v = struct.unpack_from(sfmt, raw, offset)[0]
                if is_f32 and not math.isfinite(v):
                    v = 0.0
                values_batch[k, i] = float(v)

        if self._sample_count == 0:
            self._t_start = time.perf_counter()

        t0    = self._t_base + self._sample_count / self._rate_est
        dt    = 1.0 / self._rate_est
        times = np.array([t0 + k * dt for k in range(batch)], dtype=np.float64)
        self._sample_count += batch

        if self._sample_count % self._RECAL_AT < batch:
            self._calibrate_rate()

        self._queue.put((times, values_batch))
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
            'n_ch': self._n_ch_detected, 'batch': self._batch_detected,
            'pkt_ok': self._pkt_ok,
            'pkt_err_cobs': self._pkt_err_cobs, 'pkt_err_crc': self._pkt_err_crc,
            'pkt_lost': self._pkt_lost,
            'sample_rate': int(self._rate_est), 'elapsed': elapsed,
        }


# ---------------------------------------------------------------------------
# Диалог настройки
# ---------------------------------------------------------------------------

class ComMCobsDialog(ComCobsDialog):

    def __init__(self, config: ComMCobsConfig | None = None, parent=None):
        self._mcobs_config = config or ComMCobsConfig()
        # Передаём COBS-конфиг с правильными значениями has_count/use_crc/data_format
        super().__init__(self._mcobs_config.to_cobs_config(), parent=parent)
        self.setWindowTitle('Настройка COM-mCOBS источника')
        self._add_mcobs_widgets()

    def _add_mcobs_widgets(self):
        from PySide6.QtWidgets import QGroupBox
        for child in self.children():
            if isinstance(child, QGroupBox) and 'орт' in (child.title() or ''):
                form = child.layout()
                if not isinstance(form, QFormLayout):
                    continue

                # Вставляем «Выборок в пакете» ПЕРЕД строкой COUNT (3 строки с конца)
                batch_row = QHBoxLayout()
                self._sb_batch = QSpinBox()
                self._sb_batch.setRange(1, 64)
                self._sb_batch.setValue(self._mcobs_config.batch_size)
                self._sb_batch.setFixedWidth(70)
                self._sb_batch.setToolTip(
                    'N_COBS_BATCH в прошивке.\n'
                    '1 = обычный COBS, 4 = рекомендуется, 8 = максимум'
                )
                self._sb_batch.valueChanged.connect(self._update_diagram)
                batch_row.addWidget(self._sb_batch)
                batch_row.addWidget(QLabel('(N_COBS_BATCH в прошивке)'))
                batch_row.addStretch()

                # COUNT, data_format, CRC — 3 последних строки → вставляем перед ними
                form.insertRow(form.rowCount() - 3, 'Выборок в пакете:', batch_row)
                break

    def get_mcobs_config(self) -> ComMCobsConfig:
        cobs  = self.get_cobs_config()   # читает has_count, use_crc, data_format
        batch = getattr(self, '_sb_batch', None)
        return ComMCobsConfig(
            port        = cobs.port,
            baudrate    = cobs.baudrate,
            n_channels  = cobs.n_channels,
            batch_size  = batch.value() if batch else self._mcobs_config.batch_size,
            has_count   = cobs.has_count,
            use_crc     = cobs.use_crc,
            data_format = cobs.data_format,
        )

    # ------------------------------------------------------------------
    # Предпросмотр mCOBS
    # ------------------------------------------------------------------

    def _flush_preview(self):
        while True:
            delim = self._preview_buf.find(0x00)
            if delim < 0:
                if len(self._preview_buf) > 1024:
                    self._preview_buf.clear()
                break
            cobs_data = bytes(self._preview_buf[:delim])
            del self._preview_buf[:delim + 1]
            if cobs_data:
                self._show_mcobs_packet(cobs_data)

    def _show_mcobs_packet(self, cobs_data: bytes):
        hex_str = ' '.join(f'{b:02X}' for b in cobs_data)
        raw = _cobs_decode(cobs_data)
        if raw is None:
            self._prev_log(f'RAW: {hex_str}  →  [COBS ошибка]', '#f66')
            return

        rlen = len(raw)
        batch_sb  = getattr(self, '_sb_batch', None)
        batch     = batch_sb.value() if batch_sb else 4
        has_count = self._chk_count.isChecked() if hasattr(self, '_chk_count') else True
        use_crc   = self._chk_crc.isChecked()   if hasattr(self, '_chk_crc')   else True
        data_fmt  = self._cb_format.currentData() if hasattr(self, '_cb_format') else 'int16'
        bps_val   = _bps(data_fmt)
        sfmt      = _STRUCT_FMT[data_fmt]
        fsht      = _FMT_SHORT[data_fmt]

        hdr = 1 if has_count else 0
        ftr = 2 if use_crc   else 0
        payload_len = rlen - hdr - ftr

        if payload_len < bps_val or payload_len % bps_val != 0:
            self._prev_log(
                f'RAW [{len(cobs_data)}б]: {hex_str}  →  [длина {rlen}б не соответствует]',
                '#ce9178')
            return

        n_ch = payload_len // (batch * bps_val) if payload_len % (batch * bps_val) == 0 else 0
        if n_ch == 0:
            self._prev_log(
                f'RAW [{len(cobs_data)}б]: {hex_str}  →  [не совпадает batch={batch} fmt={fsht}]',
                '#ce9178')
            return

        if use_crc:
            crc_rx  = struct.unpack_from('<H', raw, hdr + payload_len)[0]
            crc_ok  = _crc16(raw[:hdr + payload_len]) == crc_rx
            crc_str = 'CRC=OK' if crc_ok else f'CRC=ERR(rx={crc_rx:04X})'
        else:
            crc_ok, crc_str = True, 'no CRC'

        cnt_str = f'CNT={raw[0]:3d}  ' if has_count else ''
        lines   = [f'RAW [{len(cobs_data)}б]: {hex_str}']
        lines.append(f'     {cnt_str}batch={batch}  N={n_ch}  {fsht}  {crc_str}')

        for k in range(batch):
            vals = []
            for i in range(n_ch):
                v = struct.unpack_from(sfmt, raw, hdr + (k * n_ch + i) * bps_val)[0]
                if data_fmt == 'float32':
                    vals.append(f'CH{i+1}={v:10.4f}')
                else:
                    vals.append(f'CH{i+1}={v:8d}')
            lines.append(f'     выб.{k}: {" ".join(vals)}')

        color = '#9cdcfe' if crc_ok else '#f66'
        self._prev_log('\n'.join(lines), color)
