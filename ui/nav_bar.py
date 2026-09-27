"""Compact overview, segment map and navigation controls for the plot."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QWidget, QGridLayout, QHBoxLayout, QVBoxLayout, QPushButton, QLabel,
)


class NavBar(QWidget):
    navigate_to = Signal(float, float)
    go_start = Signal()
    go_end = Signal()
    page_left = Signal()
    page_right = Signal()
    zoom_in = Signal()
    zoom_out = Signal()
    start_stop = Signal()
    pause_toggled = Signal(bool)

    _BG = '#f7f9fc'
    _INK = '#263445'
    _BLUE = '#2878c8'

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(98)
        self.setObjectName('navigationBar')
        self.setStyleSheet(
            'QWidget#navigationBar { background:#f3f6fa; border-top:1px solid #d5dde7; }'
        )

        self._ov_curves: list[pg.PlotDataItem] = []
        self._lane_lines: list[pg.InfiniteLine] = []
        self._segment_items: list[pg.GraphicsObject] = []
        self._segments: list[tuple[float, float, int]] = []
        self._active_segment = -1
        self._bounds: tuple[float, float] | None = None
        self._n_channels = 0
        self._following = True
        self._view_center = 0.5
        self._view_width = 1.0
        self._min_region_pixels = 6

        self._region = pg.LinearRegionItem(
            values=[0, 1],
            brush=pg.mkBrush(40, 120, 200, 32),
            pen=pg.mkPen(self._BLUE, width=1.2),
            swapMode='block',
        )
        self._region.setZValue(5)
        for line in self._region.lines:
            line.setMovable(False)
            line.setPen(pg.mkPen(self._BLUE, width=1.2))
        self._region.sigRegionChanged.connect(self._on_region_changed)

        self._build_ui()

    def _build_ui(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(8, 5, 8, 5)
        outer.setSpacing(8)

        nav = QWidget()
        nav.setFixedWidth(88)
        nv = QGridLayout(nav)
        nv.setContentsMargins(0, 0, 0, 0)
        nv.setHorizontalSpacing(4)
        nv.setVerticalSpacing(4)
        self._btn_start = self._nav_btn('◀|', 'В начало записи (Home)', self.go_start)
        self._btn_left = self._nav_btn('◀', 'На страницу назад (PgUp)', self.page_left)
        self._btn_right = self._nav_btn('▶', 'На страницу вперёд (PgDn)', self.page_right)
        self._btn_end = self._nav_btn('|▶', 'В конец блока (End)', self.go_end)
        nv.addWidget(self._btn_start, 0, 0)
        nv.addWidget(self._btn_left, 0, 1)
        nv.addWidget(self._btn_right, 1, 0)
        nv.addWidget(self._btn_end, 1, 1)
        outer.addWidget(nav)

        overview = QWidget()
        ov_layout = QVBoxLayout(overview)
        ov_layout.setContentsMargins(0, 0, 0, 0)
        ov_layout.setSpacing(1)

        self._info = QLabel('Нет данных для обзора')
        self._info.setFixedHeight(16)
        self._info.setStyleSheet(
            'color:#42536a; font-size:10px; font-weight:600; padding-left:3px;'
        )
        ov_layout.addWidget(self._info)

        self._ov = pg.PlotWidget()
        self._ov.setBackground(self._BG)
        self._ov.setMinimumHeight(68)
        self._ov.getAxis('left').hide()
        axis = self._ov.getAxis('bottom')
        axis.setHeight(20)
        axis.setPen(pg.mkPen('#9aa8b8', width=1))
        axis.setTextPen(pg.mkPen('#4c5d72'))
        axis.setStyle(tickFont=QFont('Segoe UI', 8), tickTextOffset=3)
        axis.setLabel('Время', units='с', color='#53657a', **{'font-size': '8pt'})
        self._ov.setMouseEnabled(x=False, y=False)
        self._ov.setMenuEnabled(False)
        self._ov.hideButtons()
        self._ov.plotItem.getViewBox().setBorder(pg.mkPen('#d2dbe6', width=1))
        self._ov.plotItem.getViewBox().setDefaultPadding(0)
        self._ov.addItem(self._region)
        self._ov.setToolTip(
            'Обзор всей записи. Перетащите синее окно или щёлкните по сигналу, '
            'чтобы перейти к участку. Серые промежутки обозначают разрывы между блоками.'
        )
        scene = self._ov.scene()
        if scene is not None:
            scene.sigMouseClicked.connect(self._on_ov_clicked)
        ov_layout.addWidget(self._ov, stretch=1)
        outer.addWidget(overview, stretch=1)

        scale = QWidget()
        scale.setFixedWidth(78)
        sv = QVBoxLayout(scale)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(3)
        btn_zi = QPushButton('+')
        btn_zi.setToolTip('Приблизить (колесо вверх)')
        btn_zi.clicked.connect(self.zoom_in)
        sv.addWidget(btn_zi)
        self._lbl = QLabel('?')
        self._lbl.setAlignment(Qt.AlignCenter)
        self._lbl.setFixedHeight(22)
        self._lbl.setStyleSheet(
            'font-weight:600; font-size:10px; color:#263445;'
            'border:1px solid #cad4e0; border-radius:4px; background:#fff; padding:0 3px;'
        )
        sv.addWidget(self._lbl)
        btn_zo = QPushButton('−')
        btn_zo.setToolTip('Отдалить (колесо вниз)')
        btn_zo.clicked.connect(self.zoom_out)
        sv.addWidget(btn_zo)
        outer.addWidget(scale)

        self._btn_pause = QPushButton('Пауза')
        self._btn_pause.setCheckable(True)
        self._btn_pause.setFixedSize(72, 82)
        self._btn_pause.setToolTip(
            'Остановить прокрутку экрана. Запись при этом продолжается.'
        )
        self._btn_pause.toggled.connect(self.pause_toggled)
        self._apply_pause_style(False)
        outer.addWidget(self._btn_pause)

        self._btn_startstop = QPushButton('▶  СТАРТ')
        self._btn_startstop.setFixedSize(98, 82)
        self._btn_startstop.clicked.connect(self.start_stop)
        self._apply_start_style(False)
        outer.addWidget(self._btn_startstop)

    @staticmethod
    def _nav_btn(text: str, tip: str, signal) -> QPushButton:
        button = QPushButton(text)
        button.setFixedSize(40, 38)
        button.setToolTip(tip)
        button.setStyleSheet(
            'QPushButton { color:#34465c; font-size:14px; font-weight:600;'
            ' background:#fff; border:1px solid #cbd5e1; border-radius:5px; }'
            'QPushButton:hover { background:#e8f1fb; border-color:#8eb5dd; }'
            'QPushButton:pressed { background:#d8e8f8; }'
        )
        button.clicked.connect(signal)
        return button

    def is_paused(self) -> bool:
        return self._btn_pause.isChecked()

    def set_following(self, following: bool):
        self._following = bool(following)

    def set_paused(self, paused: bool):
        self._btn_pause.blockSignals(True)
        self._btn_pause.setChecked(paused)
        self._btn_pause.blockSignals(False)
        self._apply_pause_style(paused)

    def _apply_pause_style(self, paused: bool):
        if paused:
            self._btn_pause.setStyleSheet(
                'QPushButton { background:#fff1cc; color:#744d00; font-weight:700;'
                ' border:1px solid #e5bd5f; border-radius:7px; }'
                'QPushButton:hover { background:#ffe7a8; }'
            )
        else:
            self._btn_pause.setStyleSheet(
                'QPushButton { background:#fff; color:#34465c; font-weight:600;'
                ' border:1px solid #cbd5e1; border-radius:7px; }'
                'QPushButton:hover { background:#f0f5fa; }'
            )

    def _apply_start_style(self, active: bool):
        if active:
            self._btn_startstop.setText('■  СТОП')
            self._btn_startstop.setStyleSheet(
                'QPushButton { background:#bd3b47; color:white; font-weight:700;'
                ' font-size:12px; border:0; border-radius:7px; }'
                'QPushButton:hover { background:#a8323d; }'
            )
        else:
            self._btn_startstop.setText('▶  СТАРТ')
            self._btn_startstop.setStyleSheet(
                'QPushButton { background:#24864a; color:white; font-weight:700;'
                ' font-size:12px; border:0; border-radius:7px; }'
                'QPushButton:hover { background:#1f7541; }'
            )

    def set_recording(self, active: bool):
        self._btn_end.setToolTip(
            'К концу записи и следить (End)' if active else 'В конец блока (End)'
        )
        self._apply_start_style(active)

    def setup_channels(self, n_channels: int, colors: list[str]):
        self._ov.clear()
        self._ov_curves.clear()
        self._lane_lines.clear()
        self._segment_items.clear()
        self._segments.clear()
        self._active_segment = -1
        self._bounds = None
        self._n_channels = max(0, int(n_channels))

        if self._n_channels > 1:
            for i in range(self._n_channels - 1):
                y = self._n_channels - i - 1.5
                line = pg.InfiniteLine(
                    pos=y, angle=0,
                    pen=pg.mkPen(190, 201, 214, 115, width=0.7),
                )
                line.setZValue(-4)
                self._ov.addItem(line)
                self._lane_lines.append(line)

        for i in range(self._n_channels):
            color = colors[i % len(colors)] if colors else '#627d98'
            curve = pg.PlotDataItem(
                pen=pg.mkPen(color=color, width=1.15),
                connect='finite',
                antialias=True,
            )
            curve.setZValue(1)
            self._ov.addItem(curve)
            self._ov_curves.append(curve)

        self._ov.addItem(self._region)
        for line in self._region.lines:
            line.setMovable(False)
        self._info.setText('Нет данных для обзора' if not n_channels else 'Ожидание данных…')
        if n_channels:
            self._ov.setYRange(-0.55, n_channels + 0.48, padding=0)

    def update_overview(
        self,
        times: np.ndarray,
        values: np.ndarray,
        x_bounds: tuple[float, float] | None = None,
    ):
        times = np.asarray(times, dtype=np.float64).reshape(-1)
        values = np.asarray(values, dtype=np.float64)
        if values.ndim == 1:
            values = values.reshape(-1, 1)
        if values.ndim != 2 or len(times) != len(values) or not len(times):
            return

        n_channels = min(len(self._ov_curves), values.shape[1])
        for i, curve in enumerate(self._ov_curves):
            if i >= n_channels:
                curve.setData([], [])
                continue
            data = values[:, i]
            finite = np.isfinite(data) & np.isfinite(times)
            if not np.any(finite):
                curve.setData([], [])
                continue
            center = float(np.nanmedian(data[finite]))
            deviation = np.abs(data[finite] - center)
            scale = float(np.nanpercentile(deviation, 95)) if len(deviation) else 0.0
            if not np.isfinite(scale) or scale <= 1e-15:
                scale = float(np.nanmax(deviation)) if len(deviation) else 1.0
            if not np.isfinite(scale) or scale <= 1e-15:
                scale = 1.0
            lane = self._n_channels - 1 - i
            lane_values = lane + np.clip((data - center) / scale, -1.0, 1.0) * 0.34
            lane_values[~np.isfinite(data) | ~np.isfinite(times)] = np.nan
            curve.setData(x=times, y=lane_values, connect='finite')

        finite_t = times[np.isfinite(times)]
        if not len(finite_t):
            return
        t0, t1 = float(np.min(finite_t)), float(np.max(finite_t))
        if x_bounds is not None:
            bound0, bound1 = map(float, x_bounds)
            if np.isfinite(bound0) and np.isfinite(bound1):
                t0, t1 = min(bound0, bound1), max(bound0, bound1)
        if t1 <= t0:
            t1 = t0 + 1e-9
        self._bounds = (t0, t1)
        self._ov.setYRange(-0.55, max(0.5, self._n_channels + 0.48), padding=0)
        self._ov.setXRange(t0, t1, padding=0)
        blocked = self._region.blockSignals(True)
        self._region.setBounds([t0, t1])
        self._apply_visual_region()
        self._region.blockSignals(blocked)
        if not self._segments:
            self._info.setText(
                f'\u041e\u0431\u0437\u043e\u0440 \u043f\u043e\u0442\u043e\u043a\u0430 \u0434\u0430\u043d\u043d\u044b\u0445  \u00b7  {self._format_seconds(t1 - t0)}'
            )

    def _apply_visual_region(self):
        """Keep a grabbable overview handle while retaining the true plot span."""
        if self._bounds is None:
            return
        lo, hi = self._bounds
        span = max(0.0, hi - lo)
        viewport_px = max(1, self._ov.viewport().width())
        min_width = span * min(1.0, self._min_region_pixels / viewport_px)
        visual_width = min(span, max(self._view_width, min_width))
        if self._view_width >= span:
            center = (lo + hi) / 2.0
        else:
            center = min(max(self._view_center, lo + visual_width / 2.0),
                         hi - visual_width / 2.0)
        self._region.setRegion([
            center - visual_width / 2.0,
            center + visual_width / 2.0,
        ])

    @staticmethod
    def _clamp_region(r0: float, r1: float, lo: float, hi: float):
        bounds_width = max(0.0, hi - lo)
        width = max(0.0, float(r1) - float(r0))
        width = min(width, bounds_width)
        left = min(max(float(r0), lo), hi - width)
        return left, left + width

    def set_view_region(self, t_min: float, t_max: float):
        self._view_width = max(0.0, float(t_max) - float(t_min))
        self._view_center = (float(t_min) + float(t_max)) / 2.0
        self._region.blockSignals(True)
        self._apply_visual_region()
        self._region.blockSignals(False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._bounds is not None:
            blocked = self._region.blockSignals(True)
            self._apply_visual_region()
            self._region.blockSignals(blocked)

    def update_scale_label(self, text: str):
        self._lbl.setText(text)

    def set_segments(
        self,
        segments: list[tuple[float, float, int]],
        active_index: int = -1,
    ):
        self._segments = [
            (float(start), float(end), int(count))
            for start, end, count in segments
        ]
        self._active_segment = (
            int(active_index) if 0 <= int(active_index) < len(self._segments)
            else (0 if self._segments else -1)
        )
        self._draw_segments()
        self._update_segment_info()

    def set_active_segment(self, active_index: int):
        if self._active_segment == int(active_index):
            return
        self._active_segment = int(active_index)
        self._draw_segments()
        self._update_segment_info()

    def _draw_segments(self):
        for item in self._segment_items:
            try:
                self._ov.removeItem(item)
            except Exception:
                pass
        self._segment_items.clear()
        if not self._segments:
            return

        y_top = float(self._n_channels)
        for i, (start, end, _count) in enumerate(self._segments):
            if i:
                prev_end = self._segments[i - 1][1]
                if start > prev_end:
                    gap = pg.LinearRegionItem(
                        values=(prev_end, start),
                        brush=pg.mkBrush(145, 158, 175, 32),
                        pen=pg.mkPen(None),
                        movable=False,
                        swapMode='block',
                    )
                    gap.setZValue(-2)
                    for line in gap.lines:
                        line.setMovable(False)
                    self._ov.addItem(gap)
                    self._segment_items.append(gap)
                    for edge in (prev_end, start):
                        boundary = pg.InfiniteLine(
                            pos=edge, angle=90,
                            pen=pg.mkPen('#8292a7', width=0.8, style=Qt.PenStyle.DashLine),
                        )
                        boundary.setZValue(2)
                        self._ov.addItem(boundary)
                        self._segment_items.append(boundary)

            active = i == self._active_segment
            color = self._BLUE if active else '#9aa9bc'
            bar = pg.PlotCurveItem(
                x=[start, end], y=[y_top, y_top],
                pen=pg.mkPen(color=color, width=4 if active else 2.5),
            )
            bar.setZValue(3)
            self._ov.addItem(bar)
            self._segment_items.append(bar)

            label = pg.TextItem(
                text=f'Б{i + 1}',
                color='#1e5c98' if active else '#52657c',
                anchor=(0.5, 0.5),
                fill=pg.mkBrush(255, 255, 255, 190),
            )
            label.setFont(QFont('Segoe UI', 7, QFont.Weight.DemiBold))
            label.setPos((start + end) / 2.0, y_top)
            label.setZValue(4)
            self._ov.addItem(label)
            self._segment_items.append(label)

    def _update_segment_info(self):
        if not self._segments:
            if self._bounds is None:
                self._info.setText('Нет данных для обзора')
            return
        if not (0 <= self._active_segment < len(self._segments)):
            self._active_segment = 0
        start, end, count = self._segments[self._active_segment]
        duration = max(0.0, end - start)
        self._info.setText(
            f'Блок {self._active_segment + 1} / {len(self._segments)}'
            f'   ·   {self._format_seconds(duration)}'
            f'   ·   {count:,} отсчётов'
            '     |     Серые промежутки — разрывы'
        )

    @staticmethod
    def _format_seconds(value: float) -> str:
        if value >= 10:
            return f'{value:.1f} с'
        if value >= 1:
            return f'{value:.2f} с'
        return f'{value * 1000:.1f} мс'

    def _on_region_changed(self):
        r0, r1 = self._region.getRegion()
        center = (float(r0) + float(r1)) / 2.0
        self._view_center = center
        t0, t1 = self._logical_range_at(center)
        self.navigate_to.emit(t0, t1)

    def _logical_range_at(self, center: float) -> tuple[float, float]:
        width = max(0.0, self._view_width)
        if self._bounds is not None:
            lo, hi = self._bounds
            if width >= hi - lo:
                center = (lo + hi) / 2.0
            else:
                center = min(max(float(center), lo + width / 2.0),
                             hi - width / 2.0)
        return float(center - width / 2.0), float(center + width / 2.0)

    def _on_ov_clicked(self, event):
        if event.button() != Qt.MouseButton.LeftButton or event.double():
            return
        viewbox = self._ov.plotItem.getViewBox()
        if not viewbox.sceneBoundingRect().contains(event.scenePos()):
            return
        t = float(viewbox.mapSceneToView(event.scenePos()).x())
        if self._view_width > 0:
            t0, t1 = self._logical_range_at(t)
            self._view_center = (t0 + t1) / 2.0
            blocked = self._region.blockSignals(True)
            self._apply_visual_region()
            self._region.blockSignals(blocked)
            self.navigate_to.emit(t0, t1)
