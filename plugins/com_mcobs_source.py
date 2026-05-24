"""
COM-mCOBS источник данных — COBS с батчингом нескольких выборок в одном пакете.

Формат пакета (uart_pgc.c, PG_PROTO=1, N_COBS_BATCH > 1):

  Сырые данные до COBS-кодирования:
    [COUNT:1][Выб0_V0_L:1][Выб0_V0_H:1]...[ВыбK_VN_L:1][ВыбK_VN_H:1][CRC_L:1][CRC_H:1]
    Итого: 3 + N_BATCH × 2 × N_Of_VARS байт

  После COBS-кодирования + финальный 0x00 (делимитер):
    Максимум: RAW + 2 байта

  N_BATCH = число выборок в одном пакете (конфигурируется дефайном в прошивке)

Пропускная способность vs COM COBS (N=4, 460800 бод):
  N_BATCH=1  → ~2 862 выб/с  (= обычный COBS)
  N_BATCH=2  → ~4 389 выб/с
  N_BATCH=4  → ~4 983 выб/с  ★ рекомендуется
  N_BATCH=8  → ~5 343 выб/с

CRC-16/CCITT: poly=0x1021, init=0xFFFF
COUNT: 8-битный счётчик пакетов, детектор потерь
"""

import queue
import struct
import threading
import time
from dataclasses import dataclass, field

import numpy as np
import serial
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QFormLayout, QSpinBox, QHBoxLayout, QLabel, QCheckBox
)

from plugins.base_source import BaseSource
from plugins.com_cobs_source import (
    ComCobsConfig, ComCobsDialog, _crc16, _cobs_decode
)
from plugins.com_ascii_source import ComAsciiConfig


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

@dataclass
class ComMCobsConfig:
    port:           str             = 'COM4'
    baudrate:       int             = 460800
    n_channels:     int             = 0          # 0 = авто
    batch_size:     int             = 4          # N_COBS_BATCH в прошивке
    use_crc:        bool            = True       # COBS_USE_CRC в прошивке
    channel_names:  list[str] | None = field(default=None)

    def to_cobs_config(self) -> ComCobsConfig:
        return ComCobsConfig(
            port=self.port, baudrate=self.baudrate,
            n_channels=self.n_channels, channel_names=self.channel_names,
        )

    def to_ascii_config(self) -> ComAsciiConfig:
        return ComAsciiConfig(
            port=self.port, baudrate=self.baudrate,
            n_channels=self.n_channels, channel_names=self.channel_names,
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
        self._queue:  queue.Queue              = queue.Queue()
        self._port:   serial.Serial | None     = None
        self._thread: threading.Thread | None  = None

        self._pkt_ok        = 0
        self._pkt_err_cobs  = 0
        self._pkt_err_crc   = 0
        self._pkt_lost      = 0
        self._t_start       = 0.0
        self._sample_count  = 0

        self._n_ch_detected   = 0
        self._batch_detected  = 0    # авто-определение из первого пакета
        self._last_count      = -1

        self._rate_est  = 0.0
        self._t_base    = 0.0

        self._reconnect_delay = 2.0
        self._reconnect_count = 0

        self._drain_timer = QTimer()
        self._drain_timer.timeout.connect(self._drain_queue)

    # ------------------------------------------------------------------
    # BaseSource interface
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

        self._running         = True
        self._t_start         = 0.0   # будет установлен по первому пакету
        self._sample_count    = 0
        self._pkt_ok          = 0
        self._pkt_err_cobs    = 0
        self._pkt_err_crc     = 0
        self._pkt_lost        = 0
        self._n_ch_detected   = self._config.n_channels
        self._batch_detected  = 0
        self._last_count      = -1
        self._rate_est        = self._rate_from_baud()
        self._t_base          = 0.0

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
    # Оценка частоты
    # ------------------------------------------------------------------

    def _rate_from_baud(self) -> float:
        n_ch  = self._n_ch_detected or self._config.n_channels or 4
        batch = self._batch_detected or self._config.batch_size
        overhead = 2 if self._config.use_crc else 1   # CRC16 + COUNT
        raw_len = overhead + 1 + batch * 2 * n_ch     # +1 = COUNT
        enc_len = raw_len + 2
        pkt_rate = self._config.baudrate / (enc_len * 10.0)
        return pkt_rate * batch   # выборок в секунду

    def _calibrate_rate(self):
        elapsed = time.perf_counter() - self._t_start
        if elapsed < 1.0 or self._sample_count < 500:
            return
        measured  = self._sample_count / elapsed
        new_rate  = self._rate_est + 0.15 * (measured - self._rate_est)
        self._t_base  += self._sample_count * (1.0/self._rate_est - 1.0/new_rate)
        self._rate_est = new_rate

    # ------------------------------------------------------------------
    # Фоновый поток чтения
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
                    f'Попытка реконнекта #{self._reconnect_count} '
                    f'({self._config.port})…'
                )
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
    # Разбор пакета
    # ------------------------------------------------------------------

    def _parse_packet(self, cobs_data: bytes):
        raw = _cobs_decode(cobs_data)
        if raw is None:
            self._pkt_err_cobs += 1
            return

        rlen = len(raw)
        # Минимум: COUNT(1) + 1 выборка × 1 канал (2) + CRC(2) = 5 байт
        min_len = 3 if self._config.use_crc else 1
        if rlen < min_len + 2:
            self._pkt_err_cobs += 1
            return

        # Определить payload длину
        payload_len = rlen - 1 - (2 if self._config.use_crc else 0)

        # payload_len = batch × n_ch × 2  → должен делиться без остатка
        if payload_len < 2 or payload_len % 2 != 0:
            self._pkt_err_cobs += 1
            return

        # Авто-определение n_ch и batch из первого пакета
        if self._n_ch_detected == 0:
            # Определяем из batch_size (из конфига) и payload
            n_ch = self._config.n_channels or 4
            if payload_len % (n_ch * 2) == 0:
                batch = payload_len // (n_ch * 2)
                self._n_ch_detected  = n_ch
                self._batch_detected = batch
                self._rate_est = self._rate_from_baud()
            else:
                self._pkt_err_cobs += 1
                return

        n_ch  = self._n_ch_detected
        batch = self._batch_detected or self._config.batch_size

        if payload_len != batch * n_ch * 2:
            self._pkt_err_cobs += 1
            return

        # CRC проверка
        if self._config.use_crc:
            crc_rx   = struct.unpack_from('<H', raw, 1 + payload_len)[0]
            crc_calc = _crc16(raw[:1 + payload_len])
            if crc_rx != crc_calc:
                self._pkt_err_crc += 1
                return

        # Детектирование потерь по COUNT
        count = raw[0]
        if self._last_count >= 0:
            expected = (self._last_count + 1) & 0xFF
            if count != expected:
                lost = (count - expected) & 0xFF
                self._pkt_lost += lost
                self._emit_error(
                    f'mCOBS: потеряно {lost} пакетов '
                    f'(ожидался {expected}, получен {count})'
                )
        self._last_count = count

        # Извлечь все выборки из батча
        values_batch = np.empty((batch, n_ch), dtype=np.float32)
        for k in range(batch):
            for i in range(n_ch):
                offset = 1 + (k * n_ch + i) * 2
                v = struct.unpack_from('<h', raw, offset)[0]
                values_batch[k, i] = float(v)

        # Первый пакет — фиксируем реальное время начала данных
        if self._sample_count == 0:
            self._t_start = time.perf_counter()

        # Rate-locked timestamps для всех выборок батча
        t0 = self._t_base + self._sample_count / self._rate_est
        dt = 1.0 / self._rate_est
        times = np.array([t0 + k * dt for k in range(batch)], dtype=np.float64)
        self._sample_count += batch

        if self._sample_count % self._RECAL_AT < batch:
            self._calibrate_rate()

        self._queue.put((times, values_batch))
        self._pkt_ok += 1

    # ------------------------------------------------------------------
    # Дренаж очереди
    # ------------------------------------------------------------------

    def _drain_queue(self):
        while True:
            try:
                times, values = self._queue.get_nowait()
                self._emit(times, values)
            except queue.Empty:
                break

    # ------------------------------------------------------------------
    # Статистика
    # ------------------------------------------------------------------

    @property
    def stats(self) -> dict:
        elapsed = max(0.001, time.perf_counter() - self._t_start)
        return {
            'port':          self._config.port,
            'baudrate':      self._config.baudrate,
            'n_ch':          self._n_ch_detected,
            'batch':         self._batch_detected,
            'pkt_ok':        self._pkt_ok,
            'pkt_err_cobs':  self._pkt_err_cobs,
            'pkt_err_crc':   self._pkt_err_crc,
            'pkt_lost':      self._pkt_lost,
            'sample_rate':   int(self._rate_est),
            'elapsed':       elapsed,
        }


# ---------------------------------------------------------------------------
# Диалог настройки
# ---------------------------------------------------------------------------

class ComMCobsDialog(ComCobsDialog):
    def __init__(self, config: ComMCobsConfig | None = None, parent=None):
        self._mcobs_config = config or ComMCobsConfig()
        super().__init__(self._mcobs_config.to_cobs_config(), parent=parent)
        self.setWindowTitle('Настройка COM-mCOBS источника')
        self._add_mcobs_widgets()

    def _add_mcobs_widgets(self):
        # Найти groupbox параметров порта и добавить в него поля mCOBS
        from PySide6.QtWidgets import QGroupBox
        for child in self.children():
            if isinstance(child, QGroupBox) and 'орт' in (child.title() or ''):
                form = child.layout()
                if form is None:
                    continue

                # Выборок в пакете
                batch_h = QHBoxLayout()
                self._sb_batch = QSpinBox()
                self._sb_batch.setRange(1, 64)
                self._sb_batch.setValue(self._mcobs_config.batch_size)
                self._sb_batch.setFixedWidth(70)
                self._sb_batch.setToolTip(
                    'N_COBS_BATCH в прошивке.\n'
                    '1 = обычный COBS, 4 = рекомендуется, 8 = максимум'
                )
                batch_h.addWidget(self._sb_batch)
                batch_h.addWidget(QLabel('(N_COBS_BATCH в прошивке)'))
                batch_h.addStretch()
                form.addRow('Выборок в пакете:', batch_h)

                # CRC
                self._chk_crc = QCheckBox('CRC-16/CCITT в пакете (COBS_USE_CRC=1)')
                self._chk_crc.setChecked(self._mcobs_config.use_crc)
                self._chk_crc.setToolTip(
                    'Совпадает с COBS_USE_CRC в uart_pgc.h.\n'
                    'Отключение ускоряет передачу на ~5%.'
                )
                form.addRow('', self._chk_crc)
                break

    def get_mcobs_config(self) -> ComMCobsConfig:
        base = self.get_config()
        batch = getattr(self, '_sb_batch', None)
        crc   = getattr(self, '_chk_crc', None)
        return ComMCobsConfig(
            port          = base.port,
            baudrate      = base.baudrate,
            n_channels    = base.n_channels,
            batch_size    = batch.value() if batch else self._mcobs_config.batch_size,
            use_crc       = crc.isChecked() if crc else self._mcobs_config.use_crc,
            channel_names = self._mcobs_config.channel_names,
        )

    # Переопределяем предпросмотр — показываем mCOBS пакеты
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
        batch_size = getattr(self, '_sb_batch', None)
        batch = batch_size.value() if batch_size else 4
        use_crc = getattr(self, '_chk_crc', None)
        crc_on = use_crc.isChecked() if use_crc else True

        payload_len = rlen - 1 - (2 if crc_on else 0)
        if payload_len < 2 or payload_len % 2 != 0:
            self._prev_log(f'RAW: {hex_str}  →  [неверная длина {rlen}]', '#ce9178')
            return

        n_ch = payload_len // (batch * 2) if payload_len % (batch * 2) == 0 else 0
        if n_ch == 0:
            self._prev_log(f'RAW [{len(cobs_data)}б]: {hex_str}  →  [не совпадает с batch={batch}]', '#ce9178')
            return

        if crc_on:
            crc_rx   = struct.unpack_from('<H', raw, 1 + payload_len)[0]
            crc_ok   = _crc16(raw[:1 + payload_len]) == crc_rx
        else:
            crc_ok   = True
            crc_rx   = 0

        count = raw[0]
        lines = [f'RAW [{len(cobs_data)}б]: {hex_str}']
        lines.append(f'     CNT={count:3d}  batch={batch}  N={n_ch}  {"CRC=OK" if crc_ok else f"CRC=ERR(rx={crc_rx:04X})"}')

        for k in range(batch):
            vals = []
            for i in range(n_ch):
                v = struct.unpack_from('<h', raw, 1 + (k*n_ch + i)*2)[0]
                vals.append(f'CH{i+1}={v:7d}')
            lines.append(f'     выб.{k}: {" ".join(vals)}')

        import struct as _struct
        color = '#9cdcfe' if crc_ok else '#f66'
        self._prev_log('\n'.join(lines), color)

import struct  # нужен в _show_mcobs_packet
