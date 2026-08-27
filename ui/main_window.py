"""
MainWindow — главное окно FlowerGraph.

Компоновка:

  ┌─ Меню ──────────────────────────────────────────────────┐
  ├─ Тулбар ────────────────────────────────────────────────┤
  ├─ PlotArea ────────────────┬─ Правая панель ─────────────┤
  │  (графики)                │  ChannelPanel (каналы)      │
  │                           ├─────────────────────────────┤
  │                           │  BlockListPanel (блоки)     │
  ├─ NavBar ─ [|◀][◀][▶][▶|] ┴ [===overview===] ─ [─][s][+]┤
  └─ Строка состояния ───────────────────────────────────────┘
"""

import os
import time
import tempfile
from enum import Enum, auto
from pathlib import Path

import numpy as np
from PySide6.QtWidgets import (
    QMainWindow, QStatusBar, QMenuBar, QToolBar,
    QLabel, QMessageBox, QComboBox, QWidget,
    QHBoxLayout, QVBoxLayout, QSplitter, QPushButton,
    QListWidget, QListWidgetItem, QFileDialog, QInputDialog,
    QSystemTrayIcon, QMenu,
)
from PySide6.QtCore import QSize, QTimer, Qt
from PySide6.QtGui import QAction, QKeySequence

from core.config import config
from core.app_icon import app_icon
from core.session import Session, Block, ChannelInfo, Annotation
from core import file_io
from core import calib_file
from ui.export_dialog import ExportCsvDialog, export_csv
from ui.plot_area import (PlotArea, TIME_DIV_SEQ, N_DIV, fmt_time_div,
                          PLOT_STYLE_LINE, PLOT_STYLE_LINE_POINTS,
                          PLOT_STYLE_POINTS, PLOT_STYLE_BARS, PLOT_STYLE_LABELS)
from ui.channel_panel import ChannelPanel
from ui.nav_bar import NavBar
from ui.amplitude_scale import AmplitudeScalePanel
from ui.stats_panel import StatsPanel
from ui.calib_dialog import ChannelCalibDialog
from plugins.virtual_generator import VirtualGenerator, VirtualGeneratorDialog, GeneratorConfig
from plugins.com_ascii_source import ComAsciiSource, ComAsciiConfig
from plugins.com_cobs_source import ComCobsSource, ComCobsConfig, ComCobsDialog
from plugins.com_mcobs_source import ComMCobsSource, ComMCobsConfig, ComMCobsDialog
from plugins.base_source import BaseSource
from plugins import pg_export
from ui.com_ascii_dialog import ComAsciiDialog
from core.i18n import tr, set_lang, get_lang

APP_NAME    = 'FlowerGraph'
APP_VERSION = '0.7.0'
FILE_FILTER    = 'FlowerGraph Data (*.fgd);;Все файлы (*)'
PGC_FILTER     = 'PGC (*.pgc);;Все файлы (*)'


class AppState(Enum):
    IDLE       = auto()
    MONITORING = auto()
    RECORDING  = auto()


class SourceType(Enum):
    GENERATOR = 'generator'
    COM_ASCII = 'com_ascii'
    COM_COBS  = 'com_cobs'
    COM_MCOBS = 'com_mcobs'

_SOURCE_LABELS = {
    SourceType.GENERATOR: 'Генератор',
    SourceType.COM_ASCII: 'COM ASCII',
    SourceType.COM_COBS:  'COM COBS',
    SourceType.COM_MCOBS: 'COM mCOBS',
}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._state: AppState = AppState.IDLE
        self._source: BaseSource | None = None
        self._pkt_count  = 0

        # запись
        self._session           = Session()
        self._current_block_idx = -1
        self._rec_chunks_t:  list[np.ndarray] = []
        self._rec_chunks_v:  list[np.ndarray] = []
        self._rec_start_wall = 0.0
        self._rec_total_pts  = 0
        self._rec_n_channels = 0
        self._block_global_offsets: list[float] = []   # глобальные смещения блоков для NavBar

        # Потоковая запись во временный файл
        self._tmp_t_file  = None   # открытый бинарный файл для временных меток
        self._tmp_v_file  = None   # открытый бинарный файл для значений
        self._tmp_t_path  = ''
        self._tmp_v_path  = ''
        self._use_streaming = True  # всегда писать на диск (экономия RAM)
        self._pending_ann: list[tuple[float, str]] = []
        self._rec_last_t = 0.0

        # Источники данных
        self._source_type       = SourceType.GENERATOR
        self._gen_config        = GeneratorConfig()
        self._com_config        = ComAsciiConfig()
        self._cobs_config       = ComCobsConfig()
        self._mcobs_config      = ComMCobsConfig()
        self._current_sample_rate = 1000

        # виджеты
        self._plot_area      = PlotArea()
        self._channel_panel  = ChannelPanel()
        self._nav_bar        = NavBar()
        self._amp_scale      = AmplitudeScalePanel()
        self._stats_panel    = StatsPanel()

        # шкала амплитуды: активный канал и кнопки масштаба
        self._amp_scale.channel_activated.connect(self._on_channel_activated)
        self._amp_scale.sig_scale_up.connect(self._on_amp_scale_up)
        self._amp_scale.sig_scale_down.connect(self._on_amp_scale_down)

        # панель каналов ↔ PlotArea
        self._channel_panel.sig_visibility.connect(self._plot_area.set_channel_visible)
        self._channel_panel.sig_scale.connect(self._plot_area.set_channel_scale)
        self._channel_panel.sig_offset.connect(self._plot_area.set_channel_offset)
        self._channel_panel.sig_auto.connect(self._on_auto_scale)
        self._channel_panel.sig_calib_requested.connect(self._on_calib_requested)

        # PlotArea → NavBar (через MainWindow для учёта глобальных смещений блоков)
        self._plot_area.overview_ready.connect(self._on_plot_overview_ready)
        self._plot_area.view_range_changed.connect(self._on_view_range_changed)
        self._plot_area.time_div_changed.connect(
            lambda idx: self._nav_bar.update_scale_label(fmt_time_div(TIME_DIV_SEQ[idx]))
        )

        # NavBar → PlotArea (через MainWindow для навигации между блоками)
        self._nav_bar.navigate_to.connect(self._on_nav_navigate)
        self._nav_bar.go_start.connect(self._plot_area.go_to_start)
        self._nav_bar.go_end.connect(self._plot_area.go_to_end)
        self._nav_bar.page_left.connect(self._plot_area.page_left)
        self._nav_bar.page_right.connect(self._plot_area.page_right)
        self._nav_bar.zoom_in.connect(self._plot_area.zoom_in_discrete)
        self._nav_bar.zoom_out.connect(self._plot_area.zoom_out_discrete)
        self._nav_bar.start_stop.connect(self._on_start_stop)

        # PlotArea → MainWindow
        self._plot_area.following_changed.connect(self._on_following_changed)
        self._plot_area.markers_moved.connect(self._on_markers_moved)
        self._plot_area.cursor_moved.connect(self._on_cursor_moved)
        self._plot_area.selection_action_requested.connect(self._on_selection_action)
        self._plot_area.selection_changed.connect(self._on_selection_changed)

        saved_lang = config.get('language', default='ru')
        set_lang(saved_lang)

        self._restore_source_configs()

        self._setup_window()
        self._setup_tray()
        self._build_menu()
        self._build_toolbar()
        self._build_statusbar()
        self._build_central()
        self._restore_geometry()
        self._restore_source_type()

        # Инициализировать метку масштаба
        self._nav_bar.update_scale_label(
            fmt_time_div(TIME_DIV_SEQ[self._plot_area.time_div_idx])
        )

        # таймеры
        self._stat_timer = QTimer()
        self._stat_timer.timeout.connect(self._update_status)
        self._stat_timer.start(1000)

        self._autosave_timer = QTimer()
        self._autosave_timer.timeout.connect(self._autosave)

        self._rec_timer = QTimer()
        self._rec_timer.timeout.connect(self._update_rec_counter)

    # ==================================================================
    # Компоновка
    # ==================================================================

    def _setup_window(self):
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(1000, 680)
        icon = app_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)

    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = None
            return
        icon = app_icon()
        self._tray = QSystemTrayIcon(self)
        if not icon.isNull():
            self._tray.setIcon(icon)
        self._tray.setToolTip(APP_NAME)
        menu = QMenu(self)
        act_show = QAction('Показать', self)
        act_show.triggered.connect(self._show_from_tray)
        act_quit = QAction('Выход', self)
        act_quit.triggered.connect(self.close)
        menu.addAction(act_show)
        menu.addSeparator()
        menu.addAction(act_quit)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._show_from_tray()

    def _build_central(self):
        # Левая панель: шкала амплитуды
        left = QWidget()
        left.setMinimumWidth(100)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)
        lv.addWidget(self._amp_scale, stretch=1)

        # Правая панель: настройки каналов + список блоков
        right = QWidget()
        right.setMinimumWidth(200)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        rv.addWidget(self._channel_panel, stretch=1)

        block_header = QLabel('  Блоки данных')
        block_header.setStyleSheet(
            'background:#e8e8e8; font-weight:bold; padding:3px 4px;'
            'border-top:1px solid #ccc;'
        )
        rv.addWidget(block_header)
        self._block_list = QListWidget()
        self._block_list.setMaximumHeight(120)
        self._block_list.setMinimumHeight(50)
        self._block_list.itemClicked.connect(self._on_block_list_click)
        self._block_list.setStyleSheet('font-size:11px;')
        rv.addWidget(self._block_list)

        # Основная область: QSplitter [левая панель] + [график] + [правая панель]
        self._main_splitter = QSplitter(Qt.Horizontal)
        self._main_splitter.addWidget(left)
        self._main_splitter.addWidget(self._plot_area)
        self._main_splitter.addWidget(right)
        self._main_splitter.setSizes([150, 700, 378])

        # Статистика + навигация: вертикальный сплиттер
        self._stats_toggle_btn = QWidget()
        stats_row_layout = QHBoxLayout(self._stats_toggle_btn)
        stats_row_layout.setContentsMargins(2, 0, 2, 0)
        stats_row_layout.setSpacing(4)
        self._btn_stats_toggle = QPushButton('▼ Статистика')
        self._btn_stats_toggle.setCheckable(True)
        self._btn_stats_toggle.setChecked(True)
        self._btn_stats_toggle.setFixedHeight(18)
        self._btn_stats_toggle.setStyleSheet('font-size:9px; text-align:left; padding:0 4px;')
        self._btn_stats_toggle.toggled.connect(self._on_stats_toggle)
        stats_row_layout.addWidget(self._btn_stats_toggle)
        stats_row_layout.addStretch()

        self._center_splitter = QSplitter(Qt.Vertical)
        self._center_splitter.addWidget(self._main_splitter)
        self._center_splitter.addWidget(self._stats_panel)
        self._center_splitter.setSizes([550, 120])

        # Центральный виджет: сплиттер + строка кнопки + NavBar внизу
        central = QWidget()
        cv = QVBoxLayout(central)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        cv.addWidget(self._center_splitter, stretch=1)
        cv.addWidget(self._stats_toggle_btn)
        cv.addWidget(self._nav_bar)
        self.setCentralWidget(central)

    def _restore_source_configs(self):
        """Загрузить последние настройки источников из конфига."""
        d = config.get('source_ascii', default=None)
        if isinstance(d, dict):
            self._com_config = ComAsciiConfig.from_dict(d)

        d = config.get('source_cobs', default=None)
        if isinstance(d, dict):
            self._cobs_config = ComCobsConfig.from_dict(d)

        d = config.get('source_mcobs', default=None)
        if isinstance(d, dict):
            self._mcobs_config = ComMCobsConfig.from_dict(d)

        d = config.get('source_generator', default=None)
        if isinstance(d, dict):
            self._gen_config = GeneratorConfig.from_dict(d)

    def _restore_source_type(self):
        """Восстановить последний выбранный тип источника в combo."""
        saved = config.get('source_type', default=SourceType.GENERATOR.value)
        for i in range(self._cb_source.count()):
            if self._cb_source.itemData(i).value == saved:
                self._cb_source.setCurrentIndex(i)
                break

    def _restore_geometry(self):
        w = config.get('window')
        self.resize(w['width'], w['height'])
        self.move(w['x'], w['y'])
        if w.get('maximized'):
            self.showMaximized()
        splitter_sizes = config.get('splitter_sizes', default=None)
        if splitter_sizes and len(splitter_sizes) == 3:
            self._main_splitter.setSizes(splitter_sizes)
        center_sizes = config.get('center_splitter_sizes', default=None)
        if center_sizes and len(center_sizes) == 2:
            self._center_splitter.setSizes(center_sizes)

    # ==================================================================
    # Меню
    # ==================================================================

    def _build_menu(self):
        mb: QMenuBar = self.menuBar()

        # Файл
        m_file = mb.addMenu(tr('menu_file'))
        m_file.addAction(self._action(tr('new'),      self._on_new_file, QKeySequence.New))
        m_file.addSeparator()
        m_file.addAction(self._action(tr('open'),     self._on_open,    QKeySequence.Open))
        m_file.addAction(self._action(tr('save'),     self._on_save,    QKeySequence.Save))
        m_file.addAction(self._action(tr('save_as'),  self._on_save_as, QKeySequence.SaveAs))
        m_file.addSeparator()
        m_import = m_file.addMenu(tr('import'))
        m_import.addAction(self._action(
            tr('import_pgc'), self._on_import_pgc))
        m_file.addSeparator()
        m_export = m_file.addMenu(tr('export'))
        m_export.addAction(self._action(
            tr('export_csv'), self._on_export_csv,
            QKeySequence('Ctrl+E')))
        m_export.addAction(self._action(
            tr('export_png'), self._on_export_png,
            QKeySequence('Ctrl+Shift+E')))
        m_export.addAction(self._action(
            tr('export_pgc'), self._on_export_pgc))
        m_file.addSeparator()
        self._menu_recent = m_file.addMenu(tr('recent'))
        self._update_recent_menu()
        m_file.addSeparator()
        m_file.addAction(self._action(tr('quit'), self.close, QKeySequence.Quit))

        # Правка
        m_edit = mb.addMenu(tr('menu_edit'))
        m_edit.addAction(self._action('Отменить',  self._stub, QKeySequence.Undo))
        m_edit.addAction(self._action('Повторить', self._stub, QKeySequence.Redo))

        # Вид
        m_view = mb.addMenu(tr('menu_view'))
        m_view.addAction(self._action('Авто-масштаб Y', self._on_autorange_y, QKeySequence('F5')))
        m_view.addSeparator()

        # Выравнивание каналов по Y
        m_align = m_view.addMenu('Выравнивание Y')
        m_align.addAction(self._action(
            'Совместить базовые уровни', self._on_y_overlay,
            QKeySequence('Alt+0')))
        m_align.addAction(self._action(
            'Разнести по вертикали',     self._on_y_distribute,
            QKeySequence('Alt+D')))
        m_align.addAction(self._action(
            'Авто каждый независимо',    self._on_y_auto,
            QKeySequence('Alt+A')))
        m_align.addAction(self._action(
            'Совместить сетки',          self._on_y_grids,
            QKeySequence('Alt+G')))
        m_align.addSeparator()
        m_align.addAction(self._action(
            'Сбросить масштаб Y',        self._on_y_reset,
            QKeySequence('Alt+R')))
        m_align.addSeparator()
        m_align.addAction(self._action(
            'Приблизить Y активного (×1.5)',  self._on_y_zoom_in,
            QKeySequence('Ctrl+=')))
        m_align.addAction(self._action(
            'Отдалить Y активного  (×0.67)',  self._on_y_zoom_out,
            QKeySequence('Ctrl+-')))
        m_align.addAction(self._action(
            'Приблизить Y всех  (×1.5)',      self._on_y_zoom_in_all,
            QKeySequence('Alt+=')))
        m_align.addAction(self._action(
            'Отдалить Y всех   (×0.67)',      self._on_y_zoom_out_all,
            QKeySequence('Alt+-')))

        m_view.addSeparator()
        m_view.addAction(self._action('Панель каналов',
                                      self._toggle_channel_panel))
        m_view.addSeparator()

        # Масштаб шкалы X — подменю со стандартными шагами (как в PG)
        m_zoom = m_view.addMenu('Масштаб шкалы X')
        m_zoom.addAction(self._action('Уменьшить (приблизить)',
                                      self._plot_area.zoom_in_discrete,
                                      QKeySequence('Shift+=')))
        m_zoom.addAction(self._action('Увеличить (отдалить)',
                                      self._plot_area.zoom_out_discrete,
                                      QKeySequence('Shift+-')))
        m_zoom.addSeparator()
        for i, t in enumerate(TIME_DIV_SEQ):
            label = fmt_time_div(t)
            idx   = i  # capture
            m_zoom.addAction(self._action(label, lambda checked=False, ii=idx: self._plot_area.set_time_div_idx(ii)))

        # Навигация
        m_nav = m_view.addMenu('Навигация')
        m_nav.addAction(self._action('В начало',          self._plot_area.go_to_start,  QKeySequence('Home')))
        m_nav.addAction(self._action('Предыдущая стр.',   self._plot_area.page_left,    QKeySequence('PgUp')))
        m_nav.addAction(self._action('Следующая стр.',    self._plot_area.page_right,   QKeySequence('PgDown')))
        m_nav.addAction(self._action('В конец / следить', self._plot_area.go_to_end,    QKeySequence('End')))

        # Запись
        m_rec = mb.addMenu(tr('menu_record'))
        m_rec.addAction(self._action('Настройка источника…', self._on_source_config))
        m_rec.addSeparator()
        self._act_start = self._action(tr('start'), self._on_start, QKeySequence('Space'))
        self._act_stop  = self._action(tr('stop'),  self._on_stop,  QKeySequence('Escape'))
        self._act_stop.setEnabled(False)
        m_rec.addAction(self._act_start)
        m_rec.addAction(self._act_stop)
        m_rec.addSeparator()
        m_rec.addAction(self._action('Добавить метку', self._on_add_annotation, QKeySequence('M')))

        # Блок
        m_blk = mb.addMenu(tr('menu_block'))
        m_blk.addAction(self._action('Найти блок',  self._on_find_block,   QKeySequence('Shift+B')))
        m_blk.addAction(self._action('Показать блок (масштаб)', self._on_show_block))
        m_blk.addSeparator()
        m_blk.addAction(self._action('← Предыдущий',  self._on_prev_block, QKeySequence('Shift+PgUp')))
        m_blk.addAction(self._action('→ Следующий',   self._on_next_block, QKeySequence('Shift+PgDown')))

        # Обработка
        m_proc = mb.addMenu('Обработка')
        m_proc.addAction(self._action('Применить функцию…', self._stub))

        # Анализ
        m_anal = mb.addMenu('Анализ')
        m_anal.addAction(self._action('Спектроанализатор', self._stub))
        m_anal.addAction(self._action('Гистограмма',       self._stub))
        m_anal.addAction(self._action('Таблица значений',  self._stub))

        # Инструменты
        m_tools = mb.addMenu(tr('menu_tools'))
        m_tools.addAction(self._action('Цифровой вольтметр', self._stub))
        m_tools.addSeparator()
        m_calib = m_tools.addMenu('Калибровка каналов')
        m_calib.addAction(self._action(
            'Загрузить калибровку из .cal…', self._on_load_calib_file))
        m_calib.addAction(self._action(
            'Сохранить калибровку в .cal…', self._on_save_calib_file))
        m_tools.addSeparator()
        m_lang = m_tools.addMenu(tr('language') + ' / Language')
        m_lang.addAction(self._action(tr('lang_ru'), lambda: self._set_language('ru')))
        m_lang.addAction(self._action(tr('lang_en'), lambda: self._set_language('en')))
        m_tools.addSeparator()
        m_tools.addAction(self._action(tr('settings') + '…', self._stub))

        # Справка
        m_help = mb.addMenu(tr('menu_help'))
        m_help.addAction(self._action(tr('about'), self._on_about))

    # ==================================================================
    # Тулбар — только основные кнопки, навигация вынесена в NavBar
    # ==================================================================

    def _build_toolbar(self):
        tb = QToolBar('Основная панель')
        tb.setIconSize(QSize(20, 20))
        tb.setMovable(False)
        self.addToolBar(tb)

        # Файл
        tb.addAction(self._action('Новый',     self._on_new_file, QKeySequence.New))
        tb.addAction(self._action('Открыть',   self._on_open,     QKeySequence.Open))
        tb.addAction(self._action('Сохранить', self._on_save,     QKeySequence.Save))
        tb.addSeparator()

        # Запись (только Стоп; Старт — большая кнопка в NavBar)
        self._tb_stop = self._action('■ Стоп', self._on_stop)
        self._tb_stop.setEnabled(False)
        tb.addAction(self._tb_stop)
        tb.addAction(self._action('◆ Метка', self._on_add_annotation))
        tb.addSeparator()

        # Следить
        self._act_follow = QAction('⟳ Следить', self)
        self._act_follow.setCheckable(True)
        self._act_follow.setChecked(True)
        self._act_follow.setToolTip('Авто-прокрутка к последним данным')
        self._act_follow.triggered.connect(
            lambda c: self._plot_area.set_following(c)
        )
        tb.addAction(self._act_follow)

        # Авто Y
        self._act_auto_y = QAction('↕ Авто Y', self)
        self._act_auto_y.setCheckable(True)
        self._act_auto_y.setChecked(True)
        self._act_auto_y.setToolTip('Автомасштабирование по оси Y\n(снять — зафиксировать текущий диапазон)')
        self._act_auto_y.triggered.connect(
            lambda c: self._plot_area.set_y_autorange(c)
        )
        tb.addAction(self._act_auto_y)

        # Маркеры
        self._act_markers = QAction('M1|M2', self)
        self._act_markers.setCheckable(True)
        self._act_markers.setToolTip('Скользящие маркеры ΔT / ΔY')
        self._act_markers.triggered.connect(
            lambda c: self._plot_area.show_markers(c)
        )
        tb.addAction(self._act_markers)
        tb.addSeparator()

        # FPS
        tb.addWidget(QLabel(' FPS: '))
        self._cb_fps = QComboBox()
        for fps in [10, 20, 25, 30, 60]:
            self._cb_fps.addItem(str(fps), fps)
        self._cb_fps.setCurrentIndex(2)
        self._cb_fps.setFixedWidth(55)
        self._cb_fps.currentIndexChanged.connect(
            lambda: self._plot_area.set_fps(self._cb_fps.currentData())
        )
        tb.addWidget(self._cb_fps)
        tb.addSeparator()

        # Выбор источника сигнала
        tb.addWidget(QLabel(' Источник: '))
        self._cb_source = QComboBox()
        for st in SourceType:
            self._cb_source.addItem(_SOURCE_LABELS[st], st)
        self._cb_source.setFixedWidth(110)
        self._cb_source.currentIndexChanged.connect(self._on_source_type_changed)
        tb.addWidget(self._cb_source)
        self._btn_src_cfg = self._action('⚙ Настройка…', self._on_source_config)
        self._btn_src_cfg.setToolTip('Настройка выбранного источника')
        tb.addAction(self._btn_src_cfg)
        tb.addSeparator()

        # Y-ось: выравнивание каналов
        tb.addWidget(QLabel(' Y: '))
        act_ov   = self._action('≡ Нули',    self._on_y_overlay)
        act_ov.setToolTip('Совместить базовые уровни (все нули в одной точке)\nAlt+0')
        act_dist = self._action('⊥ Разнести', self._on_y_distribute)
        act_dist.setToolTip('Разнести каналы по вертикали\nAlt+D')
        act_auto = self._action('↕ Авто',    self._on_y_auto)
        act_auto.setToolTip('Авто-масштаб каждого канала независимо\nAlt+A')
        act_rst  = self._action('⟳ Сброс Y', self._on_y_reset)
        act_rst.setToolTip('Сбросить масштаб и смещение Y (scale=1, offset=0)\nAlt+R')
        for a in (act_ov, act_dist, act_auto, act_rst):
            tb.addAction(a)
        tb.addSeparator()

        # Y zoom: приблизить / отдалить
        act_yzin  = self._action('↑Y', self._on_y_zoom_in)
        act_yzin.setToolTip('Приблизить Y активного канала (×1.5)  Ctrl+=')
        act_yzout = self._action('↓Y', self._on_y_zoom_out)
        act_yzout.setToolTip('Отдалить Y активного канала (×0.67)  Ctrl+-')
        tb.addAction(act_yzin)
        tb.addAction(act_yzout)
        tb.addSeparator()

        # Стиль графика
        tb.addWidget(QLabel(' График: '))
        self._cb_plot_style = QComboBox()
        for key in (PLOT_STYLE_LINE, PLOT_STYLE_LINE_POINTS,
                    PLOT_STYLE_POINTS, PLOT_STYLE_BARS):
            self._cb_plot_style.addItem(PLOT_STYLE_LABELS[key], key)
        self._cb_plot_style.setFixedWidth(120)
        self._cb_plot_style.currentIndexChanged.connect(
            lambda: self._plot_area.set_plot_style(
                self._cb_plot_style.currentData()
            )
        )
        tb.addWidget(self._cb_plot_style)

    # ==================================================================
    # Строка состояния
    # ==================================================================

    def _build_statusbar(self):
        sb: QStatusBar = self.statusBar()
        self._lbl_source  = QLabel('Источник: нет')
        self._lbl_rate    = QLabel('0 пак/с')
        self._lbl_rec_cnt = QLabel('')
        self._lbl_markers = QLabel('')
        self._lbl_coords  = QLabel('')
        self._lbl_sel     = QLabel('')
        self._lbl_sel.setStyleSheet('color:#886600;')
        self._lbl_errors  = QLabel('Ошибок: 0')
        self._lbl_mode    = QLabel('')
        for lbl in (self._lbl_source, self._lbl_rate, self._lbl_rec_cnt,
                    self._lbl_markers, self._lbl_coords,
                    self._lbl_sel, self._lbl_errors):
            sb.addWidget(lbl)
            sb.addWidget(_sep())
        sb.addPermanentWidget(self._lbl_mode)

    # ==================================================================
    # Управление источником
    # ==================================================================

    def _on_start(self):
        if self._state != AppState.IDLE:
            return
        if not self._start_source():
            QMessageBox.warning(
                self, 'Источник',
                'Не удалось запустить источник. Проверьте COM-порт.',
            )
            return
        self._rec_chunks_t.clear()
        self._rec_chunks_v.clear()
        self._pending_ann.clear()
        self._rec_last_t = 0.0
        self._rec_start_wall = time.time()
        self._rec_total_pts  = 0
        self._rec_n_channels = 0
        self._open_tmp_files()
        interval_min = config.get('autosave_interval_min', default=5)
        self._autosave_timer.start(int(interval_min * 60 * 1000))
        self._rec_timer.start(1000)
        self._set_state(AppState.RECORDING)

    def _on_record(self):
        """Устарело — оставлено для совместимости."""
        self._on_start()

    def _on_monitor(self):
        """Устарело — запускает запись."""
        self._on_start()

    def _on_start_stop(self):
        if self._state == AppState.IDLE:
            self._on_start()
        else:
            self._on_stop()

    def _open_tmp_files(self):
        self._close_tmp_files(remove=True)
        fd_t = fd_v = None
        try:
            fd_t, self._tmp_t_path = tempfile.mkstemp(prefix='fg_rec_t_', suffix='.bin')
            fd_v, self._tmp_v_path = tempfile.mkstemp(prefix='fg_rec_v_', suffix='.bin')
            self._tmp_t_file = open(fd_t, 'wb')
            self._tmp_v_file = open(fd_v, 'wb')
        except Exception:
            if fd_t is not None:
                try:
                    os.close(fd_t)
                except Exception:
                    pass
            if fd_v is not None:
                try:
                    os.close(fd_v)
                except Exception:
                    pass
            self._close_tmp_files(remove=True)
            self._tmp_t_file = None
            self._tmp_v_file = None
            QMessageBox.warning(
                self, 'Запись',
                'Не удалось открыть временные файлы — запись пойдёт в RAM.',
            )

    def _close_tmp_files(self, remove: bool = False):
        for f in (self._tmp_t_file, self._tmp_v_file):
            try:
                if f:
                    f.close()
            except Exception:
                pass
        self._tmp_t_file = None
        self._tmp_v_file = None
        if remove:
            for p in (self._tmp_t_path, self._tmp_v_path):
                if p:
                    try:
                        os.remove(p)
                    except Exception:
                        pass
            self._tmp_t_path = ''
            self._tmp_v_path = ''

    def _on_stop(self):
        self._autosave_timer.stop()
        self._rec_timer.stop()
        was_recording = self._state == AppState.RECORDING

        if self._source:
            self._source.stop()
        self._plot_area.stop()

        has_data = (self._rec_total_pts > 0)
        if was_recording and has_data:
            self._finalize_block()
        elif was_recording:
            self._close_tmp_files(remove=True)

        self._source = None
        self._set_state(AppState.IDLE)
        self._lbl_rec_cnt.setText('')

    def _start_source(self) -> bool:
        st = self._source_type
        if st == SourceType.COM_ASCII:
            src = ComAsciiSource(self._com_config)
            cfg_n_ch = self._com_config.n_channels
        elif st == SourceType.COM_COBS:
            src = ComCobsSource(self._cobs_config)
            cfg_n_ch = self._cobs_config.n_channels
        elif st == SourceType.COM_MCOBS:
            src = ComMCobsSource(self._mcobs_config)
            cfg_n_ch = self._mcobs_config.n_channels
        else:
            src = VirtualGenerator(self._gen_config)
            cfg_n_ch = -1

        src.set_data_callback(self._on_data)
        src.set_error_callback(self._on_source_error)

        ok = src.start()
        src._drain_errors()
        if not ok or not src.is_running:
            return False

        is_com = st in (SourceType.COM_ASCII, SourceType.COM_COBS, SourceType.COM_MCOBS)
        self._source = src
        if is_com and cfg_n_ch == 0:
            self._source_ready = False
            self._lbl_source.setText(f'Источник: {src.get_name()} — ожидание…')
        else:
            if hasattr(src, 'effective_sample_rate'):
                sr = src.effective_sample_rate()
            else:
                sr = self._gen_config.sample_rate
            self._setup_source_ui(src, sr)
            self._source_ready = True
            self._lbl_source.setText(f'Источник: {src.get_name()}')
        return True

    def _set_state(self, state: AppState):
        self._state = state
        idle      = state == AppState.IDLE
        active    = not idle
        recording = state == AppState.RECORDING

        self._act_start.setEnabled(idle)
        self._act_stop.setEnabled(active)
        self._tb_stop.setEnabled(active)
        self._nav_bar.set_recording(active)
        if hasattr(self, '_cb_source'):
            self._cb_source.setEnabled(idle)

        if idle:
            self._lbl_mode.setText('')
            self._lbl_mode.setStyleSheet('')
            if self._session.is_empty:
                self._lbl_source.setText('Источник: нет')
        elif recording:
            self._lbl_mode.setText('⏺ ЗАПИСЬ')
            self._lbl_mode.setStyleSheet('color:#c00000;font-weight:bold;')

    def _setup_source_ui(self, src: BaseSource, sample_rate: int):
        """Инициализировать PlotArea и панели под конкретный источник."""
        self._current_sample_rate = sample_rate
        names  = src.get_channel_names()
        n      = src.get_channel_count()
        self._plot_area.setup(n_channels=n, names=names, sample_rate=sample_rate)
        colors = self._plot_area.get_channel_colors()
        self._channel_panel.setup(names, colors)
        self._nav_bar.setup_channels(n, colors)
        self._amp_scale.setup(names, colors)
        self._stats_panel.setup(names, colors)
        self._block_global_offsets = []   # сбросить — live-режим не использует смещения

    def _on_data(self, times: np.ndarray, values: np.ndarray):
        # Авто-инициализация UI при первом пакете от COM-источника
        is_com = self._source_type in (SourceType.COM_ASCII, SourceType.COM_COBS, SourceType.COM_MCOBS)
        if is_com and not getattr(self, '_source_ready', True):
            self._source_ready = True
            n_ch = values.shape[1]
            self._com_config.n_channels = n_ch
            # Частота из бод-рейта (уже откалибрована в ComAsciiSource)
            sr = self._source.effective_sample_rate()
            self._setup_source_ui(self._source, sr)
            self._lbl_source.setText(f'Источник: {self._source.get_name()}')

        self._plot_area.push_data(times, values)
        self._pkt_count += 1
        if self._state == AppState.RECORDING:
            if not self._rec_n_channels:
                self._rec_n_channels = values.shape[1]
            if self._tmp_t_file is not None and self._tmp_v_file is not None:
                times.astype(np.float64).tofile(self._tmp_t_file)
                values.astype(np.float32).tofile(self._tmp_v_file)
            else:
                self._rec_chunks_t.append(times.copy())
                self._rec_chunks_v.append(values.copy())
            self._rec_total_pts += len(times)
            self._rec_last_t = float(times[-1])

    def _on_source_error(self, msg: str):
        # Сообщения авто-реконнекта — в строку состояния, не как «ошибку»
        if 'Реконнект' in msg or 'Попытка' in msg:
            self._lbl_source.setText(f'COM: {msg}')
        else:
            self._lbl_errors.setText(f'Ошибка: {msg}')

    # ==================================================================
    # Финализация блока
    # ==================================================================

    def _finalize_block(self):
        # Получить данные: из временных файлов или из RAM-буферов
        if (self._tmp_t_file is not None
                and self._tmp_v_file is not None
                and self._rec_n_channels > 0):
            self._close_tmp_files()
            try:
                times_all  = np.fromfile(self._tmp_t_path, dtype=np.float64)
                values_all = np.fromfile(
                    self._tmp_v_path, dtype=np.float32
                ).reshape(-1, self._rec_n_channels)
            except Exception as e:
                QMessageBox.critical(self, 'Ошибка финализации',
                                     f'Не удалось прочитать временный файл:\n{e}')
                return
            finally:
                for p in (self._tmp_t_path, self._tmp_v_path):
                    try: os.remove(p)
                    except Exception: pass
        else:
            self._close_tmp_files()
            if not self._rec_chunks_t:
                return
            times_all  = np.concatenate(self._rec_chunks_t)
            values_all = np.concatenate(self._rec_chunks_v)
        self._rec_chunks_t.clear()
        self._rec_chunks_v.clear()

        n_ch  = values_all.shape[1]
        names = (self._source.get_channel_names()
                 if self._source else [f'CH{i+1}' for i in range(n_ch)])
        ch_info = []
        for i, n in enumerate(names[:n_ch]):
            scale, offset = (1.0, 0.0)
            try:
                scale, offset = self._plot_area.get_channel_calib(i)
            except Exception:
                pass
            ch_info.append(ChannelInfo(name=n, scale=scale, offset=offset))

        anns = [Annotation(t=t, text=text) for t, text in self._pending_ann]
        self._pending_ann.clear()

        block = Block(
            start_time  = self._rec_start_wall,
            source_name = (self._source.get_name() if self._source else ''),
            sample_rate = self._current_sample_rate,
            channels    = ch_info,
            times       = times_all,
            values      = values_all,
            annotations = anns,
        )
        self._session.add_block(block)
        self._current_block_idx = self._session.n_blocks - 1
        self._display_block(self._current_block_idx)
        self._refresh_block_list()
        self._update_title()
        # Авто-зум: показать весь записанный блок целиком
        self._on_show_block()


    def _autosave(self):
        if self._state == AppState.RECORDING:
            for f in (self._tmp_t_file, self._tmp_v_file):
                try:
                    if f:
                        f.flush()
                except Exception:
                    pass
            return
        if self._session.file_path:
            try:
                file_io.save(self._session, self._session.file_path)
            except Exception:
                pass

    # ==================================================================
    # Файловые операции
    # ==================================================================

    def _on_open(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Открыть файл', '', FILE_FILTER)
        if not path:
            return
        try:
            self._session = file_io.load(path)
        except Exception as e:
            QMessageBox.critical(self, 'Ошибка открытия', str(e))
            return
        config.add_recent_file(path)
        self._update_recent_menu()
        self._update_title()
        self._refresh_block_list()
        if self._session.n_blocks > 0:
            self._current_block_idx = 0
            self._display_block(0)   # внутри вызывает _update_session_overview()

    def _on_import_pgc(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Импорт .pgc', '', PGC_FILTER)
        if not path:
            return
        try:
            from plugins import pg_import
            block = pg_import.load(path)
        except Exception as e:
            QMessageBox.critical(self, 'Ошибка импорта', str(e))
            return
        self._session.add_block(block)
        self._update_title()
        self._refresh_block_list()
        idx = self._session.n_blocks - 1
        self._current_block_idx = idx
        self._display_block(idx)

    def _on_save(self) -> bool:
        if self._session.file_path:
            return self._do_save(self._session.file_path)
        return self._on_save_as()

    def _on_save_as(self) -> bool:
        path, _ = QFileDialog.getSaveFileName(self, 'Сохранить файл', '', FILE_FILTER)
        if not path:
            return False
        return self._do_save(path)

    def _do_save(self, path: str) -> bool:
        try:
            file_io.save(self._session, path)
            config.add_recent_file(self._session.file_path)
            self._update_recent_menu()
            self._update_title()
            return True
        except Exception as e:
            QMessageBox.critical(self, 'Ошибка сохранения', str(e))
            return False

    # ==================================================================
    # Навигация по блокам
    # ==================================================================

    def _display_block(self, idx: int):
        if not (0 <= idx < self._session.n_blocks):
            return
        block = self._session.blocks[idx]
        self._current_block_idx = idx
        names  = [ch.name for ch in block.channels]
        units  = [ch.unit for ch in block.channels]
        self._plot_area.load_block(block)
        colors = self._plot_area.get_channel_colors()
        self._channel_panel.setup(names, colors)
        self._amp_scale.setup(names, colors)
        self._stats_panel.setup(names, colors)
        # Показать единицы измерения
        for i, u in enumerate(units):
            self._channel_panel.update_unit(i, u)
        self._refresh_block_list()
        self._update_session_overview()

    # ==================================================================
    # Сессионный обзор: все блоки в NavBar
    # ==================================================================

    def _update_session_overview(self):
        """Собрать сводный обзор всех блоков сессии и отобразить в NavBar."""
        n = self._session.n_blocks
        if n == 0:
            return

        MAX_PTS = 1500
        pts_per_block = max(50, MAX_PTS // n)

        # Глобальные смещения: блоки укладываются встык с зазором 0.5 с
        offsets = [0.0]
        for i in range(1, n):
            offsets.append(offsets[-1] + self._session.blocks[i - 1].duration + 0.5)
        self._block_global_offsets = offsets

        n_ch_max = max(b.n_channels for b in self._session.blocks)
        colors   = self._plot_area.get_channel_colors()
        while len(colors) < n_ch_max:
            colors.append('#888888')

        # Пересоздать кривые NavBar с нужным числом каналов
        self._nav_bar.setup_channels(n_ch_max, colors)

        all_times  = []
        all_values = []
        for i, block in enumerate(self._session.blocks):
            t = block.times.astype(np.float64)
            v = block.values.astype(np.float32)
            step  = max(1, len(t) // pts_per_block)
            t_dec = t[::step]
            v_dec = v[::step]
            # Переводим локальное время в глобальное
            t_global = t_dec - float(block.t_start) + offsets[i]
            all_times.append(t_global)
            # Дополняем NaN если число каналов меньше максимума
            if v_dec.shape[1] < n_ch_max:
                pad   = np.full((len(v_dec), n_ch_max - v_dec.shape[1]),
                                np.nan, dtype=np.float32)
                v_dec = np.hstack([v_dec, pad])
            all_values.append(v_dec)

        combined_t = np.concatenate(all_times)
        combined_v = np.concatenate(all_values)
        self._nav_bar.update_overview(combined_t, combined_v)
        self._nav_bar.mark_blocks(offsets)

        # Синхронизировать регион с текущим видом
        try:
            vr = self._plot_area._plot.plotItem.getViewBox().viewRange()[0]
            self._on_view_range_changed(float(vr[0]), float(vr[1]))
        except Exception:
            pass

    def _current_block_offset(self) -> float:
        if (self._block_global_offsets
                and 0 <= self._current_block_idx < len(self._block_global_offsets)):
            return self._block_global_offsets[self._current_block_idx]
        return 0.0

    def _on_plot_overview_ready(self, times: np.ndarray, values: np.ndarray):
        """Live-обзор из кольцевого буфера PlotArea — только в активном режиме."""
        if self._state in (AppState.MONITORING, AppState.RECORDING):
            self._nav_bar.update_overview(times, values)

    def _on_view_range_changed(self, t0: float, t1: float):
        """Трансляция диапазона вида PlotArea в координаты NavBar."""
        if self._state in (AppState.MONITORING, AppState.RECORDING):
            self._nav_bar.set_view_region(t0, t1)
        else:
            # Статический режим: пересчитать в глобальные координаты
            if not self._block_global_offsets:
                self._nav_bar.set_view_region(t0, t1)
                return
            idx    = self._current_block_idx
            offset = self._current_block_offset()
            t_start_local = float(self._session.blocks[idx].t_start) if (
                0 <= idx < self._session.n_blocks) else 0.0
            self._nav_bar.set_view_region(
                t0 - t_start_local + offset,
                t1 - t_start_local + offset,
            )

    def _on_nav_navigate(self, global_t0: float, global_t1: float):
        """Навигация из NavBar (глобальные координаты) → PlotArea (локальные)."""
        if self._state in (AppState.MONITORING, AppState.RECORDING):
            self._plot_area.set_view_range(global_t0, global_t1)
            return
        if not self._block_global_offsets:
            self._plot_area.set_view_range(global_t0, global_t1)
            return

        # Определить целевой блок по глобальной позиции
        blocks  = self._session.blocks
        offsets = self._block_global_offsets
        target  = len(offsets) - 1
        for i in range(len(offsets) - 1):
            if global_t0 < offsets[i + 1]:
                target = i
                break

        # Переключить блок если нужно
        if target != self._current_block_idx:
            self._display_block(target)

        offset        = offsets[target]
        t_start_local = float(blocks[target].t_start)
        local_t0      = global_t0 - offset + t_start_local
        local_t1      = global_t1 - offset + t_start_local
        self._plot_area.set_view_range(local_t0, local_t1)

    def _on_find_block(self):
        """Перейти к началу текущего блока без смены масштаба."""
        if not (0 <= self._current_block_idx < self._session.n_blocks):
            return
        b  = self._session.blocks[self._current_block_idx]
        w  = TIME_DIV_SEQ[self._plot_area.time_div_idx] * N_DIV
        self._plot_area.set_view_range(b.t_start, b.t_start + w)

    def _on_show_block(self):
        """Показать весь активный блок, подобрав масштаб."""
        if not (0 <= self._current_block_idx < self._session.n_blocks):
            return
        b  = self._session.blocks[self._current_block_idx]
        # Выбрать масштаб чтобы блок влез
        needed = b.duration / N_DIV
        idx = 0
        for i, t in enumerate(TIME_DIV_SEQ):
            if t >= needed:
                idx = i
                break
        self._plot_area.set_time_div_idx(idx)
        self._plot_area.set_view_range(b.t_start, b.t_end)

    def _on_prev_block(self):
        if self._current_block_idx > 0:
            self._display_block(self._current_block_idx - 1)

    def _on_next_block(self):
        if self._current_block_idx < self._session.n_blocks - 1:
            self._display_block(self._current_block_idx + 1)

    def _on_block_list_click(self, item: QListWidgetItem):
        idx = item.data(Qt.UserRole)
        if idx is not None and idx != self._current_block_idx:
            self._display_block(idx)

    def _refresh_block_list(self):
        self._block_list.blockSignals(True)
        self._block_list.clear()
        for b in self._session.blocks:
            dt    = b.start_datetime().strftime('%H:%M:%S')
            label = (f'Блок {b.index + 1}  [{dt}]  '
                     f'{b.duration:.2f}с  {b.n_samples:,}пт')
            item  = QListWidgetItem(label)
            item.setData(Qt.UserRole, b.index)
            self._block_list.addItem(item)
        if 0 <= self._current_block_idx < self._block_list.count():
            self._block_list.setCurrentRow(self._current_block_idx)
        self._block_list.blockSignals(False)

    # ==================================================================
    # Аннотации
    # ==================================================================

    def _on_add_annotation(self):
        if self._state != AppState.RECORDING or self._rec_total_pts <= 0:
            return
        text, ok = QInputDialog.getText(self, 'Метка', 'Текст метки:')
        if ok and text:
            self._pending_ann.append((self._rec_last_t, text))

    # ==================================================================
    # Маркеры
    # ==================================================================

    def _on_markers_moved(self, t1: float, t2: float):
        y1  = self._plot_area.get_values_at(t1)
        y2  = self._plot_area.get_values_at(t2)
        dt  = abs(t2 - t1)
        if y1 is not None and y2 is not None and len(y1):
            dy = y1[0] - y2[0]
            self._lbl_markers.setText(
                f'M1={t1:.4f}s  M2={t2:.4f}s  ΔT={dt:.4f}s  ΔY={dy:+.4f}'
            )
        else:
            self._lbl_markers.setText(f'M1={t1:.4f}s  M2={t2:.4f}s  ΔT={dt:.4f}s')

    # ==================================================================
    # Шкала амплитуды: курсор и активный канал
    # ==================================================================

    def _on_cursor_moved(self, x: float, y_vals):
        import math
        if math.isnan(x):
            self._amp_scale.clear_values()
            self._lbl_coords.setText('')
        else:
            self._amp_scale.update_values(y_vals)
            self._lbl_coords.setText(f'X = {x:.5g} с')

    def _on_channel_activated(self, idx: int):
        self._plot_area.set_active_channel(idx)
        self._amp_scale.set_active(idx)
        block = self._current_block()
        if block and idx < len(block.channels):
            ch = block.channels[idx]
            self._plot_area.update_active_channel_yaxis(ch.name, ch.unit)

    def _on_amp_scale_up(self, idx: int):
        self._plot_area.set_active_channel(idx)
        r = self._plot_area.y_zoom_active(1.5)
        if r:
            self._channel_panel.update_scale_offset(idx, r[0], r[1])

    def _on_amp_scale_down(self, idx: int):
        self._plot_area.set_active_channel(idx)
        r = self._plot_area.y_zoom_active(1.0 / 1.5)
        if r:
            self._channel_panel.update_scale_offset(idx, r[0], r[1])

    # ==================================================================
    # Панель каналов — авто-масштаб
    # ==================================================================

    def _on_auto_scale(self, idx: int):
        result = self._plot_area.auto_scale_channel(idx)
        if result:
            self._channel_panel.update_scale_offset(idx, result[0], result[1])

    # ==================================================================
    # Калибровка каналов
    # ==================================================================

    def _on_calib_requested(self, idx: int):
        """Открыть диалог калибровки для канала idx."""
        block = self._current_block()
        if block is not None and idx < len(block.channels):
            ch = block.channels[idx]
            name, unit = ch.name, ch.unit
            coeff, offset = ch.scale, ch.offset
        else:
            # Живой режим — берём из PlotArea
            names = self._plot_area.get_channel_colors()  # цвета — только для count
            n = self._plot_area._n_channels
            if idx >= n:
                return
            name   = f'CH{idx+1}'
            unit   = ''
            coeff, offset = self._plot_area.get_channel_calib(idx)

        dlg = ChannelCalibDialog(idx, name, unit, coeff, offset, parent=self)
        if dlg.exec() != ChannelCalibDialog.Accepted:
            return

        new_name   = dlg.get_name()
        new_unit   = dlg.get_unit()
        new_coeff  = dlg.get_coeff()
        new_offset = dlg.get_offset()

        # Применить к PlotArea
        self._plot_area.set_channel_calib(idx, new_coeff, new_offset)

        # Обновить ChannelInfo в текущем блоке
        if block is not None and idx < len(block.channels):
            block.channels[idx].name   = new_name
            block.channels[idx].unit   = new_unit
            block.channels[idx].scale  = new_coeff
            block.channels[idx].offset = new_offset
            self._session.modified = True
            self._channel_panel.update_unit(idx, new_unit)
            self._channel_panel.update_name(idx, new_name)
            self._amp_scale.update_name(idx, new_name)

        # Обновить все блоки сессии с тем же числом каналов (опционально)
        # Сохранить в .cal файл рядом с .fgd
        if self._session.file_path:
            self._save_calibration_file()

    def _save_calibration_file(self):
        """Сохранить калибровку всех каналов текущего блока в .cal файл."""
        block = self._current_block()
        if block is None or not self._session.file_path:
            return
        try:
            names   = [ch.name   for ch in block.channels]
            units   = [ch.unit   for ch in block.channels]
            coeffs  = [ch.scale  for ch in block.channels]
            offsets = [ch.offset for ch in block.channels]
            cal_path = calib_file.cal_path_for(self._session.file_path)
            calib_file.save_calibration(cal_path, names, units, coeffs, offsets)
        except Exception as e:
            pass  # некритично

    def _load_calibration_file(self):
        """Загрузить калибровку из .cal файла и применить к текущему блоку."""
        if not self._session.file_path:
            return
        cal_path = calib_file.cal_path_for(self._session.file_path)
        entries  = calib_file.load_calibration(cal_path)
        if not entries:
            return
        block = self._current_block()
        if block is None:
            return
        coeffs  = []
        offsets = []
        for i, ch in enumerate(block.channels):
            if i < len(entries):
                e = entries[i]
                ch.name   = e['name']
                ch.unit   = e['unit']
                ch.scale  = e['coeff']
                ch.offset = e['offset']
            coeffs.append(ch.scale)
            offsets.append(ch.offset)
        self._plot_area.set_all_calibrations(coeffs, offsets)
        # Обновить UI
        for i, ch in enumerate(block.channels):
            self._channel_panel.update_unit(i, ch.unit)
            self._channel_panel.update_name(i, ch.name)
            self._amp_scale.update_name(i, ch.name)

    # ==================================================================
    # Следящий режим
    # ==================================================================

    def _on_following_changed(self, following: bool):
        self._act_follow.blockSignals(True)
        self._act_follow.setChecked(following)
        self._act_follow.blockSignals(False)

    # ==================================================================
    # Новый файл
    # ==================================================================

    def _on_new_file(self):
        if self._state != AppState.IDLE:
            r = QMessageBox.question(
                self, 'Новый файл',
                'Идёт запись / мониторинг. Остановить и создать новый файл?',
                QMessageBox.Yes | QMessageBox.No,
            )
            if r != QMessageBox.Yes:
                return
            self._on_stop()

        if self._session.modified or self._session.n_blocks > 0:
            r = QMessageBox.question(
                self, 'Новый файл',
                'Текущие данные будут потеряны. Сохранить перед созданием нового файла?',
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if r == QMessageBox.Cancel:
                return
            if r == QMessageBox.Yes:
                if not self._on_save():
                    return

        # Сброс всего
        self._session           = Session()
        self._current_block_idx = -1
        self._plot_area.clear_everything()
        self._nav_bar.setup_channels(0, [])
        self._channel_panel.setup([], [])
        self._amp_scale.setup([], [])
        self._refresh_block_list()
        self.setWindowTitle(APP_NAME)
        self._lbl_source.setText('Источник: нет')
        self._lbl_mode.setText('')

    # ==================================================================
    # Y-ось: выравнивание и масштаб
    # ==================================================================

    def _apply_y_results(self, results: list[tuple[float, float]]):
        """Обновить ChannelPanel и перерисовать после изменения Y-параметров."""
        for i, (s, o) in enumerate(results):
            self._channel_panel.update_scale_offset(i, s, o)
        self._plot_area.request_redraw()

    def _on_y_overlay(self):
        self._apply_y_results(self._plot_area.y_align_overlay())

    def _on_y_distribute(self):
        self._apply_y_results(self._plot_area.y_align_distribute())

    def _on_y_auto(self):
        self._apply_y_results(self._plot_area.y_align_auto())

    def _on_y_grids(self):
        self._apply_y_results(self._plot_area.y_align_grids())

    def _on_y_reset(self):
        self._apply_y_results(self._plot_area.y_reset())
        self._plot_area.set_y_autorange(True)
        self._act_auto_y.setChecked(True)

    def _on_y_zoom_in(self):
        """Ctrl+= → приблизить Y активного канала."""
        r = self._plot_area.y_zoom_active(1.5)
        if r:
            idx = self._plot_area.active_channel
            self._channel_panel.update_scale_offset(idx, r[0], r[1])

    def _on_y_zoom_out(self):
        """Ctrl+- → отдалить Y активного канала."""
        r = self._plot_area.y_zoom_active(1.0 / 1.5)
        if r:
            idx = self._plot_area.active_channel
            self._channel_panel.update_scale_offset(idx, r[0], r[1])

    def _on_y_zoom_in_all(self):
        """Alt+= → приблизить Y всех каналов."""
        self._apply_y_results(self._plot_area.y_zoom_all(1.5))

    def _on_y_zoom_out_all(self):
        """Alt+- → отдалить Y всех каналов."""
        self._apply_y_results(self._plot_area.y_zoom_all(1.0 / 1.5))

    # ==================================================================
    # Вид
    # ==================================================================

    def _on_autorange_y(self):
        self._plot_area.set_y_autorange(True)
        self._act_auto_y.setChecked(True)

    def _toggle_channel_panel(self):
        self._channel_panel.parent().setVisible(
            not self._channel_panel.parent().isVisible()
        )

    def _on_stats_toggle(self, checked: bool):
        self._stats_panel.setVisible(checked)
        if checked:
            sizes = self._center_splitter.sizes()
            total = sum(sizes)
            if sizes[1] < 60:
                self._center_splitter.setSizes([total - 120, 120])

    # ==================================================================
    # Выбор и настройка источника
    # ==================================================================

    def _on_source_type_changed(self):
        idx = self._cb_source.currentIndex()
        self._source_type = self._cb_source.itemData(idx)

    def _on_source_config(self):
        """Открыть диалог настройки текущего источника."""
        st = self._source_type
        if st == SourceType.GENERATOR:
            self._configure_generator()
        elif st == SourceType.COM_ASCII:
            self._configure_com_ascii()
        elif st == SourceType.COM_COBS:
            self._configure_com_cobs()
        elif st == SourceType.COM_MCOBS:
            self._configure_com_mcobs()

    def _configure_generator(self):
        if self._state != AppState.IDLE:
            return
        dlg = VirtualGeneratorDialog(self._gen_config, parent=self)
        if dlg.exec():
            self._gen_config = dlg.get_config()
            if self._source and isinstance(self._source, VirtualGenerator):
                self._source.set_config(self._gen_config)
                names  = self._source.get_channel_names()
                self._plot_area.setup(
                    n_channels=self._gen_config.n_channels,
                    names=names,
                    sample_rate=self._gen_config.sample_rate,
                )
                colors = self._plot_area.get_channel_colors()
                self._channel_panel.setup(names, colors)
                self._nav_bar.setup_channels(self._gen_config.n_channels, colors)

    def _configure_com_ascii(self):
        if self._state != AppState.IDLE:
            return
        dlg = ComAsciiDialog(self._com_config, parent=self)
        if dlg.exec():
            self._com_config = dlg.get_config()

    def _configure_com_cobs(self):
        if self._state != AppState.IDLE:
            return
        dlg = ComCobsDialog(self._cobs_config, parent=self)
        if dlg.exec():
            self._cobs_config = dlg.get_cobs_config()

    def _configure_com_mcobs(self):
        if self._state != AppState.IDLE:
            return
        dlg = ComMCobsDialog(self._mcobs_config, parent=self)
        if dlg.exec():
            self._mcobs_config = dlg.get_mcobs_config()

    # Обратная совместимость с пунктами меню
    def _on_generator_config(self):
        self._cb_source.setCurrentIndex(
            next(i for i in range(self._cb_source.count())
                 if self._cb_source.itemData(i) == SourceType.GENERATOR)
        )
        self._configure_generator()

    def _on_com_config(self):
        self._cb_source.setCurrentIndex(
            next(i for i in range(self._cb_source.count())
                 if self._cb_source.itemData(i) == SourceType.COM_ASCII)
        )
        self._configure_com_ascii()

    # ==================================================================
    # Статус
    # ==================================================================

    def _update_rec_counter(self):
        if self._state != AppState.RECORDING:
            return
        elapsed = int(time.time() - self._rec_start_wall)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)

        # Оценка занятого пространства (потоковая запись → диск)
        n_ch = self._rec_n_channels or 0
        size_mb = self._rec_total_pts * n_ch * (4 + 8/n_ch if n_ch else 4) / 1_048_576 if n_ch else 0
        if self._tmp_t_file is not None:
            size_str = f'  ~{size_mb:.0f} МБ диск' if size_mb > 10 else ''
        else:
            size_str = f'  ~{size_mb:.0f} МБ RAM' if size_mb > 50 else ''

        self._lbl_rec_cnt.setText(
            f'⏺ {h:02d}:{m:02d}:{s:02d}  {self._rec_total_pts:,} пт{size_str}'
        )

    def _update_status(self):
        self._lbl_rate.setText(f'{self._pkt_count} пак/с')
        self._pkt_count = 0

    def _update_title(self):
        fp   = self._session.file_path
        name = Path(fp).name if fp else 'новый файл'
        mark = ' *' if self._session.modified else ''
        self.setWindowTitle(f'{APP_NAME} — {name}{mark}')

    def _update_recent_menu(self):
        self._menu_recent.clear()
        files: list = config.get('recent_files', default=[])
        if not files:
            self._menu_recent.addAction('(пусто)').setEnabled(False)
        for path in files:
            self._menu_recent.addAction(path, lambda p=path: self._load_recent(p))

    def _load_recent(self, path: str):
        try:
            self._session = file_io.load(path)
            self._update_title()
            self._refresh_block_list()
            if self._session.n_blocks > 0:
                self._current_block_idx = 0
                self._display_block(0)
        except Exception as e:
            QMessageBox.critical(self, 'Ошибка', str(e))

    # ==================================================================
    # Выделение области данных
    # ==================================================================

    def _on_selection_changed(self, t0: float, t1: float, n: int):
        if n == 0:
            self._lbl_sel.setText('')
            self._stats_panel.clear()
        else:
            dt = t1 - t0
            self._lbl_sel.setText(
                f'▐ Выделение: {dt:.4f} с  ·  {n:,} отсч.'
            )
            self._update_stats(t0, t1)

    def _update_stats(self, t0: float, t1: float):
        """Вычислить и показать статистику по выделенному диапазону."""
        block = self._current_block()
        if block is None:
            # Живой режим — работаем с PlotArea данными
            self._stats_panel.clear()
            return
        t = block.times
        i0 = max(0, int(np.searchsorted(t, t0)) - 1)
        i1 = min(len(t), int(np.searchsorted(t, t1)) + 1)
        if i1 <= i0:
            self._stats_panel.clear()
            return
        seg = block.values[i0:i1]  # (n, ch)
        channel_data = []
        units = []
        for ci, ch in enumerate(block.channels):
            A = ch.scale
            B = ch.offset
            col = seg[:, ci].astype(np.float64) * A + B
            channel_data.append(col)
            units.append(ch.unit)
        self._stats_panel.update_stats(channel_data, units)

    def _on_selection_action(self, action: str, t0: float, t1: float):
        if action == 'copy':
            self._copy_selection_to_block(t0, t1)
        elif action == 'delete':
            self._delete_selection_from_block(t0, t1)

    def _copy_selection_to_block(self, t0: float, t1: float):
        block = self._current_block()
        if block is None:
            return
        t  = block.times
        i0 = max(0, int(np.searchsorted(t, t0)))
        i1 = min(len(t), int(np.searchsorted(t, t1)) + 1)
        if i1 - i0 < 2:
            QMessageBox.information(self, 'Выделение',
                                    'В выделении недостаточно данных.')
            return
        ch_info   = [
            ChannelInfo(name=ch.name, unit=ch.unit, scale=ch.scale, offset=ch.offset)
            for ch in block.channels
        ]
        new_block = Block(
            start_time  = block.start_time,
            source_name = block.source_name + ' [выделение]',
            sample_rate = block.sample_rate,
            channels    = ch_info,
            times       = block.times[i0:i1].copy(),
            values      = block.values[i0:i1].copy(),
        )
        self._session.add_block(new_block)
        self._plot_area.clear_selection()
        self._refresh_block_list()
        self._update_session_overview()
        QMessageBox.information(
            self, 'Скопировано',
            f'Создан блок {self._session.n_blocks}: '
            f'{new_block.n_samples:,} отсчётов  ({new_block.duration:.3f} с).',
        )

    def _delete_selection_from_block(self, t0: float, t1: float):
        block = self._current_block()
        if block is None:
            return
        t  = block.times
        i0 = max(0, int(np.searchsorted(t, t0)))
        i1 = min(len(t), int(np.searchsorted(t, t1)) + 1)
        n_del = i1 - i0
        if n_del < 1:
            return

        r = QMessageBox.question(
            self, 'Удалить выделенные данные',
            f'Удалить {n_del:,} отсчётов [{t0:.4f} … {t1:.4f} с] из блока {block.index + 1}?\n'
            'Блок будет разделён на два. Действие необратимо.',
            QMessageBox.Yes | QMessageBox.No,
        )
        if r != QMessageBox.Yes:
            return

        orig_idx = block.index
        ch_info  = [ChannelInfo(name=ch.name, unit=ch.unit,
                                scale=ch.scale, offset=ch.offset)
                    for ch in block.channels]

        # Левая часть (до выделения)
        left_ok  = i0 >= 2
        # Правая часть (после выделения)
        right_ok = (len(t) - i1) >= 2

        # Убрать оригинальный блок
        self._session.blocks.pop(orig_idx)

        ins = orig_idx
        if left_ok:
            bl = Block(
                start_time  = block.start_time,
                source_name = block.source_name,
                sample_rate = block.sample_rate,
                channels    = [ChannelInfo(name=c.name, unit=c.unit,
                                           scale=c.scale, offset=c.offset)
                               for c in block.channels],
                times       = block.times[:i0].copy(),
                values      = block.values[:i0].copy(),
            )
            bl.index = ins
            self._session.blocks.insert(ins, bl)
            ins += 1

        if right_ok:
            br = Block(
                start_time  = block.start_time,
                source_name = block.source_name,
                sample_rate = block.sample_rate,
                channels    = [ChannelInfo(name=c.name, unit=c.unit,
                                           scale=c.scale, offset=c.offset)
                               for c in block.channels],
                times       = block.times[i1:].copy(),
                values      = block.values[i1:].copy(),
            )
            br.index = ins
            self._session.blocks.insert(ins, br)

        # Переиндексировать всё
        for i, b in enumerate(self._session.blocks):
            b.index = i
        self._session.modified = True

        self._plot_area.clear_selection()
        self._current_block_idx = min(orig_idx, len(self._session.blocks) - 1)
        self._refresh_block_list()
        if self._session.blocks:
            self._display_block(self._current_block_idx)
        self._update_session_overview()

    # ==================================================================
    # Экспорт данных
    # ==================================================================

    def _current_block(self) -> Block | None:
        if 0 <= self._current_block_idx < self._session.n_blocks:
            return self._session.blocks[self._current_block_idx]
        return None

    def _current_view_range(self) -> tuple[float, float] | None:
        try:
            vr = self._plot_area._plot.plotItem.getViewBox().viewRange()[0]
            return float(vr[0]), float(vr[1])
        except Exception:
            return None

    def _on_export_csv(self):
        block = self._current_block()
        if block is None:
            QMessageBox.information(self, 'Экспорт',
                'Нет данных для экспорта. Запишите или загрузите блок.')
            return

        view_range = self._current_view_range()
        dlg = ExportCsvDialog(block, view_range=view_range, parent=self)
        if dlg.exec() != ExportCsvDialog.Accepted:
            return

        settings = dlg.get_settings()
        sep      = settings['separator']
        ext_map  = {'\t': 'txt', ',': 'csv', ';': 'csv'}
        ext      = ext_map.get(sep, 'csv')
        filt     = (f'CSV файл (*.{ext});;Все файлы (*)' if ext == 'csv'
                    else f'Текстовый файл (*.txt);;Все файлы (*)')

        path, _ = QFileDialog.getSaveFileName(
            self, 'Экспорт в текстовый файл', f'block_{block.index+1}.{ext}', filt
        )
        if not path:
            return

        try:
            n = export_csv(block, path, settings, view_range)
            QMessageBox.information(
                self, 'Экспорт завершён',
                f'Сохранено {n:,} строк в файл:\n{path}'
            )
        except Exception as e:
            QMessageBox.critical(self, 'Ошибка экспорта', str(e))

    def _on_export_png(self):
        import pyqtgraph as pg
        path, _ = QFileDialog.getSaveFileName(
            self, 'Сохранить график как PNG',
            'flowergraph_plot.png',
            'PNG изображение (*.png);;Все файлы (*)'
        )
        if not path:
            return
        try:
            exporter = pg.exporters.ImageExporter(
                self._plot_area._plot.plotItem
            )
            # Ширина 1920 пкс, высота пропорционально
            exporter.parameters()['width'] = 1920
            exporter.export(path)
            QMessageBox.information(self, 'Экспорт завершён',
                f'График сохранён:\n{path}')
        except Exception as e:
            QMessageBox.critical(self, 'Ошибка экспорта', str(e))

    def _on_export_pgc(self):
        block = self._current_block()
        if block is None:
            QMessageBox.information(self, 'Экспорт PGC', 'Нет активного блока для экспорта.')
            return
        path, _ = QFileDialog.getSaveFileName(self, 'Экспорт в .pgc', '', PGC_FILTER)
        if not path:
            return
        try:
            pg_export.save(block, path)
            QMessageBox.information(self, 'Экспорт завершён', f'Сохранено:\n{path}')
        except Exception as e:
            QMessageBox.critical(self, 'Ошибка экспорта', str(e))

    def _set_language(self, lang: str):
        if get_lang() == lang:
            return
        set_lang(lang)
        config.set('language', value=lang)
        config.save()
        stop_en  = self._act_stop.isEnabled()
        start_en = self._act_start.isEnabled()
        self.menuBar().clear()
        self._build_menu()
        self._act_start.setEnabled(start_en)
        self._act_stop.setEnabled(stop_en)

    # ==================================================================

    def _on_load_calib_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Загрузить калибровку', '', 'Calibration (*.cal);;All (*)')
        if not path:
            return
        entries = calib_file.load_calibration(path)
        if not entries:
            QMessageBox.information(self, 'Калибровка', 'Файл не содержит данных.')
            return
        block = self._current_block()
        if block:
            for i, e in enumerate(entries):
                if i >= len(block.channels):
                    break
                block.channels[i].name   = e['name']
                block.channels[i].unit   = e['unit']
                block.channels[i].scale  = e['coeff']
                block.channels[i].offset = e['offset']
            self._session.modified = True
        coeffs  = [e['coeff']  for e in entries]
        offsets = [e['offset'] for e in entries]
        self._plot_area.set_all_calibrations(coeffs, offsets)
        if block:
            for i, e in enumerate(entries):
                if i >= len(block.channels):
                    break
                self._channel_panel.update_unit(i, e['unit'])
                self._channel_panel.update_name(i, e['name'])

    def _on_save_calib_file(self):
        block = self._current_block()
        if block is None:
            QMessageBox.information(self, 'Калибровка', 'Нет активного блока.')
            return
        default = ''
        if self._session.file_path:
            default = str(calib_file.cal_path_for(self._session.file_path))
        path, _ = QFileDialog.getSaveFileName(
            self, 'Сохранить калибровку', default,
            'Calibration (*.cal);;All (*)')
        if not path:
            return
        try:
            names   = [ch.name   for ch in block.channels]
            units   = [ch.unit   for ch in block.channels]
            coeffs  = [ch.scale  for ch in block.channels]
            offsets = [ch.offset for ch in block.channels]
            calib_file.save_calibration(path, names, units, coeffs, offsets)
            QMessageBox.information(self, 'Калибровка сохранена', path)
        except Exception as e:
            QMessageBox.critical(self, 'Ошибка', str(e))

    def _on_about(self):
        QMessageBox.about(
            self, f'О программе {APP_NAME}',
            f'<b>{APP_NAME}</b> v{APP_VERSION}<br>'
            'Регистрация, визуализация и анализ сигналов.<br><br>'
            'Python + PySide6 + pyqtgraph'
        )

    # ==================================================================
    # Утилиты
    # ==================================================================

    def _action(self, title: str, slot, shortcut=None) -> QAction:
        a = QAction(title, self)
        if shortcut:
            a.setShortcut(shortcut)
        a.triggered.connect(slot)
        return a

    def _stub(self):
        pass

    def keyPressEvent(self, event):
        from PySide6.QtWidgets import QLineEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox
        fw = self.focusWidget()
        if isinstance(fw, (QLineEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox)):
            super().keyPressEvent(event)
            return
        key = event.key()
        if key == Qt.Key_Left:
            self._plot_area.scroll_left_div()
            event.accept()
        elif key == Qt.Key_Right:
            self._plot_area.scroll_right_div()
            event.accept()
        elif key == Qt.Key_Up:
            r = self._plot_area.shift_active_offset(+1.0)
            if r:
                idx = self._plot_area.active_channel
                self._channel_panel.update_scale_offset(idx, r[0], r[1])
            event.accept()
        elif key == Qt.Key_Down:
            r = self._plot_area.shift_active_offset(-1.0)
            if r:
                idx = self._plot_area.active_channel
                self._channel_panel.update_scale_offset(idx, r[0], r[1])
            event.accept()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        if self._state != AppState.IDLE:
            self._on_stop()
        if self._session.modified:
            r = QMessageBox.question(
                self, 'Несохранённые данные',
                'Сессия содержит несохранённые данные. Сохранить перед выходом?',
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if r == QMessageBox.Cancel:
                event.ignore()
                return
            if r == QMessageBox.Yes:
                if not self._on_save():
                    event.ignore()
                    return
        geo = self.geometry()
        config.set('window', 'x',         value=geo.x())
        config.set('window', 'y',         value=geo.y())
        config.set('window', 'width',     value=geo.width())
        config.set('window', 'height',    value=geo.height())
        config.set('window', 'maximized', value=self.isMaximized())
        config.set('splitter_sizes',      value=list(self._main_splitter.sizes()))
        config.set('center_splitter_sizes', value=list(self._center_splitter.sizes()))
        config.set('source_type',      value=self._source_type.value)
        config.set('source_ascii',     value=self._com_config.to_dict())
        config.set('source_cobs',      value=self._cobs_config.to_dict())
        config.set('source_mcobs',     value=self._mcobs_config.to_dict())
        config.set('source_generator', value=self._gen_config.to_dict())
        config.save()
        if getattr(self, '_tray', None) is not None:
            self._tray.hide()
        super().closeEvent(event)


def _sep() -> QWidget:
    w = QWidget()
    w.setFixedWidth(8)
    return w
