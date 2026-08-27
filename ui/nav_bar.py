"""
NavBar — нижняя панель навигации по времени.

Компоновка слева направо:
  [|◀][◀][▶][▶|]  [=== обзорный график (весь диапазон) ===]  [─][масштаб][+]

Кнопки:
  |◀  — В начало
  ◀   — Предыдущая страница (сдвиг на ширину окна влево)
  ▶   — Следующая страница
  ▶|  — В конец / следить

Обзор:
  LinearRegionItem показывает текущее окно просмотра.
  Перетаскивание региона = навигация.
"""

import numpy as np
import pyqtgraph as pg
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QPushButton, QLabel
)
from PySide6.QtCore import Signal, Qt


class NavBar(QWidget):
    navigate_to = Signal(float, float)   # пользователь перетащил регион
    go_start    = Signal()
    go_end      = Signal()
    page_left   = Signal()
    page_right  = Signal()
    zoom_in     = Signal()               # уменьшить время/дел
    zoom_out    = Signal()               # увеличить время/дел
    start_stop  = Signal()               # кнопка Старт/Стоп

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(68)

        self._ov_curves: list[pg.PlotDataItem] = []
        self._block_lines: list[pg.InfiniteLine] = []
        self._region = pg.LinearRegionItem(
            values=[0, 1],
            brush=pg.mkBrush(0, 100, 200, 45),
            pen=pg.mkPen('#0064c8', width=1),
            swapMode='block',
        )
        for ln in self._region.lines:
            ln.setMovable(False)
        self._region.sigRegionChanged.connect(self._on_region_changed)

        self._build_ui()

    # ------------------------------------------------------------------
    # Построение интерфейса
    # ------------------------------------------------------------------

    def _build_ui(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(4, 2, 4, 2)
        outer.setSpacing(6)

        # --- Кнопки навигации ---
        nav = QWidget()
        nav.setFixedWidth(100)
        nv = QVBoxLayout(nav)
        nv.setContentsMargins(0, 0, 0, 0)
        nv.setSpacing(2)

        row1 = QHBoxLayout()
        row1.setSpacing(2)
        row2 = QHBoxLayout()
        row2.setSpacing(2)

        self._btn_start = self._nav_btn('|◀', 'В начало (Home)',              self.go_start)
        self._btn_left  = self._nav_btn('◀',  'Предыдущая страница (PgUp)',   self.page_left)
        self._btn_right = self._nav_btn('▶',  'Следующая страница (PgDn)',    self.page_right)
        self._btn_end   = self._nav_btn('▶|', 'В конец блока (End)',          self.go_end)

        row1.addWidget(self._btn_start)
        row1.addWidget(self._btn_left)
        row2.addWidget(self._btn_right)
        row2.addWidget(self._btn_end)
        nv.addLayout(row1)
        nv.addLayout(row2)
        outer.addWidget(nav)

        # --- Обзорный график ---
        self._ov = pg.PlotWidget()
        self._ov.setBackground('#f2f2f2')
        self._ov.getAxis('left').hide()
        ax_b = self._ov.getAxis('bottom')
        ax_b.setHeight(16)
        ax_b.setPen(pg.mkPen('#606060', width=1))
        ax_b.setTextPen(pg.mkPen('#404040'))
        self._ov.setMouseEnabled(x=False, y=False)
        self._ov.plotItem.getViewBox().setBorder(pg.mkPen('#b0b0b0', width=1))
        self._ov.addItem(self._region)
        scene = self._ov.scene()
        if scene is not None:
            scene.sigMouseClicked.connect(self._on_ov_clicked)
        outer.addWidget(self._ov, stretch=1)

        # --- Масштаб ---
        scale_w = QWidget()
        scale_w.setFixedWidth(82)
        sv = QVBoxLayout(scale_w)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(2)

        btn_zi = QPushButton('+')
        btn_zi.setFixedHeight(20)
        btn_zi.setToolTip('Приблизить (колесо вверх)')
        btn_zi.clicked.connect(self.zoom_in)
        sv.addWidget(btn_zi)

        self._lbl = QLabel('?')
        self._lbl.setAlignment(Qt.AlignCenter)
        self._lbl.setFixedHeight(20)
        self._lbl.setStyleSheet(
            'font-weight: bold; font-size: 10px;'
            'border: 1px solid #aaa; background: #ffffff; padding: 0 2px;'
        )
        sv.addWidget(self._lbl)

        btn_zo = QPushButton('−')
        btn_zo.setFixedHeight(20)
        btn_zo.setToolTip('Отдалить (колесо вниз)')
        btn_zo.clicked.connect(self.zoom_out)
        sv.addWidget(btn_zo)

        outer.addWidget(scale_w)

        # --- Кнопка Старт/Стоп ---
        self._btn_startstop = QPushButton('▶  СТАРТ')
        self._btn_startstop.setFixedSize(100, 64)
        self._btn_startstop.setStyleSheet(
            'QPushButton { background:#1e8e3e; color:white; font-weight:bold;'
            '  font-size:13px; border-radius:6px; }'
            'QPushButton:hover { background:#1a7a34; }'
            'QPushButton:pressed { background:#155f28; }'
        )
        self._btn_startstop.clicked.connect(self.start_stop)
        outer.addWidget(self._btn_startstop)

    @staticmethod
    def _nav_btn(text: str, tip: str, signal) -> QPushButton:
        b = QPushButton(text)
        b.setFixedSize(46, 28)
        b.setToolTip(tip)
        b.clicked.connect(signal)
        return b

    # ------------------------------------------------------------------
    # Публичные методы
    # ------------------------------------------------------------------

    def set_recording(self, active: bool):
        """Переключить вид кнопки Старт/Стоп и смысл ▶|."""
        if active:
            self._btn_end.setToolTip('К концу записи и следить (End)')
        else:
            self._btn_end.setToolTip('В конец блока (End)')
        if active:
            self._btn_startstop.setText('■  СТОП')
            self._btn_startstop.setStyleSheet(
                'QPushButton { background:#c00000; color:white; font-weight:bold;'
                '  font-size:13px; border-radius:6px; }'
                'QPushButton:hover { background:#a50000; }'
                'QPushButton:pressed { background:#880000; }'
            )
        else:
            self._btn_startstop.setText('▶  СТАРТ')
            self._btn_startstop.setStyleSheet(
                'QPushButton { background:#1e8e3e; color:white; font-weight:bold;'
                '  font-size:13px; border-radius:6px; }'
                'QPushButton:hover { background:#1a7a34; }'
                'QPushButton:pressed { background:#155f28; }'
            )

    def setup_channels(self, n_channels: int, colors: list[str]):
        """Пересоздать кривые обзора при смене источника."""
        self._ov.clear()
        self._ov_curves.clear()
        self._block_lines.clear()
        for i in range(n_channels):
            color = colors[i % len(colors)]
            c = self._ov.plot(pen=pg.mkPen(color=color, width=1))
            self._ov_curves.append(c)
        self._ov.addItem(self._region)
        for ln in self._region.lines:
            ln.setMovable(False)

    def update_overview(self, times: np.ndarray, values: np.ndarray):
        """Обновить кривые обзора (данные уже прорежены до MAX_OVERVIEW_PTS)."""
        if len(times) < 2:
            return
        for i, c in enumerate(self._ov_curves):
            if i < values.shape[1]:
                c.setData(x=times, y=values[:, i])
        self._ov.setXRange(float(times[0]), float(times[-1]), padding=0.01)

    def set_view_region(self, t_min: float, t_max: float):
        """Сдвинуть выделенный регион без эмиссии сигнала navigate_to."""
        self._region.blockSignals(True)
        self._region.setRegion([t_min, t_max])
        self._region.blockSignals(False)

    def update_scale_label(self, text: str):
        self._lbl.setText(text)

    def mark_blocks(self, block_starts: list[float]):
        """Нарисовать вертикальные линии-разделители между блоками."""
        for line in self._block_lines:
            try:
                self._ov.removeItem(line)
            except Exception:
                pass
        self._block_lines.clear()
        for t in block_starts[1:]:   # первый блок без линии
            line = pg.InfiniteLine(
                pos=t, angle=90,
                pen=pg.mkPen('#888888', width=1, style=pg.QtCore.Qt.DashLine)
            )
            self._ov.addItem(line)
            self._block_lines.append(line)

    # ------------------------------------------------------------------

    def _on_region_changed(self):
        r = self._region.getRegion()
        self.navigate_to.emit(float(r[0]), float(r[1]))

    def _on_ov_clicked(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton or ev.double():
            return
        vb = self._ov.plotItem.getViewBox()
        if not vb.sceneBoundingRect().contains(ev.scenePos()):
            return
        t = float(vb.mapSceneToView(ev.scenePos()).x())
        r0, r1 = self._region.getRegion()
        if r0 <= t <= r1:
            return
        w = r1 - r0
        if w <= 0:
            return
        self.navigate_to.emit(t - w / 2.0, t + w / 2.0)
