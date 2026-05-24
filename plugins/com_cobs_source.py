"""
COM-COBS источник данных — бинарный протокол с COBS-кодированием.

Формат пакета (uart_pgc.c, PG_PROTO=1):

  Сырые данные до COBS-кодирования:
    [COUNT:1][V0_L:1][V0_H:1]...[VN_L:1][VN_H:1][CRC_L:1][CRC_H:1]
    Итого: 3 + 2*N байт

  После COBS-кодирования + финальный 0x00 (делимитер пакета):
    Максимум: (3 + 2*N + 2) байт

  CRC-16/CCITT: poly=0x1021, init=0xFFFF, по байтам COUNT + данные
  COUNT: 8-битный счётчик пакетов, wraparound 0xFF→0x00

Временны́е метки: rate-locked (как в COM ASCII), без зависимости от
джиттера ОС. Калибровка каждые 500 пакетов.
"""

import queue
import struct
import threading
import time
from dataclasses import dataclass, field

import numpy as np
import serial
from PySide6.QtCore import QTimer

from plugins.base_source import BaseSource
from ui.com_ascii_dialog import ComAsciiDialog
from plugins.com_ascii_source import ComAsciiConfig


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
    """
    COBS-декодирование блока данных БЕЗ финального 0x00.
    Возвращает None при ошибке структуры (0x00 внутри, выход за границу).
    """
    result = bytearray()
    idx    = 0
    n      = len(data)
    while idx < n:
        code = data[idx]
        if code == 0:
            return None          # 0x00 внутри COBS-данных = ошибка
        idx += 1
        end  = idx + code - 1
        if end > n:
            return None          # выход за пределы буфера
        result.extend(data[idx:end])
        idx = end
        if code != 0xFF and idx < n:
            result.append(0x00)  # подразумеваемый ноль между сегментами
    return bytes(result)


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

@dataclass
class ComCobsConfig:
    port:           str             = 'COM4'
    baudrate:       int             = 460800
    n_channels:     int             = 0          # 0 = авто
    channel_names:  list[str] | None = field(default=None)

    def to_ascii_config(self) -> ComAsciiConfig:
        return ComAsciiConfig(
            port=self.port, baudrate=self.baudrate,
            n_channels=self.n_channels, channel_names=self.channel_names,
        )

    @classmethod
    def from_ascii_config(cls, cfg: ComAsciiConfig) -> 'ComCobsConfig':
        return cls(
            port=cfg.port, baudrate=cfg.baudrate,
            n_channels=cfg.n_channels, channel_names=cfg.channel_names,
        )


# ---------------------------------------------------------------------------
# Источник
# ---------------------------------------------------------------------------

class ComCobsSource(BaseSource):
    _DRAIN_MS  = 15
    _RECAL_AT  = 500     # пакетов между калибровками частоты

    def __init__(self, config: ComCobsConfig | None = None):
        super().__init__()
        self._config = config or ComCobsConfig()
        self._queue: queue.Queue = queue.Queue()
        self._port:   serial.Serial | None  = None
        self._thread: threading.Thread | None = None

        # Статистика
        self._pkt_ok        = 0
        self._pkt_err_cobs  = 0
        self._pkt_err_crc   = 0
        self._pkt_lost      = 0
        self._t_start       = 0.0
        self._sample_count  = 0

        # Авто-определение числа каналов
        self._n_ch_detected = 0

        # Детектор потерь пакетов
        self._last_count    = -1

        # Rate-locked timestamps
        self._rate_est  = 0.0
        self._t_base    = 0.0

        # Авто-реконнект
        self._reconnect_delay = 2.0
        self._reconnect_count = 0

        self._drain_timer = QTimer()
        self._drain_timer.timeout.connect(self._drain_queue)

    # ------------------------------------------------------------------
    # BaseSource interface
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
    # Оценка частоты
    # ------------------------------------------------------------------

    def _rate_from_baud(self) -> float:
        n_ch    = self._n_ch_detected or self._config.n_channels or 4
        raw_len = 3 + 2 * n_ch          # сырой пакет
        enc_len = raw_len + 2           # COBS overhead + 0x00 delimiter
        return self._config.baudrate / (enc_len * 10.0)

    def _calibrate_rate(self):
        elapsed = time.perf_counter() - self._t_start
        if elapsed < 1.0 or self._sample_count < 500:
            return
        measured = self._sample_count / elapsed
        new_rate = self._rate_est + 0.15 * (measured - self._rate_est)
        # Корректируем t_base для непрерывности временны́х меток
        self._t_base  += self._sample_count * (1.0 / self._rate_est
                                                - 1.0 / new_rate)
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
        """Извлечь полные COBS-пакеты по делимитеру 0x00."""
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
    # Разбор одного пакета
    # ------------------------------------------------------------------

    def _parse_packet(self, cobs_data: bytes):
        # 1. COBS-декодирование
        raw = _cobs_decode(cobs_data)
        if raw is None:
            self._pkt_err_cobs += 1
            return

        # 2. Проверка длины: COUNT(1) + N×int16(2N) + CRC16(2) → len = 3+2N, N≥1
        rlen = len(raw)
        if rlen < 5 or (rlen - 3) % 2 != 0:
            self._pkt_err_cobs += 1
            return

        n_ch = (rlen - 3) // 2

        # 3. Авто-определение числа каналов по первому пакету
        if self._n_ch_detected == 0:
            self._n_ch_detected = n_ch
            self._rate_est = self._rate_from_baud()

        if n_ch != self._n_ch_detected:
            self._pkt_err_cobs += 1
            return

        # 4. Проверка CRC-16/CCITT
        crc_rx   = struct.unpack_from('<H', raw, 1 + n_ch * 2)[0]
        crc_calc = _crc16(raw[:1 + n_ch * 2])
        if crc_rx != crc_calc:
            self._pkt_err_crc += 1
            return

        # 5. Детектирование потерь по COUNT
        count = raw[0]
        if self._last_count >= 0:
            expected = (self._last_count + 1) & 0xFF
            if count != expected:
                lost = (count - expected) & 0xFF
                self._pkt_lost += lost
                if lost:
                    self._emit_error(
                        f'COBS: потеряно {lost} пакетов '
                        f'(ожидался {expected}, получен {count})'
                    )
        self._last_count = count

        # 6. Извлечь значения int16_t (little-endian)
        values = np.empty((1, n_ch), dtype=np.float32)
        for i in range(n_ch):
            values[0, i] = float(struct.unpack_from('<h', raw, 1 + i * 2)[0])

        # 7. Rate-locked timestamp
        self._sample_count += 1
        t = self._t_base + self._sample_count / self._rate_est
        if self._sample_count % self._RECAL_AT == 0:
            self._calibrate_rate()

        self._queue.put((np.array([t], dtype=np.float64), values))
        self._pkt_ok += 1

    # ------------------------------------------------------------------
    # Дренаж очереди (главный поток)
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
            'port':           self._config.port,
            'baudrate':       self._config.baudrate,
            'n_ch':           self._n_ch_detected,
            'pkt_ok':         self._pkt_ok,
            'pkt_err_cobs':   self._pkt_err_cobs,
            'pkt_err_crc':    self._pkt_err_crc,
            'pkt_lost':       self._pkt_lost,
            'pkt_rate':       int(self._pkt_ok / elapsed),
            'rate_est':       int(self._rate_est),
            'elapsed':        elapsed,
        }


# ---------------------------------------------------------------------------
# Диалог настройки (переиспользует COM-ASCII диалог)
# ---------------------------------------------------------------------------

class ComCobsDialog(ComAsciiDialog):
    def __init__(self, config: ComCobsConfig | None = None, parent=None):
        ascii_cfg = config.to_ascii_config() if config else ComAsciiConfig()
        super().__init__(ascii_cfg, parent=parent)
        self.setWindowTitle('Настройка COM-COBS источника')

    def get_cobs_config(self) -> ComCobsConfig:
        return ComCobsConfig.from_ascii_config(self.get_config())

    # ------------------------------------------------------------------
    # Переопределение предпросмотра — COBS пакеты (делимитер 0x00)
    # ------------------------------------------------------------------

    def _flush_preview(self):
        """COBS: разбиваем по 0x00, показываем HEX + декодированные значения."""
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
        import struct
        hex_str = ' '.join(f'{b:02X}' for b in cobs_data)

        raw = _cobs_decode(cobs_data)
        if raw is None:
            self._prev_log(f'RAW: {hex_str}   →   [COBS ошибка структуры]', '#f66')
            return

        rlen = len(raw)
        if rlen < 5 or (rlen - 3) % 2 != 0:
            raw_hex = ' '.join(f'{b:02X}' for b in raw)
            self._prev_log(
                f'RAW: {hex_str}\n'
                f'DEC: {raw_hex}   →   [неверная длина {rlen}]', '#ce9178')
            return

        n_ch    = (rlen - 3) // 2
        crc_rx  = struct.unpack_from('<H', raw, 1 + n_ch * 2)[0]
        crc_ok  = _crc16(raw[:1 + n_ch * 2]) == crc_rx
        count   = raw[0]
        vals    = [struct.unpack_from('<h', raw, 1 + i * 2)[0] for i in range(n_ch)]

        raw_hex  = ' '.join(f'{b:02X}' for b in raw)
        vals_str = '  '.join(f'CH{i+1}={v:7d}' for i, v in enumerate(vals))
        crc_str  = 'CRC=OK' if crc_ok else f'CRC=ERR(rx={crc_rx:04X})'
        color    = '#9cdcfe' if crc_ok else '#f66'

        self._prev_log(
            f'RAW [{len(cobs_data):2d}б]: {hex_str}\n'
            f'DEC [{rlen:2d}б]: {raw_hex}\n'
            f'     CNT={count:3d}  {vals_str}  {crc_str}',
            color,
        )
