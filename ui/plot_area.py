"""
PlotArea — основная область графиков (без обзорного виджета).

Два режима:
  LIVE   — данные из RingBuffer; следящий режим прокручивает вправо.
  STATIC — полный numpy-массив; навигация через ViewBox; нарезка по видимому окну.

Навигация по времени — дискретная (1-2-5), как в PowerGraph.
Мышиное колесо → дискретный шаг.
"""

import types

import numpy as np
import pyqtgraph as pg
from PySide6.QtWidgets import QWidget, QVBoxLayout, QMenu
from PySide6.QtCore import QTimer, Signal, Qt
from PySide6.QtGui import QCursor

from core.ring_buffer import RingBuffer
from core.session import Block

# ---------------------------------------------------------------------------
# Дискретный ряд масштабов (время / деление)
# ---------------------------------------------------------------------------
TIME_DIV_SEQ: list[float] = [
    1e-6, 2e-6, 5e-6,
    1e-5, 2e-5, 5e-5,
    1e-4, 2e-4, 5e-4,
    1e-3, 2e-3, 5e-3,
    1e-2, 2e-2, 5e-2,
    0.1,  0.2,  0.5,
    1.0,  2.0,  5.0,  10.0, 20.0, 30.0, 60.0,
    120.0, 300.0, 600.0, 1200.0, 1800.0, 3600.0,
]
N_DIV        = 10    # делений по горизонтали
DEFAULT_IDX  = 12    # 10 мс/дел → окно 100 мс

# Дискретный ряд цен деления по Y (единицы данных / деление)
Y_DIV_SEQ: list[float] = [
    1000.0, 500.0, 200.0, 100.0, 50.0, 20.0, 10.0,
    5.0, 2.0, 1.0, 0.5, 0.2, 0.1,
    0.05, 0.02, 0.01, 0.005, 0.002, 0.001,
]
Y_DIV_DEFAULT_IDX = 9   # 1.0 / дел — значение по умолчанию


def fmt_y_div(v: float) -> str:
    """Форматировать цену деления Y для отображения в UI."""
    if v >= 1000: return f'{v/1000:g}k'
    if v >= 1:    return f'{v:g}'
    if v >= 0.001: return f'{v*1000:g}m'
    return f'{v:g}'


def fmt_time_div(t: float) -> str:
    if t < 1e-3:  return f'{t*1e6:g} мкс/дел'
    if t < 1.0:   return f'{t*1e3:g} мс/дел'
    if t < 60.0:  return f'{t:g} с/дел'
    if t < 3600.: return f'{t/60:g} мин/дел'
    return f'{t/3600:g} ч/дел'


# ---------------------------------------------------------------------------
CHANNEL_COLORS = [
    '#0000FF', '#FF0000', '#008000', '#FF8C00',
    '#800080', '#008B8B', '#8B4513', '#808080',
    '#FF1493', '#00008B', '#006400', '#DC143C',
]

# Стиль отображения графиков
PLOT_STYLE_LINE        = 'line'
PLOT_STYLE_LINE_POINTS = 'line_points'
PLOT_STYLE_POINTS      = 'points'
PLOT_STYLE_BARS        = 'bars'

PLOT_STYLE_LABELS = {
    PLOT_STYLE_LINE:        'Линия',
    PLOT_STYLE_LINE_POINTS: 'Линия + точки',
    PLOT_STYLE_POINTS:      'Точки',
    PLOT_STYLE_BARS:        'Столбики',
}

MAX_RING_SAMPLES   = 300_000  # ≤ 300 к отсчётов в кольцевом буфере (≈ 5 MB/канал)
MIN_RING_SEC       = 5        # минимум 5 секунд в кольцевом буфере
MAX_LIVE_OV_PTS    = 2_000    # точек в быстром обзорном буфере (LIVE режим)
DEFAULT_FPS        = 25
MAX_DISPLAY_PTS    = 8000
MAX_OVERVIEW_PTS   = 1500
OV_UPDATE_TICKS    = 15    # ~600 мс при 25 FPS


def _lod_decimate(t: np.ndarray, v: np.ndarray, max_pts: int):
    """Min/max LOD: сохраняет экстремумы сигнала."""
    n = len(t)
    if n <= max_pts:
        return t, v
    n_bins = max_pts // 2
    step = n // n_bins
    if step < 2:
        return t, v
    n_trunc = n_bins * step
    v0 = v[:n_trunc, 0].reshape(n_bins, step)
    imin = v0.argmin(axis=1)
    imax = v0.argmax(axis=1)
    base = np.arange(n_bins, dtype=np.intp) * step
    idx_min = base + imin
    idx_max = base + imax
    mask = imin <= imax
    t_a = np.where(mask, t[idx_min], t[idx_max])
    t_b = np.where(mask, t[idx_max], t[idx_min])
    v_a = np.where(mask[:, None], v[idx_min], v[idx_max])
    v_b = np.where(mask[:, None], v[idx_max], v[idx_min])
    t_out = np.empty(2 * n_bins, dtype=np.float64)
    t_out[0::2] = t_a
    t_out[1::2] = t_b
    v_out = np.empty((2 * n_bins, v.shape[1]), dtype=np.float32)
    v_out[0::2] = v_a
    v_out[1::2] = v_b
    return t_out, v_out


class PlotArea(QWidget):
    following_changed          = Signal(bool)
    markers_moved              = Signal(float, float)
    time_div_changed           = Signal(int)
    view_range_changed         = Signal(float, float)
    overview_ready             = Signal(object, object)
    cursor_moved               = Signal(float, object)   # (x_time, y_vals | None)
    selection_action_requested = Signal(str, float, float)  # action, t0, t1
    selection_changed          = Signal(float, float, int)  # t0, t1, n_samples

    def __init__(self, parent=None):
        super().__init__(parent)

        # --- данные ---
        self._buffer: RingBuffer | None     = None
        self._ov_buf: RingBuffer | None     = None   # лёгкий обзорный буфер (LIVE)
        self._n_channels  = 0
        self._sample_rate = 1000
        self._following   = True
        self._static_mode  = False
        self._static_dirty = False   # True = нужна перерисовка в статическом режиме
        self._static_times:  np.ndarray | None = None
        self._static_values: np.ndarray | None = None
        self._suppress_range_signal = False
        self._active_channel = 0     # канал под управлением Ctrl+=/−
        self._mouse_proxy    = None  # SignalProxy для отслеживания курсора

        # --- стиль графиков ---
        self._plot_style = PLOT_STYLE_LINE

        # --- масштаб ---
        self._time_div_idx = DEFAULT_IDX

        # --- каналы ---
        self._curves:         list[pg.PlotDataItem] = []
        self._legend:         pg.LegendItem | None  = None
        self._channel_colors: list[str]             = []
        self._scales:        np.ndarray = np.ones(0)    # визуальный масштаб (Y-выравнивание)
        self._offsets:       np.ndarray = np.zeros(0)   # визуальное смещение
        self._calib_coeff:   np.ndarray = np.ones(0)    # физическая калибровка A (Y = A·raw + B)
        self._calib_offset:  np.ndarray = np.zeros(0)   # физическая калибровка B
        self._visible: list[bool] = []

        # --- маркеры ---
        self._markers_on = False
        PEN_DASH = pg.QtCore.Qt.DashLine
        self._m1 = pg.InfiniteLine(
            pos=0, angle=90, movable=True,
            pen=pg.mkPen('#0070c0', width=1.5, style=PEN_DASH),
            label='M1', labelOpts={'position': 0.95, 'color': '#0070c0',
                                   'fill': (255, 255, 255, 180)})
        self._m2 = pg.InfiniteLine(
            pos=0, angle=90, movable=True,
            pen=pg.mkPen('#c00000', width=1.5, style=PEN_DASH),
            label='M2', labelOpts={'position': 0.85, 'color': '#c00000',
                                   'fill': (255, 255, 255, 180)})
        self._m1.sigPositionChanged.connect(self._on_m1_moved)
        self._m2.sigPositionChanged.connect(self._on_m2_moved)

        # --- выделение ---
        self._selection_item: pg.LinearRegionItem | None = None
        self._sel_anchor:     float | None               = None

        # --- виджет ---
        self._plot = pg.PlotWidget()
        self._apply_style()
        self._install_discrete_wheel()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.addWidget(self._plot)

        vb = self._plot.plotItem.getViewBox()
        vb.sigRangeChangedManually.connect(self._on_manual_zoom)
        vb.sigXRangeChanged.connect(self._on_x_range_changed)

        self._timer   = QTimer()
        self._timer.setInterval(1000 // DEFAULT_FPS)
        self._timer.timeout.connect(self._refresh)
        self._ov_tick = 0

        self._install_selection_drag()

    # ------------------------------------------------------------------
    # Стиль
    # ------------------------------------------------------------------

    def _apply_style(self):
        self._plot.setBackground('w')
        pen_ax = pg.mkPen('#404040', width=1)
        for nm in ('left', 'bottom'):
            ax = self._plot.getAxis(nm)
            ax.setPen(pen_ax)
            ax.setTextPen(pg.mkPen('#202020'))
        self._plot.getAxis('bottom').setLabel('Время, с')
        self._plot.showGrid(x=True, y=True, alpha=0.35)
        self._plot.setMouseEnabled(x=True, y=True)
        self._plot.plotItem.getViewBox().setBorder(pg.mkPen('#888888', width=1))

    # ------------------------------------------------------------------
    # Дискретное колесо
    # ------------------------------------------------------------------

    def _install_discrete_wheel(self):
        def _wheel(ev, axis=None):
            try:
                delta = ev.angleDelta().y()
            except AttributeError:
                delta = ev.delta()
            self.zoom_in_discrete() if delta > 0 else self.zoom_out_discrete()
            ev.accept()
        self._plot.plotItem.getViewBox().wheelEvent = _wheel

    def _install_mouse_tracking(self):
        """Подключить отслеживание позиции курсора над графиком."""
        scene = self._plot.plotItem.scene()
        if scene is None:
            return
        self._mouse_proxy = pg.SignalProxy(
            scene.sigMouseMoved,
            rateLimit=30,
            slot=self._on_mouse_moved,
        )

    def _on_mouse_moved(self, args):
        pos = args[0]
        vb  = self._plot.plotItem.getViewBox()
        if vb.sceneBoundingRect().contains(pos):
            mp = vb.mapSceneToView(pos)
            t  = float(mp.x())
            y_vals = self.get_values_at(t)
            self.cursor_moved.emit(t, y_vals)
        else:
            self.cursor_moved.emit(float('nan'), None)

    # ------------------------------------------------------------------
    # Инициализация каналов
    # ------------------------------------------------------------------

    def setup(self, n_channels: int, names: list[str], sample_rate: int):
        self._timer.stop()
        self._n_channels  = n_channels
        self._sample_rate = sample_rate
        self._following   = True
        self._static_mode = False
        self._static_times  = None
        self._static_values = None
        # Снять ограничения диапазона (живой режим — границы не фиксированы)
        self._plot.plotItem.getViewBox().setLimits(xMin=None, xMax=None)

        if self._legend is not None:
            try:
                self._legend.scene().removeItem(self._legend)
            except RuntimeError:
                pass
            self._plot.plotItem.legend = None
            self._legend = None

        self._plot.clear()
        self._curves.clear()
        self._channel_colors.clear()

        was_markers_on   = self._markers_on
        self._markers_on = False

        # Кольцевой буфер: не более MAX_RING_SAMPLES, но не менее MIN_RING_SEC
        capacity      = max(MIN_RING_SEC * sample_rate,
                            min(MAX_RING_SAMPLES, 30 * sample_rate))
        self._buffer  = RingBuffer(n_channels, capacity)
        # Лёгкий обзорный буфер: по 1 точке на каждый вызов push_data
        self._ov_buf  = RingBuffer(n_channels, MAX_LIVE_OV_PTS)
        self._scales       = np.ones(n_channels)
        self._offsets      = np.zeros(n_channels)
        self._calib_coeff  = np.ones(n_channels)
        self._calib_offset = np.zeros(n_channels)
        self._visible = [True] * n_channels

        self._legend = self._plot.addLegend(offset=(10, 10), labelTextColor='#202020')
        self._legend.setBrush(pg.mkBrush(255, 255, 255, 220))
        self._legend.setPen(pg.mkPen('#888888', width=1))

        for i in range(n_channels):
            color = CHANNEL_COLORS[i % len(CHANNEL_COLORS)]
            self._channel_colors.append(color)
            c = self._plot.plot(
                pen=pg.mkPen(color=color, width=1.5),
                name=names[i],
                connect='all',      # строго линейная интерполяция между точками
                antialias=True,
            )
            c.setDownsampling(auto=True, method='peak')
            c.setClipToView(True)
            self._apply_curve_style(c, color)
            self._curves.append(c)

        self._plot.enableAutoRange(axis='y', enable=True)
        self._apply_time_div()
        self._install_mouse_tracking()
        self.clear_selection()

        if was_markers_on:
            self.show_markers(True)

        self._timer.start()

    # ------------------------------------------------------------------
    # Загрузка блока
    # ------------------------------------------------------------------

    def load_block(self, block: Block):
        names = [ch.name for ch in block.channels]
        self.setup(n_channels=block.n_channels,
                   names=names,
                   sample_rate=block.sample_rate)

        # Восстановить физическую калибровку из метаданных блока
        for i, ch in enumerate(block.channels):
            if i < len(self._calib_coeff):
                self._calib_coeff[i]  = ch.scale
                self._calib_offset[i] = ch.offset

        self._static_times  = block.times.astype(np.float64)
        self._static_values = block.values.astype(np.float32)
        self._static_mode   = True
        self._static_dirty  = True   # первый рендер после загрузки
        self._following     = False
        self.following_changed.emit(False)

        t0, t1 = float(block.t_start), float(block.t_end)
        # Жёстко ограничить навигацию пределами записи
        self._plot.plotItem.getViewBox().setLimits(xMin=t0, xMax=t1)
        self._plot.setXRange(t0, t1, padding=0.02)
        self._plot.enableAutoRange(axis='y', enable=True)
        # Сразу сгенерировать обзорные данные
        self._emit_overview()

    # ------------------------------------------------------------------
    # Дискретный масштаб
    # ------------------------------------------------------------------

    @property
    def time_div_idx(self) -> int:
        return self._time_div_idx

    def zoom_in_discrete(self):
        if self._time_div_idx > 0:
            self._time_div_idx -= 1
            self._apply_time_div()

    def zoom_out_discrete(self):
        if self._time_div_idx < len(TIME_DIV_SEQ) - 1:
            self._time_div_idx += 1
            self._apply_time_div()

    def set_time_div_idx(self, idx: int):
        self._time_div_idx = max(0, min(idx, len(TIME_DIV_SEQ) - 1))
        self._apply_time_div()

    def _apply_time_div(self):
        t_div  = TIME_DIV_SEQ[self._time_div_idx]
        window = t_div * N_DIV

        try:
            self._plot.getAxis('bottom').setTickSpacing(major=t_div, minor=t_div / 5)
        except Exception:
            pass

        if (not self._static_mode and not self._following) or self._static_mode:
            vr = self._plot.plotItem.getViewBox().viewRange()[0]
            c  = (vr[0] + vr[1]) / 2
            self._static_dirty = True
            self._set_x_range(c - window / 2, c + window / 2)

        self.time_div_changed.emit(self._time_div_idx)

    # ------------------------------------------------------------------
    # Навигация
    # ------------------------------------------------------------------

    def page_left(self):
        vr = self._plot.plotItem.getViewBox().viewRange()[0]
        w  = vr[1] - vr[0]
        self._set_x_range(vr[0] - w, vr[1] - w)
        if not self._static_mode:
            self.set_following(False)

    def page_right(self):
        vr = self._plot.plotItem.getViewBox().viewRange()[0]
        w  = vr[1] - vr[0]
        self._set_x_range(vr[0] + w, vr[1] + w)
        if not self._static_mode:
            self.set_following(False)

    def go_to_start(self):
        t0 = self._get_t_start()
        if t0 is None:
            return
        w = TIME_DIV_SEQ[self._time_div_idx] * N_DIV
        self._set_x_range(t0, t0 + w)
        if not self._static_mode:
            self.set_following(False)

    def go_to_end(self):
        if self._static_mode and self._static_times is not None and len(self._static_times):
            t1 = float(self._static_times[-1])
            w  = TIME_DIV_SEQ[self._time_div_idx] * N_DIV
            self._set_x_range(t1 - w, t1)
        else:
            self.set_following(True)

    def set_view_range(self, t_min: float, t_max: float):
        """Установить диапазон X из NavBar (без повторной эмиссии view_range_changed)."""
        self._suppress_range_signal = True
        self._plot.setXRange(t_min, t_max, padding=0)
        self._suppress_range_signal = False
        if not self._static_mode:
            self.set_following(False)

    def _set_x_range(self, t_min: float, t_max: float):
        if t_min < 0.0:
            t_max -= t_min   # сохранить ширину окна
            t_min = 0.0
        self._plot.setXRange(t_min, t_max, padding=0)

    def scroll_left_div(self):
        """Сдвинуть вид влево на одно деление."""
        vr = self._plot.plotItem.getViewBox().viewRange()[0]
        step = TIME_DIV_SEQ[self._time_div_idx]
        self._set_x_range(vr[0] - step, vr[1] - step)
        if not self._static_mode:
            self.set_following(False)

    def scroll_right_div(self):
        """Сдвинуть вид вправо на одно деление."""
        vr = self._plot.plotItem.getViewBox().viewRange()[0]
        step = TIME_DIV_SEQ[self._time_div_idx]
        self._set_x_range(vr[0] + step, vr[1] + step)
        if not self._static_mode:
            self.set_following(False)

    def _get_t_start(self) -> float | None:
        if self._static_mode and self._static_times is not None and len(self._static_times):
            return float(self._static_times[0])
        if self._buffer and self._buffer.size >= 2:
            t, _ = self._buffer.get()
            return float(t[0]) if len(t) else None
        return None

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    # ==================================================================
    # Y-ось: выравнивание, масштабирование, сброс
    # ==================================================================

    def _get_channel_amplitudes(self) -> np.ndarray | None:
        """Максимальный модуль по каждому каналу из видимых данных."""
        if self._static_mode and self._static_values is not None:
            vr = self._plot.plotItem.getViewBox().viewRange()[0]
            t  = self._static_times
            i0 = max(0, int(np.searchsorted(t, float(vr[0]))) - 1)
            i1 = min(len(t), int(np.searchsorted(t, float(vr[1]))) + 2)
            v  = self._static_values[i0:i1]
            if len(v) < 2:
                v = self._static_values
        elif self._buffer and self._buffer.size >= 2:
            n = min(self._buffer.size, 10_000)
            _, v = self._buffer.get_last(n)
        else:
            return None
        if len(v) == 0:
            return None
        # Применяем калибровку перед вычислением амплитуд
        n_ch = v.shape[1]
        v_c = v.copy()
        for i in range(min(n_ch, len(self._calib_coeff))):
            A = float(self._calib_coeff[i]);  B = float(self._calib_offset[i])
            v_c[:, i] = v[:, i] * A + B
        return np.max(np.abs(v_c), axis=0)   # shape (n_channels,)

    def y_align_overlay(self) -> list[tuple[float, float]]:
        """Совместить базовые уровни: единый масштаб, все нули совмещены."""
        amps = self._get_channel_amplitudes()
        if amps is None:
            return []
        g = float(np.max(amps))
        if g < 1e-12:
            g = 1.0
        s = 1.0 / g
        for i in range(self._n_channels):
            self._scales[i]  = s
            self._offsets[i] = 0.0
        self._static_dirty = True
        return [(s, 0.0)] * self._n_channels

    def y_align_distribute(self) -> list[tuple[float, float]]:
        """Разнести по вертикали: единый масштаб, базовые уровни разнесены."""
        amps = self._get_channel_amplitudes()
        if amps is None:
            return []
        g = float(np.max(amps))
        if g < 1e-12:
            g = 1.0
        s       = 1.0 / g
        n       = self._n_channels
        spacing = 2.2
        results = []
        for i in range(n):
            center = ((n - 1) / 2.0 - i) * spacing
            self._scales[i]  = s
            self._offsets[i] = center
            results.append((s, center))
        self._static_dirty = True
        return results

    def y_align_auto(self) -> list[tuple[float, float]]:
        """Авто каждый: независимый масштаб, нули совмещены."""
        amps = self._get_channel_amplitudes()
        if amps is None:
            return []
        results = []
        for i in range(self._n_channels):
            a = max(float(amps[i]), 1e-12)
            s = 1.0 / a
            self._scales[i]  = s
            self._offsets[i] = 0.0
            results.append((s, 0.0))
        self._static_dirty = True
        return results

    def y_align_grids(self) -> list[tuple[float, float]]:
        """Совместить сетки: как overlay, но сохраняет относительные амплитуды."""
        return self.y_align_overlay()

    def y_reset(self) -> list[tuple[float, float]]:
        """Сброс: scale=1, offset=0 для всех каналов."""
        results = []
        for i in range(self._n_channels):
            self._scales[i]  = 1.0
            self._offsets[i] = 0.0
            results.append((1.0, 0.0))
        self._static_dirty = True
        return results

    def set_active_channel(self, idx: int):
        if 0 <= idx < self._n_channels:
            self._active_channel = idx

    def update_active_channel_yaxis(self, name: str, unit: str):
        """Обновить подпись оси Y для активного канала."""
        label = name
        if unit:
            label += f' [{unit}]'
        self._plot.getAxis('left').setLabel(label)

    @property
    def active_channel(self) -> int:
        return self._active_channel

    def y_zoom_active(self, factor: float) -> tuple[float, float] | None:
        """Масштабировать Y только активного канала."""
        i = self._active_channel
        if 0 <= i < self._n_channels:
            self._scales[i] *= factor
            self._static_dirty = True
            return self._scales[i], self._offsets[i]
        return None

    def shift_active_offset(self, delta_divs: float) -> tuple[float, float] | None:
        """Сдвинуть активный канал по вертикали на delta_divs делений."""
        i = self._active_channel
        if 0 <= i < self._n_channels:
            self._offsets[i] += delta_divs * self._scales[i]
            self._static_dirty = True
            return self._scales[i], self._offsets[i]
        return None

    def y_zoom_all(self, factor: float) -> list[tuple[float, float]]:
        """Масштабировать Y всех каналов (factor>1 = приблизить, <1 = отдалить)."""
        results = []
        for i in range(self._n_channels):
            self._scales[i] *= factor
            results.append((self._scales[i], self._offsets[i]))
        return results

    def clear_everything(self):
        """Полный сброс — для команды «Новый файл»."""
        self._timer.stop()
        if self._buffer:
            self._buffer.clear()
        if self._ov_buf:
            self._ov_buf.clear()
        self._static_mode   = False
        self._static_dirty  = False
        self._static_times  = None
        self._static_values = None
        self._following     = True
        self.clear_selection()
        self._plot.plotItem.getViewBox().setLimits(xMin=None, xMax=None)
        for c in self._curves:
            c.setData([], [])
        if self._n_channels > 0:
            self._scales  = np.ones(self._n_channels)
            self._offsets = np.zeros(self._n_channels)

    def freeze_live_view(self):
        """После остановки мониторинга: перейти в статический режим,
        показать весь кольцевой буфер как законченный снимок."""
        if self._buffer is None or self._buffer.size < 2:
            return
        times, values = self._buffer.get()
        if len(times) < 2:
            return
        self._static_times  = times
        self._static_values = values
        self._static_mode   = True
        self._static_dirty  = True
        self._following     = False
        self.following_changed.emit(False)
        t0, t1 = float(times[0]), float(times[-1])
        # Ограничить навигацию пределами буфера
        self._plot.plotItem.getViewBox().setLimits(xMin=t0, xMax=t1)
        self._plot.setXRange(t0, t1, padding=0.02)
        self._plot.enableAutoRange(axis='y', enable=True)
        self._emit_overview()
        if not self._timer.isActive():
            self._timer.start()

    def push_data(self, times: np.ndarray, values: np.ndarray):
        if self._buffer is not None and not self._static_mode:
            self._buffer.push(times, values)
            # Один отсчёт на батч — лёгкий обзорный буфер
            if self._ov_buf is not None and len(times):
                self._ov_buf.push(times[-1:], values[-1:])

    def stop(self):
        self._timer.stop()

    def get_channel_colors(self) -> list[str]:
        return list(self._channel_colors)

    def request_redraw(self):
        """Принудительная перерисовка после внешнего изменения scale/offset/visible."""
        self._static_dirty = True

    def set_y_autorange(self, enabled: bool):
        """Включить / выключить автомасштабирование по оси Y."""
        self._plot.enableAutoRange(axis='y', enable=enabled)

    def set_channel_scale(self, idx: int, scale: float):
        if 0 <= idx < len(self._scales):
            self._scales[idx] = scale
            self._static_dirty = True

    def set_channel_offset(self, idx: int, offset: float):
        if 0 <= idx < len(self._offsets):
            self._offsets[idx] = offset
            self._static_dirty = True

    def set_channel_visible(self, idx: int, visible: bool):
        if 0 <= idx < len(self._visible):
            self._visible[idx] = visible
            if idx < len(self._curves):
                self._curves[idx].setVisible(visible)
            self._static_dirty = True

    def set_channel_calib(self, idx: int, coeff: float, offset: float):
        """Установить физическую калибровку (A, B) для канала idx."""
        if 0 <= idx < len(self._calib_coeff):
            self._calib_coeff[idx]  = coeff
            self._calib_offset[idx] = offset
            self._static_dirty = True

    def set_all_calibrations(self, coeffs: list[float], offsets: list[float]):
        """Массовая установка калибровки для всех каналов."""
        for i in range(min(len(coeffs), len(self._calib_coeff))):
            self._calib_coeff[i]  = coeffs[i]
            self._calib_offset[i] = offsets[i]
        self._static_dirty = True

    def get_channel_calib(self, idx: int) -> tuple[float, float]:
        """Вернуть (coeff, offset) калибровки канала."""
        if 0 <= idx < len(self._calib_coeff):
            return float(self._calib_coeff[idx]), float(self._calib_offset[idx])
        return 1.0, 0.0

    def auto_scale_channel(self, idx: int) -> tuple[float, float] | None:
        v_src = self._static_values if self._static_mode else None
        if v_src is None and self._buffer and self._buffer.size >= 2:
            _, v_src = self._buffer.get_last(min(self._buffer.size, 10000))
        if v_src is None or idx >= v_src.shape[1]:
            return None
        # Работаем на откалиброванных данных
        raw = v_src[:, idx]
        A   = float(self._calib_coeff[idx])  if idx < len(self._calib_coeff)  else 1.0
        B   = float(self._calib_offset[idx]) if idx < len(self._calib_offset) else 0.0
        calib = raw * A + B
        vmax = float(np.max(np.abs(calib)))
        scale  = (1.0 / vmax) if vmax > 1e-12 else 1.0
        self._scales[idx]  = scale
        self._offsets[idx] = 0.0
        return scale, 0.0

    def get_values_at(self, t: float) -> np.ndarray | None:
        t_src = self._static_times if self._static_mode else None
        v_src = self._static_values if self._static_mode else None
        if t_src is None and self._buffer and self._buffer.size >= 2:
            t_src, v_src = self._buffer.get_last(min(self._buffer.size, 5000))
        if t_src is None or len(t_src) < 2:
            return None
        idx = int(np.searchsorted(t_src, t))
        idx = max(0, min(idx, len(t_src) - 1))
        raw = v_src[idx, :]
        n   = len(raw)
        calib_c = self._calib_coeff[:n]  if len(self._calib_coeff)  >= n else np.ones(n)
        calib_o = self._calib_offset[:n] if len(self._calib_offset) >= n else np.zeros(n)
        return ((raw * calib_c + calib_o) * self._scales[:n] + self._offsets[:n]).copy()

    def set_following(self, v: bool):
        self._following = v
        self.following_changed.emit(v)

    def show_markers(self, visible: bool):
        self._markers_on = visible
        items = self._plot.plotItem.items
        if visible:
            if self._m1 not in items:
                self._plot.addItem(self._m1)
            if self._m2 not in items:
                self._plot.addItem(self._m2)
            vr   = self._plot.plotItem.getViewBox().viewRange()[0]
            span = vr[1] - vr[0]
            self._m1.setPos(vr[0] + span * 0.3)
            self._m2.setPos(vr[0] + span * 0.7)
        else:
            for m in (self._m1, self._m2):
                try:
                    self._plot.removeItem(m)
                except Exception:
                    pass

    def set_fps(self, fps: int):
        self._timer.setInterval(max(15, 1000 // fps))

    # ------------------------------------------------------------------
    # Внутренние обработчики
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Выделение области данных (левый drag мышью)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Стиль отображения графиков
    # ------------------------------------------------------------------

    def _apply_curve_style(self, c: pg.PlotDataItem, color: str,
                           style: str | None = None):
        """Применить стиль к одной кривой, не трогая данные."""
        s = style or self._plot_style

        # Сбросить внутреннее состояние PlotCurveItem — устраняет артефакты
        # от предыдущего stepMode='center' (расширенный X-массив остаётся
        # в кэше и вызывает ValueError shapes mismatch при любом сдвиге вида)
        try:
            curve = c.curve
            curve.xData   = None
            curve.yData   = None
            curve.path    = None
            curve.stepMode = False
        except Exception:
            pass

        sym_brush = pg.mkBrush(color)
        sym_pen   = pg.mkPen(None)

        if s == PLOT_STYLE_LINE:
            c.setPen(pg.mkPen(color=color, width=1.5))
            c.setSymbol(None)
            c.setFillLevel(None)
            c.opts['stepMode'] = False

        elif s == PLOT_STYLE_LINE_POINTS:
            c.setPen(pg.mkPen(color=color, width=1))
            c.setSymbol('o')
            c.setSymbolSize(5)
            c.setSymbolPen(sym_pen)
            c.setSymbolBrush(sym_brush)
            c.setFillLevel(None)
            c.opts['stepMode'] = False

        elif s == PLOT_STYLE_POINTS:
            c.setPen(None)
            c.setSymbol('o')
            c.setSymbolSize(5)
            c.setSymbolPen(sym_pen)
            c.setSymbolBrush(sym_brush)
            c.setFillLevel(None)
            c.opts['stepMode'] = False

        elif s == PLOT_STYLE_BARS:
            # stepMode требует len(X)=len(Y)+1 — используем fillLevel=0 без stepMode
            # (заполнение до нуля даёт визуальный эффект «столбиков» при зуме)
            c.setPen(pg.mkPen(color=color, width=1))
            c.setSymbol(None)
            c.setFillLevel(0)
            c.setBrush(pg.mkBrush(color + '60'))
            c.opts['stepMode'] = False

        try:
            c.updateItems(True)
        except Exception:
            pass

    def set_plot_style(self, style: str):
        """Сменить стиль всех кривых (глобально)."""
        self._plot_style = style
        for i, c in enumerate(self._curves):
            color = self._channel_colors[i] if i < len(self._channel_colors) else '#888'
            self._apply_curve_style(c, color, style)

    def _find_nearest_time(self, t: float) -> float:
        """Найти время ближайшей реальной точки данных к t."""
        t_src = self._static_times if self._static_mode else None
        if t_src is None and self._buffer and self._buffer.size >= 2:
            t_src, _ = self._buffer.get_last(
                min(self._buffer.size, MAX_RING_SAMPLES))
        if t_src is None or len(t_src) < 2:
            return t
        idx = int(np.searchsorted(t_src, t))
        idx = max(0, min(idx, len(t_src) - 1))
        if idx > 0 and abs(float(t_src[idx - 1]) - t) < abs(float(t_src[idx]) - t):
            idx -= 1
        return float(t_src[idx])

    def _count_samples_in(self, t0: float, t1: float) -> int:
        """Число отсчётов в диапазоне [t0, t1]."""
        t_src = self._static_times if self._static_mode else None
        if t_src is None and self._buffer and self._buffer.size >= 2:
            t_src, _ = self._buffer.get_last(
                min(self._buffer.size, MAX_RING_SAMPLES))
        if t_src is None:
            return 0
        i0 = int(np.searchsorted(t_src, t0))
        i1 = int(np.searchsorted(t_src, t1, side='right'))
        return max(0, i1 - i0)

    def _install_selection_drag(self):
        """Перехватить drag ViewBox: левая кнопка → выделение, правая → pan."""
        vb = self._plot.plotItem.getViewBox()
        vb.setMouseMode(pg.ViewBox.RectMode)   # правый drag = pan
        _p = self

        def _drag(vb_self, ev, axis=None):
            if ev.button() == Qt.LeftButton:
                pos = vb_self.mapSceneToView(ev.scenePos())
                t   = _p._find_nearest_time(float(pos.x()))   # снеп к данным

                if ev.isStart():
                    _p._sel_anchor = t
                    if _p._selection_item is None:
                        _p._selection_item = pg.LinearRegionItem(
                            values=[t, t],
                            brush=pg.mkBrush(255, 180, 0, 55),
                            pen=pg.mkPen('#cc8800', width=1),
                            movable=True,
                        )
                        _p._selection_item.sigRegionChanged.connect(
                            _p._on_selection_region_changed)
                        _p._plot.addItem(_p._selection_item)
                    else:
                        _p._selection_item.setRegion([t, t])

                if _p._sel_anchor is not None and _p._selection_item:
                    t0 = min(_p._sel_anchor, t)
                    t1 = max(_p._sel_anchor, t)
                    _p._selection_item.setRegion([t0, t1])
                    n  = _p._count_samples_in(t0, t1)
                    _p.selection_changed.emit(t0, t1, n)

                if ev.isFinish():
                    _p._sel_anchor = None
                ev.accept()
            else:
                pg.ViewBox.mouseDragEvent(vb_self, ev, axis)

        vb.mouseDragEvent = types.MethodType(_drag, vb)

        # Одиночный левый клик (без движения) → снять выделение
        def _click(vb_self, ev):
            if ev.button() == Qt.LeftButton:
                _p.clear_selection()
                ev.accept()
            else:
                pg.ViewBox.mouseClickEvent(vb_self, ev)
        vb.mouseClickEvent = types.MethodType(_click, vb)

        def _dbl_click(vb_self, ev):
            if ev.button() == Qt.LeftButton and _p._selection_item is not None:
                r = _p._selection_item.getRegion()
                t0, t1 = float(r[0]), float(r[1])
                if t1 > t0 + 1e-9:
                    _p._suppress_range_signal = True
                    _p._plot.setXRange(t0, t1, padding=0.02)
                    _p._suppress_range_signal = False
                    _p._static_dirty = True
                    ev.accept()
                    return
            pg.ViewBox.mouseDoubleClickEvent(vb_self, ev)
        vb.mouseDoubleClickEvent = types.MethodType(_dbl_click, vb)

        orig_ctx = vb.__class__.raiseContextMenu

        def _ctx(vb_self, ev):
            if _p._selection_item is not None:
                r = _p._selection_item.getRegion()
                _p._show_selection_menu(float(r[0]), float(r[1]))
            else:
                orig_ctx(vb_self, ev)

        vb.raiseContextMenu = types.MethodType(_ctx, vb)

    def _on_selection_region_changed(self):
        if self._selection_item is not None:
            r = self._selection_item.getRegion()
            t0, t1 = float(r[0]), float(r[1])
            n = self._count_samples_in(t0, t1)
            self.selection_changed.emit(t0, t1, n)

    def clear_selection(self):
        """Снять выделение."""
        if self._selection_item is not None:
            try:
                self._plot.removeItem(self._selection_item)
            except Exception:
                pass
            self._selection_item = None
            self.selection_changed.emit(0.0, 0.0, 0)
        self._sel_anchor = None

    def get_selection(self) -> tuple[float, float] | None:
        if self._selection_item is None:
            return None
        r = self._selection_item.getRegion()
        return float(r[0]), float(r[1])

    def _show_selection_menu(self, t0: float, t1: float):
        menu = QMenu(self)
        dt   = t1 - t0
        menu.addAction(
            f'Копировать в новый блок  ({dt:.4f} с)',
            lambda: self.selection_action_requested.emit('copy', t0, t1),
        )
        menu.addAction(
            'Удалить выделенные данные',
            lambda: self.selection_action_requested.emit('delete', t0, t1),
        )
        menu.addSeparator()
        menu.addAction('Снять выделение', self.clear_selection)
        menu.exec(QCursor.pos())

    # ------------------------------------------------------------------
    # Маркеры M1/M2 со снепом к реальным точкам данных
    # ------------------------------------------------------------------

    def _snap_marker(self, marker: pg.InfiniteLine):
        """Привязать маркер к ближайшей реальной точке данных."""
        t     = marker.value()
        t_src = self._static_times if self._static_mode else None
        if t_src is None and self._buffer and self._buffer.size >= 2:
            # Ищем только в видимом диапазоне — быстро
            try:
                vr    = self._plot.plotItem.getViewBox().viewRange()[0]
                n_vis = max(2, int((vr[1] - vr[0]) * self._sample_rate) + 200)
            except Exception:
                n_vis = 10_000
            t_src, _ = self._buffer.get_last(min(n_vis, MAX_RING_SAMPLES))
        if t_src is None or len(t_src) < 2:
            return
        idx = int(np.searchsorted(t_src, t))
        idx = max(0, min(idx, len(t_src) - 1))
        if idx > 0 and abs(float(t_src[idx - 1]) - t) < abs(float(t_src[idx]) - t):
            idx -= 1
        snapped = float(t_src[idx])
        if snapped != t:
            marker.blockSignals(True)
            marker.setValue(snapped)
            marker.blockSignals(False)

    def _on_m1_moved(self, _=None):
        self._snap_marker(self._m1)
        self.markers_moved.emit(float(self._m1.value()), float(self._m2.value()))

    def _on_m2_moved(self, _=None):
        self._snap_marker(self._m2)
        self.markers_moved.emit(float(self._m1.value()), float(self._m2.value()))

    def _on_manual_zoom(self, _=None):
        if self._following and not self._static_mode:
            self._following = False
            self.following_changed.emit(False)

    def _on_x_range_changed(self, _vb=None, x_range=None):
        if self._static_mode:
            self._static_dirty = True   # пользователь сдвинул вид → нужен рендер
        # В следящем режиме NavBar обновляется только раз в ~600 мс (в _emit_overview),
        # иначе он перерисовывается 25 раз/с и блокирует основной поток.
        if (not self._suppress_range_signal
                and x_range is not None
                and not self._following):
            self.view_range_changed.emit(float(x_range[0]), float(x_range[1]))

    # ------------------------------------------------------------------
    # Получение видимых данных
    # ------------------------------------------------------------------

    def _get_view_data(self) -> tuple[np.ndarray | None, np.ndarray | None]:
        if self._static_mode and self._static_times is not None:
            vr   = self._plot.plotItem.getViewBox().viewRange()[0]
            t_lo = float(vr[0])
            t_hi = float(vr[1])
            t    = self._static_times
            i0   = max(0, int(np.searchsorted(t, t_lo)) - 1)
            i1   = min(len(t), int(np.searchsorted(t, t_hi)) + 2)
            if i0 >= i1:
                return None, None
            return t[i0:i1], self._static_values[i0:i1]

        if self._buffer is None or self._buffer.size < 2:
            return None, None

        if self._following:
            n = max(2, int(TIME_DIV_SEQ[self._time_div_idx] * N_DIV * self._sample_rate) + 1)
            n = min(n, MAX_RING_SAMPLES)  # колесо не может вызвать копирование > ёмкости буфера
            return self._buffer.get_last(n)
        else:
            # Non-following LIVE: используем get_last для размера текущего окна.
            # buffer.get() (полная копия буфера) здесь нельзя — слишком дорого.
            vr       = self._plot.plotItem.getViewBox().viewRange()[0]
            t_lo     = float(vr[0])
            t_hi     = float(vr[1])
            n_window = max(2, int((t_hi - t_lo) * self._sample_rate) + 2)
            n_window = min(n_window, MAX_RING_SAMPLES)  # не больше ёмкости буфера
            t, v = self._buffer.get_last(n_window)
            if len(t) < 2:
                return None, None
            # Срезаем до видимого окна (get_last отдаёт самые свежие данные)
            i0 = max(0, int(np.searchsorted(t, t_lo)) - 1)
            i1 = min(len(t), int(np.searchsorted(t, t_hi)) + 2)
            if i0 >= i1:
                return t, v   # показываем всё что есть
            return t[i0:i1], v[i0:i1]

    # ------------------------------------------------------------------
    # Отрисовка
    # ------------------------------------------------------------------

    def _refresh(self):
        # В статическом режиме рисуем только когда пользователь что-то сдвинул.
        # Это предотвращает зависание модальных диалогов (save, open и т.д.)
        if self._static_mode:
            if not self._static_dirty:
                return
            self._static_dirty = False

        t, v = self._get_view_data()
        if t is None or len(t) < 2:
            return

        if len(t) > MAX_DISPLAY_PTS:
            t, v = _lod_decimate(t, v, MAX_DISPLAY_PTS)

        for i, curve in enumerate(self._curves):
            if not self._visible[i]:
                continue
            A = float(self._calib_coeff[i])  if i < len(self._calib_coeff)  else 1.0
            B = float(self._calib_offset[i]) if i < len(self._calib_offset) else 0.0
            y = (v[:, i] * A + B) * self._scales[i] + self._offsets[i]
            curve.setData(x=t, y=y)

        if self._following and not self._static_mode:
            t_max = float(t[-1])
            w     = TIME_DIV_SEQ[self._time_div_idx] * N_DIV
            self._set_x_range(t_max - w, t_max)

        # Обзор реже
        self._ov_tick += 1
        if self._ov_tick >= OV_UPDATE_TICKS:
            self._ov_tick = 0
            self._emit_overview()

    def _emit_overview(self):
        if self._static_mode and self._static_times is not None:
            # STATIC: полные данные, нарезаем один раз при загрузке
            t_all, v_all = self._static_times, self._static_values
            if len(t_all) > MAX_OVERVIEW_PTS:
                idx  = np.round(np.linspace(0, len(t_all)-1, MAX_OVERVIEW_PTS)).astype(np.intp)
                t_ov = t_all[idx]
                v_ov = v_all[idx]
            else:
                t_ov, v_ov = t_all, v_all
        elif self._ov_buf is not None and self._ov_buf.size >= 2:
            # LIVE: лёгкий буфер (≤ MAX_LIVE_OV_PTS точек) — без копирования гигантских массивов
            t_ov, v_ov = self._ov_buf.get()
        else:
            return

        if len(t_ov) < 2:
            return

        # Применяем калибровку + визуальный масштаб/смещение
        v_scaled = v_ov.astype(np.float32, copy=True)
        for i in range(min(v_scaled.shape[1], len(self._scales))):
            A = float(self._calib_coeff[i])  if i < len(self._calib_coeff)  else 1.0
            B = float(self._calib_offset[i]) if i < len(self._calib_offset) else 0.0
            v_scaled[:, i] = (v_ov[:, i] * A + B) * self._scales[i] + self._offsets[i]

        self.overview_ready.emit(t_ov, v_scaled)
        # В следящем режиме обновляем регион NavBar здесь (раз в ~600 мс),
        # а не каждый кадр через sigXRangeChanged.
        if self._following:
            vr = self._plot.plotItem.getViewBox().viewRange()[0]
            self.view_range_changed.emit(float(vr[0]), float(vr[1]))
