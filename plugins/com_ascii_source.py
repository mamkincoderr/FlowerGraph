"""
COM-ASCII источник данных — формат PowerGraph.

Формат пакета (от CH32V303):
  N каналов × 8 байт + CR (0x0D)

Каждые 8 байт канала:
  [знак]['d']['d']['d']['d']['.']['d'][' ']
  где знак = '0' (положит.) или '-' (отрицат.)
  пример: b"00300.0 " → +300.0
           b"-01234.5 " → -1234.5

Поток данных:
  background thread читает порт → queue → drain QTimer → _emit() → MainWindow._on_data()
"""

import queue
import threading
import time
from dataclasses import dataclass

import numpy as np
import serial
import serial.tools.list_ports
from PySide6.QtCore import QTimer

from plugins.base_source import BaseSource


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

@dataclass
class ComAsciiConfig:
    port:         str = 'COM4'
    baudrate:     int = 460800
    n_channels:   int = 0          # 0 = авто-определение по первому пакету
    channel_names: list[str] | None = None


# ---------------------------------------------------------------------------
# Плагин
# ---------------------------------------------------------------------------

class ComAsciiSource(BaseSource):
    _DRAIN_MS      = 15    # мс между опросами очереди в главном потоке
    _AUTODETECT_TIMEOUT = 3.0   # с ожидания первого пакета

    def __init__(self, config: ComAsciiConfig | None = None):
        super().__init__()
        self._config      = config or ComAsciiConfig()
        self._queue: queue.Queue = queue.Queue()
        self._port: serial.Serial | None = None
        self._thread: threading.Thread | None = None

        # Счётчики для диагностики
        self._pkt_ok    = 0
        self._pkt_err   = 0
        self._t_start   = 0.0
        self._sample_count = 0

        # Авто-определение числа каналов
        self._n_ch_detected = 0   # 0 = ещё не определено

        # Частота с непрерывной калибровкой (rate-locked timestamps)
        self._rate_est  = 0.0
        self._t_base    = 0.0
        self._recal_at  = 500

        # Авто-реконнект
        self._reconnect_delay = 2.0   # с между попытками
        self._reconnect_count = 0

        self._drain_timer = QTimer()
        self._drain_timer.timeout.connect(self._drain_queue)

    # ------------------------------------------------------------------
    # BaseSource interface
    # ------------------------------------------------------------------

    def get_name(self) -> str:
        return f'COM-ASCII ({self._config.port} {self._config.baudrate})'

    def get_channel_count(self) -> int:
        return self._n_ch_detected or self._config.n_channels or 4

    def get_channel_names(self) -> list[str]:
        n = self.get_channel_count()
        if self._config.channel_names and len(self._config.channel_names) >= n:
            return self._config.channel_names[:n]
        return [f'CH{i + 1}' for i in range(n)]

    def get_config_widget(self):
        return None

    def start(self):
        try:
            self._port = serial.Serial(
                port        = self._config.port,
                baudrate    = self._config.baudrate,
                bytesize    = serial.EIGHTBITS,
                parity      = serial.PARITY_NONE,
                stopbits    = serial.STOPBITS_ONE,
                timeout     = 0.05,
            )
        except serial.SerialException as e:
            self._emit_error(f'Не удалось открыть {self._config.port}: {e}')
            return

        self._running       = True
        self._t_start       = time.perf_counter()
        self._sample_count  = 0
        self._pkt_ok        = 0
        self._pkt_err       = 0
        self._n_ch_detected = self._config.n_channels  # 0 = авто
        self._rate_est      = self._rate_from_baud()   # стартовая оценка
        self._t_base        = 0.0

        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

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
            try:
                self._port.close()
            except Exception:
                pass
        self._port = None
        # Сливаем остаток
        self._drain_queue()

    # ------------------------------------------------------------------
    # Фоновый поток чтения
    # ------------------------------------------------------------------

    def _read_loop(self):
        buf = bytearray()
        while self._running:
            # Порт не открыт — пытаемся переподключиться
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
                        port=self._config.port,
                        baudrate=self._config.baudrate,
                        bytesize=serial.EIGHTBITS,
                        parity=serial.PARITY_NONE,
                        stopbits=serial.STOPBITS_ONE,
                        timeout=0.05,
                    )
                    buf.clear()
                    self._emit_error(
                        f'Реконнект успешен ({self._config.port})'
                    )
                except serial.SerialException:
                    pass   # следующая попытка через _reconnect_delay
                continue

            try:
                waiting = self._port.in_waiting
                if waiting:
                    chunk = self._port.read(waiting)
                    buf.extend(chunk)
                    self._process_buf(buf)
                else:
                    time.sleep(0.001)
            except serial.SerialException as e:
                self._emit_error(f'Потеря связи: {e}')
                buf.clear()
                try:
                    self._port.close()
                except Exception:
                    pass
                self._port = None   # триггер для авто-реконнекта

    def _process_buf(self, buf: bytearray):
        """Извлекаем полные пакеты из буфера (разделитель CR=0x0D)."""
        while True:
            cr = buf.find(0x0D)
            if cr < 0:
                # Накопилось слишком много без разделителя — сброс
                if len(buf) > 512:
                    buf.clear()
                break
            line = bytes(buf[:cr])
            # Пропускаем дополнительный LF (0x0A) после CR если есть
            skip = cr + 1
            if skip < len(buf) and buf[skip] == 0x0A:
                skip += 1
            del buf[:skip]

            if line:
                self._parse_line(line)

    def _rate_from_baud(self) -> float:
        """Оценка частоты из бод-рейта (стартовое значение)."""
        n_ch = self._n_ch_detected or self._config.n_channels or 4
        bytes_per_pkt = n_ch * 8 + 1   # +1 CR
        return self._config.baudrate / (bytes_per_pkt * 10.0)

    def _calibrate_rate(self):
        """
        Пересчитать частоту по реальному времени стены.

        Ключевое отличие от наивного пересчёта: при изменении rate_est
        корректируется t_base так, чтобы текущая метка времени НЕ изменилась.
        Меняется только наклон БУДУЩИХ меток → никаких скачков назад/вперёд.

            t = t_base + count / rate_est

        При калибровке:
            new_t_base = t_base + count/old_rate - count/new_rate
            ⟹ t_base + count/new_rate + Δ = t_base + count/old_rate  (непрерывно)
        """
        elapsed = time.perf_counter() - self._t_start
        if elapsed < 1.0 or self._sample_count < 500:
            return

        measured  = self._sample_count / elapsed
        # Мягкое сближение: 15% от разницы за одну итерацию
        new_rate  = self._rate_est + 0.15 * (measured - self._rate_est)

        # Сохраняем непрерывность: пересчитываем t_base
        # t_old = t_base + count/old_rate
        # t_new = new_t_base + count/new_rate   должно быть = t_old
        self._t_base += self._sample_count * (1.0 / self._rate_est
                                               - 1.0 / new_rate)
        self._rate_est = new_rate

    def _parse_line(self, line: bytes):
        n = len(line)
        # Пакет PG: N × 8 байт (каждый канал = 8 символов)
        if n == 0 or n % 8 != 0:
            self._pkt_err += 1
            return

        n_ch = n // 8

        # Авто-определение числа каналов по первому пакету
        if self._n_ch_detected == 0:
            self._n_ch_detected = n_ch
            self._rate_est = self._rate_from_baud()  # обновить с n_ch

        if n_ch != self._n_ch_detected:
            self._pkt_err += 1
            return

        values = np.empty((1, n_ch), dtype=np.float32)

        try:
            for i in range(n_ch):
                seg = line[i * 8 : (i + 1) * 8]
                sign    = seg[0]
                val_str = bytes([seg[1], seg[2], seg[3], seg[4],
                                 seg[5], seg[6]]).decode('ascii')
                val = float(val_str)
                if sign == ord('-'):
                    val = -val
                values[0, i] = val
        except (ValueError, UnicodeDecodeError, IndexError):
            self._pkt_err += 1
            return

        # Rate-locked timestamp с непрерывной калибровкой
        # t = t_base + count / rate_est  — при калибровке t_base корректируется,
        # поэтому переход абсолютно непрерывен (нет ни скачков, ни возвратов)
        self._sample_count += 1
        t = self._t_base + self._sample_count / self._rate_est

        # Калибровка каждые _recal_at пакетов
        if self._sample_count % self._recal_at == 0:
            self._calibrate_rate()

        times = np.array([t], dtype=np.float64)
        self._queue.put((times, values))
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

    def effective_sample_rate(self) -> int:
        """Откалиброванная частота дискретизации (Гц)."""
        return int(self._rate_est) if self._rate_est > 0 else 1000

    @property
    def stats(self) -> dict:
        elapsed = max(0.001, time.perf_counter() - self._t_start)
        return {
            'port':      self._config.port,
            'baudrate':  self._config.baudrate,
            'n_ch':      self._n_ch_detected,
            'pkt_ok':    self._pkt_ok,
            'pkt_err':   self._pkt_err,
            'pkt_rate':  int(self._pkt_ok / elapsed),
            'rate_est':  int(self._rate_est),
            'elapsed':   elapsed,
        }
