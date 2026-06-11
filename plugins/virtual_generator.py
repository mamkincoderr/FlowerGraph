from dataclasses import dataclass, field
import queue
import threading
import time
import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QSpinBox, QComboBox,
    QDoubleSpinBox, QDialogButtonBox, QTableWidget,
    QTableWidgetItem, QGroupBox, QHeaderView
)
from plugins.base_source import BaseSource


WAVEFORM_LABELS = ['Синус', 'Меандр', 'Пила', 'Шум', 'Постоянный']
WAVEFORM_KEYS   = ['sine',  'square', 'sawtooth', 'noise', 'constant']
SAMPLE_RATES    = [100, 500, 1000, 2000, 5000, 10000]


@dataclass
class ChannelConfig:
    waveform:  str   = 'sine'
    amplitude: float = 1.0
    frequency: float = 1.0
    noise_std: float = 0.0

    def to_dict(self) -> dict:
        return {'waveform': self.waveform, 'amplitude': self.amplitude,
                'frequency': self.frequency, 'noise_std': self.noise_std}

    @classmethod
    def from_dict(cls, d: dict) -> 'ChannelConfig':
        return cls(waveform=d.get('waveform', 'sine'), amplitude=d.get('amplitude', 1.0),
                   frequency=d.get('frequency', 1.0), noise_std=d.get('noise_std', 0.0))


@dataclass
class GeneratorConfig:
    n_channels:  int  = 4
    sample_rate: int  = 10_000
    channels: list[ChannelConfig] = field(
        default_factory=lambda: [
            ChannelConfig(waveform='sine',     amplitude=1.0, frequency=100.0),
            ChannelConfig(waveform='sine',     amplitude=0.8, frequency=200.0),
            ChannelConfig(waveform='square',   amplitude=1.0, frequency=50.0),
            ChannelConfig(waveform='sawtooth', amplitude=1.0, frequency=33.0),
        ]
    )

    def to_dict(self) -> dict:
        return {
            'n_channels': self.n_channels,
            'sample_rate': self.sample_rate,
            'channels': [c.to_dict() for c in self.channels[:self.n_channels]],
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'GeneratorConfig':
        channels = [ChannelConfig.from_dict(c) for c in d.get('channels', [])]
        n = d.get('n_channels', 4)
        while len(channels) < n:
            channels.append(ChannelConfig())
        return cls(n_channels=n, sample_rate=d.get('sample_rate', 10_000), channels=channels[:n])


class VirtualGenerator(BaseSource):
    _BATCH_MS   = 10    # мс между батчами — маленький размер = плавная прокрутка
    _DRAIN_MS   = 15   # мс между опросами очереди в главном потоке

    def __init__(self, config: GeneratorConfig | None = None):
        super().__init__()
        self._config = config or GeneratorConfig()
        self._phases        = np.zeros(self._config.n_channels)
        self._sample_count  = 0
        self._thread: threading.Thread | None = None
        self._queue: queue.Queue = queue.Queue()

        # Таймер в главном потоке — сливает очередь в UI-коллбэк
        self._drain_timer = QTimer()
        self._drain_timer.timeout.connect(self._drain_queue)

    # --- BaseSource interface ---

    def get_name(self) -> str:
        return 'Виртуальный генератор'

    def get_channel_count(self) -> int:
        return self._config.n_channels

    def get_channel_names(self) -> list[str]:
        return [f'CH{i + 1}' for i in range(self._config.n_channels)]

    def get_config_widget(self):
        return None

    def start(self):
        self._phases       = np.zeros(self._config.n_channels)
        self._sample_count = 0
        self._running      = True
        # Очищаем очередь от предыдущего сеанса
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        # Фоновый поток генерирует данные строго по расписанию
        self._thread = threading.Thread(target=self._gen_loop, daemon=True)
        self._thread.start()
        # Главный поток сливает очередь каждые _DRAIN_MS
        self._drain_timer.start(self._DRAIN_MS)

    def stop(self):
        self._running = False
        self._drain_timer.stop()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
            self._thread = None
        # Сливаем оставшиеся батчи (данные не теряем)
        self._drain_queue()

    # --- Config ---

    def set_config(self, config: GeneratorConfig):
        was_running = self._running
        if was_running:
            self.stop()
        self._config = config
        self._phases = np.zeros(config.n_channels)
        if was_running:
            self.start()

    # --- Генерация (фоновый поток) ---

    def _gen_loop(self):
        """Строго тактируемый фоновый поток — не зависит от UI."""
        interval = self._BATCH_MS / 1000.0
        next_tick = time.perf_counter()
        while self._running:
            self._generate_batch()
            next_tick += interval
            sleep = next_tick - time.perf_counter()
            if sleep > 0:
                time.sleep(sleep)
            else:
                # Накопилось отставание — сбрасываем, чтобы не накапливать
                next_tick = time.perf_counter()

    def _generate_batch(self):
        sr = self._config.sample_rate
        n  = max(1, int(sr * self._BATCH_MS / 1000))
        t_start = self._sample_count / sr
        times   = t_start + np.arange(n, dtype=np.float64) / sr
        self._sample_count += n

        values = np.empty((n, self._config.n_channels), dtype=np.float32)
        for i, ch in enumerate(self._config.channels):
            values[:, i] = self._make_wave(ch, n, i)

        # Кладём в очередь (thread-safe); главный поток заберёт
        self._queue.put((times, values))

    def _drain_queue(self):
        """Вызывается в главном потоке — сливает всё накопленное за интервал."""
        while True:
            try:
                times, values = self._queue.get_nowait()
                self._emit(times, values)
            except queue.Empty:
                break

    def _make_wave(self, ch: ChannelConfig, n: int, idx: int) -> np.ndarray:
        sr = self._config.sample_rate
        t = np.arange(n, dtype=np.float64) / sr
        phase = self._phases[idx]
        arg = 2.0 * np.pi * ch.frequency * t + phase

        if ch.waveform == 'sine':
            y = ch.amplitude * np.sin(arg)
        elif ch.waveform == 'square':
            y = ch.amplitude * np.sign(np.sin(arg))
        elif ch.waveform == 'sawtooth':
            y = ch.amplitude * (2.0 * ((ch.frequency * t + phase / (2 * np.pi)) % 1.0) - 1.0)
        elif ch.waveform == 'noise':
            y = ch.amplitude * np.random.randn(n)
        else:
            y = np.full(n, ch.amplitude, dtype=np.float64)

        if ch.noise_std > 0:
            y = y + ch.noise_std * np.random.randn(n)

        self._phases[idx] = (phase + 2.0 * np.pi * ch.frequency * n / sr) % (2.0 * np.pi)
        return y.astype(np.float32)


# ---------------------------------------------------------------------------

class VirtualGeneratorDialog(QDialog):
    def __init__(self, config: GeneratorConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Настройка виртуального генератора')
        self.setMinimumWidth(520)
        self._build_ui()
        self._load(config)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        grp = QGroupBox('Общие параметры')
        form = QFormLayout(grp)

        self._sb_channels = QSpinBox()
        self._sb_channels.setRange(1, 8)
        self._sb_channels.valueChanged.connect(self._on_channels_changed)
        form.addRow('Число каналов:', self._sb_channels)

        self._cb_rate = QComboBox()
        for r in SAMPLE_RATES:
            self._cb_rate.addItem(f'{r} Гц', r)
        form.addRow('Частота дискретизации:', self._cb_rate)
        layout.addWidget(grp)

        grp2 = QGroupBox('Параметры каналов')
        v2 = QVBoxLayout(grp2)
        self._table = QTableWidget()
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels(
            ['Форма сигнала', 'Амплитуда', 'Частота (Гц)', 'Шум σ']
        )
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.verticalHeader().setVisible(True)
        v2.addWidget(self._table)
        layout.addWidget(grp2)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

    def _load(self, cfg: GeneratorConfig):
        self._sb_channels.blockSignals(True)
        self._sb_channels.setValue(cfg.n_channels)
        self._sb_channels.blockSignals(False)

        idx = SAMPLE_RATES.index(cfg.sample_rate) if cfg.sample_rate in SAMPLE_RATES else 2
        self._cb_rate.setCurrentIndex(idx)
        self._rebuild_table(cfg.n_channels, cfg.channels)

    def _on_channels_changed(self, n: int):
        current = self._read_table()
        while len(current) < n:
            current.append(ChannelConfig())
        self._rebuild_table(n, current)

    def _rebuild_table(self, n: int, channels: list[ChannelConfig]):
        self._table.setRowCount(n)
        for i in range(n):
            ch = channels[i] if i < len(channels) else ChannelConfig()
            self._table.setVerticalHeaderItem(i, QTableWidgetItem(f'CH{i + 1}'))

            cb = QComboBox()
            for lbl in WAVEFORM_LABELS:
                cb.addItem(lbl)
            cb.setCurrentIndex(WAVEFORM_KEYS.index(ch.waveform) if ch.waveform in WAVEFORM_KEYS else 0)
            self._table.setCellWidget(i, 0, cb)

            for col, (val, lo, step) in enumerate(
                [(ch.amplitude, -1e6, 0.1),
                 (ch.frequency, 0.001, 0.5),
                 (ch.noise_std, 0.0,   0.01)],
                start=1
            ):
                sb = QDoubleSpinBox()
                sb.setRange(lo, 1e6)
                sb.setSingleStep(step)
                sb.setDecimals(3)
                sb.setValue(val)
                self._table.setCellWidget(i, col, sb)

    def _read_table(self) -> list[ChannelConfig]:
        result = []
        for i in range(self._table.rowCount()):
            wf = WAVEFORM_KEYS[self._table.cellWidget(i, 0).currentIndex()]
            amp   = self._table.cellWidget(i, 1).value()
            freq  = self._table.cellWidget(i, 2).value()
            noise = self._table.cellWidget(i, 3).value()
            result.append(ChannelConfig(waveform=wf, amplitude=amp,
                                        frequency=freq, noise_std=noise))
        return result

    def get_config(self) -> GeneratorConfig:
        return GeneratorConfig(
            n_channels=self._sb_channels.value(),
            sample_rate=self._cb_rate.currentData(),
            channels=self._read_table(),
        )
