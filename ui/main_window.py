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
import sys
import time
import tempfile
from enum import Enum, auto
from pathlib import Path

import numpy as np
from PySide6.QtWidgets import (
    QMainWindow, QStatusBar, QMenuBar, QToolBar, QToolButton, QDockWidget,
    QLabel, QMessageBox, QComboBox, QWidget, QFrame,
    QHBoxLayout, QVBoxLayout, QPushButton,
    QListWidget, QListWidgetItem, QFileDialog, QInputDialog,
    QSystemTrayIcon, QMenu,
)
from PySide6.QtCore import QSize, QTimer, Qt, QByteArray
from PySide6.QtGui import QAction, QKeySequence, QGuiApplication

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
from ui.stats_panel import StatsPanel
from ui.calib_dialog import ChannelCalibDialog
from plugins.virtual_generator import VirtualGenerator, VirtualGeneratorDialog, GeneratorConfig
from plugins.com_ascii_source import ComAsciiSource, ComAsciiConfig
from plugins.com_cobs_source import ComCobsSource, ComCobsConfig, ComCobsDialog
from plugins.com_mcobs_source import ComMCobsSource, ComMCobsConfig, ComMCobsDialog
from plugins.fg_net_source import FgNetSource, FgNetConfig, FgNetConfigDialog
from plugins.base_source import BaseSource
from plugins import pg_export
from ui.com_ascii_dialog import ComAsciiDialog
from core.i18n import tr, set_lang, get_lang
from core.measure import fmt_span, index_range

APP_NAME    = 'FlowerGraph'
# Полная версия MAJOR.MINOR.PATCH.BUILD — единственный источник истины.
# Релиз: поднять последнюю компоненту, закоммитить, повесить тег v<APP_VERSION>.
APP_VERSION = '0.7.3.124'


def _build_number() -> str:
    """Фолбэк-номер сборки для 3-компонентной APP_VERSION (старый режим): из env
    FG_BUILD_NUMBER / GITHUB_RUN_NUMBER, иначе build_number.txt (инкрементит
    FlowerGraph.spec). '0' если ничего не найдено. При 4-компонентной APP_VERSION
    не используется."""
    n = (os.environ.get('FG_BUILD_NUMBER')
         or os.environ.get('GITHUB_RUN_NUMBER') or '').strip()
    if n:
        return n
    for base in (getattr(sys, '_MEIPASS', None),
                 str(Path(__file__).resolve().parent.parent)):
        if not base:
            continue
        try:
            v = (Path(base) / 'build_number.txt').read_text(encoding='utf-8').strip()
            if v:
                return v
        except OSError:
            pass
    return '0'


APP_BUILD        = APP_VERSION.rsplit('.', 1)[-1] if APP_VERSION.count('.') >= 3 else _build_number()
APP_VERSION_FULL = APP_VERSION if APP_VERSION.count('.') >= 3 else f'{APP_VERSION}.{APP_BUILD}'
FILE_FILTER    = 'FlowerGraph Data (*.fgd);;Все файлы (*)'
PGC_FILTER     = 'PGC (*.pgc);;Все файлы (*)'


class AppState(Enum):
    IDLE      = auto()
    RECORDING = auto()


class SourceType(Enum):
    GENERATOR = 'generator'
    COM_ASCII = 'com_ascii'
    COM_COBS  = 'com_cobs'
    COM_MCOBS = 'com_mcobs'
    FG_NET    = 'fg_net'

_SOURCE_LABELS = {
    SourceType.GENERATOR: 'Генератор',
    SourceType.COM_ASCII: 'COM ASCII',
    SourceType.COM_COBS:  'COM COBS',
    SourceType.COM_MCOBS: 'COM mCOBS',
    SourceType.FG_NET:    'FG-NET (Wi-Fi)',
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
        self._fgnet_config      = FgNetConfig()
        self._current_sample_rate = 1000

        # виджеты
        self._plot_area      = PlotArea()
        self._channel_panel  = ChannelPanel()
        self._nav_bar        = NavBar()
        self._stats_panel    = StatsPanel()
        self._error_log: list[str] = []
        self._error_count = 0
        self._last_error = ''
        self._samples_win = 0
        self._quit_from_tray = False
        self._channel_names: list[str] = []
        self._channel_units: list[str] = []

        self._channel_panel.sig_visibility.connect(self._plot_area.set_channel_visible)
        self._channel_panel.sig_scale.connect(self._on_y_per_div_edited)
        self._channel_panel.sig_offset.connect(self._plot_area.set_channel_offset)
        self._channel_panel.sig_auto.connect(self._on_auto_scale)
        self._channel_panel.sig_calib_requested.connect(self._on_calib_requested)
        self._channel_panel.sig_activated.connect(self._on_channel_activated)

        # PlotArea → NavBar (через MainWindow для учёта глобальных смещений блоков)
        self._plot_area.overview_ready.connect(self._on_plot_overview_ready)
        self._plot_area.view_range_changed.connect(self._on_view_range_changed)
        self._plot_area.time_label_changed.connect(self._nav_bar.update_scale_label)
        self._plot_area.zoom_limits_changed.connect(self._nav_bar.set_zoom_enabled)
        self._plot_area.channel_changed.connect(self._sync_channel_panel)
        self._plot_area.autorange_changed.connect(self._on_autorange_changed)
        self._plot_area.context_action.connect(self._on_plot_context)

        # NavBar → PlotArea (через MainWindow для навигации между блоками)
        self._nav_bar.navigate_to.connect(self._on_nav_navigate)
        self._nav_bar.go_start.connect(self._plot_area.go_to_start)
        self._nav_bar.go_end.connect(self._plot_area.go_to_end)
        self._nav_bar.prev_block.connect(self._on_prev_block)
        self._nav_bar.next_block.connect(self._on_next_block)
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
        self._build_central()
        self._build_menu()
        self._build_toolbar()
        self._build_statusbar()
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
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION_FULL}")
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
        act_quit.triggered.connect(self._quit_via_tray)
        menu.addAction(act_show)
        menu.addSeparator()
        menu.addAction(act_quit)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _quit_via_tray(self):
        self._quit_from_tray = True
        self.close()

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
        central = QWidget()
        cv = QVBoxLayout(central)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        cv.addWidget(self._plot_area, stretch=1)
        cv.addWidget(self._nav_bar)
        self.setCentralWidget(central)
        self._plot_area.show_placeholder(
            'Нет данных. Выберите источник и нажмите Старт.'
        )

        self._dock_channels = QDockWidget(tr('channels_title'), self)
        self._dock_channels.setObjectName('dock_channels')
        self._dock_channels.setWidget(self._channel_panel)
        self.addDockWidget(Qt.RightDockWidgetArea, self._dock_channels)

        block_host = QWidget()
        bv = QVBoxLayout(block_host)
        bv.setContentsMargins(0, 0, 0, 0)
        self._block_list = QListWidget()
        self._block_list.setMinimumHeight(80)
        self._block_list.currentRowChanged.connect(self._on_block_row_changed)
        self._block_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._block_list.customContextMenuRequested.connect(self._on_block_menu)
        bv.addWidget(self._block_list)
        self._dock_blocks = QDockWidget(tr('blocks_title'), self)
        self._dock_blocks.setObjectName('dock_blocks')
        self._dock_blocks.setWidget(block_host)
        self.addDockWidget(Qt.RightDockWidgetArea, self._dock_blocks)
        self.splitDockWidget(self._dock_channels, self._dock_blocks, Qt.Vertical)

        self._dock_stats = QDockWidget(tr('stats_title'), self)
        self._dock_stats.setObjectName('dock_stats')
        self._dock_stats.setWidget(self._stats_panel)
        self.addDockWidget(Qt.BottomDockWidgetArea, self._dock_stats)
        self.resizeDocks([self._dock_channels, self._dock_blocks], [360, 160], Qt.Vertical)
        self.resizeDocks([self._dock_channels], [320], Qt.Horizontal)
        self.resizeDocks([self._dock_stats], [110], Qt.Vertical)

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

        d = config.get('source_fgnet', default=None)
        if isinstance(d, dict):
            self._fgnet_config = FgNetConfig.from_dict(d)

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
        blob = config.get('window_geometry_b64', default=None)
        state = config.get('window_state_b64', default=None)
        if isinstance(blob, str) and blob:
            self.restoreGeometry(QByteArray.fromBase64(blob.encode('ascii')))
        else:
            w = config.get('window')
            self.resize(w['width'], w['height'])
            self.move(w['x'], w['y'])
            if w.get('maximized'):
                self.showMaximized()
        if isinstance(state, str) and state:
            self.restoreState(QByteArray.fromBase64(state.encode('ascii')))
        self._ensure_on_screen()

    def _ensure_on_screen(self):
        screen = QGuiApplication.screenAt(self.frameGeometry().center())
        if screen is not None:
            return
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(geo.x() + 40, geo.y() + 40)
        if self.width() > geo.width() or self.height() > geo.height():
            self.resize(min(self.width(), geo.width() - 80),
                        min(self.height(), geo.height() - 80))

    # ==================================================================
    # Меню и тулбар: одна команда — один QAction
    # ==================================================================

    def _act(self, key: str, title: str, slot, shortcut=None, tip: str | None = None,
             checkable: bool = False) -> QAction:
        action = QAction(title, self)
        if shortcut is not None:
            if isinstance(shortcut, (list, tuple)):
                action.setShortcuts([QKeySequence(s) if not isinstance(s, QKeySequence) else s
                                     for s in shortcut])
            else:
                action.setShortcut(shortcut if isinstance(shortcut, QKeySequence)
                                   else QKeySequence(shortcut))
            action.setShortcutContext(Qt.WindowShortcut)
        if tip:
            action.setToolTip(tip)
            action.setStatusTip(tip)
        action.setCheckable(checkable)
        action.triggered.connect(slot)
        self._actions[key] = action
        return action

    def _create_actions(self):
        self._actions = {}
        A = self._act
        A('new', tr('new'), self._on_new_file, QKeySequence.StandardKey.New)
        A('open', tr('open'), self._on_open, QKeySequence.StandardKey.Open)
        A('save', tr('save'), self._on_save, QKeySequence.StandardKey.Save)
        A('save_as', tr('save_as'), self._on_save_as, QKeySequence.StandardKey.SaveAs)
        A('import', tr('import_pgc'), self._on_import_pgc)
        A('export_csv', tr('export_csv'), self._on_export_csv, 'Ctrl+E')
        A('export_png', tr('export_png'), self._on_export_png, 'Ctrl+Shift+E')
        A('export_pgc', tr('export_pgc'), self._on_export_pgc)
        A('quit', tr('quit'), self.close, QKeySequence.StandardKey.Quit)
        self._act_run = A('run', tr('start'), self._on_start_stop, ['Space', 'F9'],
                          'Старт или стоп записи')
        A('mark', tr('mark'), self._on_add_annotation, ['Ctrl+M', 'Insert'],
          'Метка в записи')
        A('escape', 'Снять выделение', self._on_escape, 'Escape')
        A('src_cfg', '⚙ Настройка…', self._on_source_config, tip='Настройка выбранного источника')
        self._act_follow = A('follow', tr('follow'), self._on_follow_toggled, checkable=True)
        self._act_follow.setChecked(True)
        self._act_auto_y = A('auto_y', tr('auto_y'), self._on_auto_y_toggled, 'F5', checkable=True)
        self._act_auto_y.setChecked(True)
        self._act_markers = A('markers', tr('markers'), self._on_markers_toggled,
                              tip='Маркеры ΔT / ΔY', checkable=True)
        self._act_legend = A('legend', 'Легенда', self._on_legend_toggled, checkable=True)
        A('y_overlay', 'Совместить базовые уровни', self._on_y_overlay, 'Alt+0')
        A('y_dist', 'Разнести по вертикали', self._on_y_distribute, 'Alt+D')
        A('y_auto', 'Авто каждый независимо', self._on_y_auto, 'Alt+A')
        A('y_reset', 'Сбросить масштаб Y', self._on_y_reset, 'Alt+R')
        A('y_in', 'Масштаб Y: приблизить', self._on_y_zoom_in,
          [QKeySequence.StandardKey.ZoomIn, 'Ctrl++', 'Ctrl+='])
        A('y_out', 'Масштаб Y: отдалить', self._on_y_zoom_out, 'Ctrl+-')
        A('y_in_all', 'Приблизить Y всех', self._on_y_zoom_in_all, 'Alt+=')
        A('y_out_all', 'Отдалить Y всех', self._on_y_zoom_out_all, 'Alt+-')
        A('t_in', 'Масштаб времени: приблизить', self._plot_area.zoom_in_discrete,
          [QKeySequence.StandardKey.ZoomIn, 'Shift++', 'Shift+='])
        # ZoomIn на двух действиях снова даст ambiguous. Время — только Shift.
        self._actions['t_in'].setShortcuts([QKeySequence('Shift++'), QKeySequence('Shift+=')])
        A('t_out', 'Масштаб времени: отдалить', self._plot_area.zoom_out_discrete, 'Shift+-')
        A('nav_left', 'На деление влево', self._plot_area.scroll_left_div, 'Left')
        A('nav_right', 'На деление вправо', self._plot_area.scroll_right_div, 'Right')
        A('nav_page_l', 'Предыдущая страница', self._plot_area.page_left, 'PgUp')
        A('nav_page_r', 'Следующая страница', self._plot_area.page_right, 'PgDown')
        A('nav_home', 'В начало', self._plot_area.go_to_start, 'Home')
        A('nav_end', 'В конец / следить', self._plot_area.go_to_end, 'End')
        A('ch_up', 'Сместить активный канал вверх', self._on_channel_nudge_up, 'Up')
        A('ch_down', 'Сместить активный канал вниз', self._on_channel_nudge_down, 'Down')
        A('blk_prev', 'Предыдущий блок', self._on_prev_block, 'Shift+PgUp')
        A('blk_next', 'Следующий блок', self._on_next_block, 'Shift+PgDown')
        A('load_cal', 'Загрузить калибровку из .cal…', self._on_load_calib_file)
        A('save_cal', 'Сохранить калибровку в .cal…', self._on_save_calib_file)
        A('about', tr('about'), self._on_about)

    def _build_menu(self):
        self._create_actions()
        mb: QMenuBar = self.menuBar()
        a = self._actions

        m_file = mb.addMenu(tr('menu_file'))
        for key in ('new', 'open', 'save', 'save_as'):
            if key == 'open':
                m_file.addSeparator()
            m_file.addAction(a[key])
        m_file.addSeparator()
        m_import = m_file.addMenu(tr('import'))
        m_import.addAction(a['import'])
        m_export = m_file.addMenu(tr('export'))
        m_export.addAction(a['export_csv'])
        m_export.addAction(a['export_png'])
        m_export.addAction(a['export_pgc'])
        m_file.addSeparator()
        self._menu_recent = m_file.addMenu(tr('recent'))
        self._update_recent_menu()
        m_file.addSeparator()
        m_file.addAction(a['quit'])

        m_view = mb.addMenu(tr('menu_view'))
        m_view.addAction(a['auto_y'])
        m_view.addAction(a['follow'])
        m_view.addAction(a['markers'])
        m_view.addAction(a['legend'])
        m_view.addSeparator()
        m_align = m_view.addMenu('Выравнивание Y')
        for key in ('y_overlay', 'y_dist', 'y_auto', 'y_reset'):
            m_align.addAction(a[key])
        m_align.addSeparator()
        for key in ('y_in', 'y_out', 'y_in_all', 'y_out_all'):
            m_align.addAction(a[key])
        m_view.addSeparator()
        m_view.addAction(self._dock_channels.toggleViewAction())
        m_view.addAction(self._dock_blocks.toggleViewAction())
        m_view.addAction(self._dock_stats.toggleViewAction())
        m_view.addSeparator()
        m_style = m_view.addMenu('Стиль графика')
        for key in (PLOT_STYLE_LINE, PLOT_STYLE_LINE_POINTS, PLOT_STYLE_POINTS, PLOT_STYLE_BARS):
            act = QAction(PLOT_STYLE_LABELS[key], self)
            act.triggered.connect(lambda _c=False, k=key: self._set_plot_style(k))
            m_style.addAction(act)
        m_fps = m_view.addMenu('Частота обновления')
        for fps in (10, 20, 25, 30, 60):
            act = QAction(f'{fps} FPS', self)
            act.triggered.connect(lambda _c=False, f=fps: self._plot_area.set_fps(f))
            m_fps.addAction(act)
        m_zoom = m_view.addMenu('Масштаб времени')
        m_zoom.addAction(a['t_in'])
        m_zoom.addAction(a['t_out'])
        m_nav = m_view.addMenu('Навигация')
        for key in ('nav_home', 'nav_page_l', 'nav_page_r', 'nav_end',
                    'nav_left', 'nav_right', 'ch_up', 'ch_down'):
            m_nav.addAction(a[key])

        m_rec = mb.addMenu(tr('menu_record'))
        m_rec.addAction(a['src_cfg'])
        m_rec.addSeparator()
        m_rec.addAction(self._act_run)
        m_rec.addSeparator()
        m_rec.addAction(a['mark'])

        m_blk = mb.addMenu(tr('menu_block'))
        m_blk.addAction(a['blk_prev'])
        m_blk.addAction(a['blk_next'])

        m_tools = mb.addMenu(tr('menu_tools'))
        m_cal = m_tools.addMenu('Калибровка каналов')
        m_cal.addAction(a['load_cal'])
        m_cal.addAction(a['save_cal'])
        m_tools.addSeparator()
        m_lang = m_tools.addMenu(tr('language') + ' / Language')
        m_lang.addAction(self._action(tr('lang_ru'), lambda: self._set_language('ru')))
        m_lang.addAction(self._action(tr('lang_en'), lambda: self._set_language('en')))

        m_help = mb.addMenu(tr('menu_help'))
        m_help.addAction(a['about'])

    def _build_toolbar(self):
        tb = QToolBar('Основная панель')
        tb.setObjectName('main_toolbar')
        tb.setIconSize(QSize(20, 20))
        tb.setMovable(True)
        self.addToolBar(tb)
        a = self._actions
        for key in ('new', 'open', 'save'):
            tb.addAction(a[key])
        tb.addSeparator()
        tb.addWidget(QLabel(' Источник: '))
        self._cb_source = QComboBox()
        self._cb_source.setFocusPolicy(Qt.ClickFocus)
        for st in SourceType:
            self._cb_source.addItem(_SOURCE_LABELS[st], st)
        self._cb_source.setMinimumWidth(120)
        self._cb_source.currentIndexChanged.connect(self._on_source_type_changed)
        tb.addWidget(self._cb_source)
        tb.addAction(a['src_cfg'])
        tb.addAction(self._act_run)
        tb.addAction(a['mark'])
        tb.addSeparator()
        tb.addAction(self._act_follow)
        tb.addAction(self._act_markers)
        tb.addSeparator()
        yb = QToolButton()
        yb.setText('Y')
        yb.setPopupMode(QToolButton.InstantPopup)
        yb.setFocusPolicy(Qt.NoFocus)
        ymenu = QMenu(yb)
        for key in ('y_overlay', 'y_dist', 'y_auto', 'y_reset', 'y_in', 'y_out', 'auto_y'):
            ymenu.addAction(a[key])
        yb.setMenu(ymenu)
        tb.addWidget(yb)

    def _set_plot_style(self, style: str):
        self._plot_area.set_plot_style(style)
        config.set('display', 'plot_style', value=style)

    def _on_follow_toggled(self, checked: bool):
        self._plot_area.set_following(bool(checked))

    def _on_auto_y_toggled(self, checked: bool):
        self._plot_area.set_y_autorange(bool(checked))

    def _on_markers_toggled(self, checked: bool):
        self._plot_area.show_markers(bool(checked))

    def _on_legend_toggled(self, checked: bool):
        self._plot_area.set_legend_visible(bool(checked))
        config.set('display', 'legend', value=bool(checked))

    def _on_escape(self):
        self._plot_area.clear_selection()

    def _on_channel_nudge_up(self):
        self._nudge_active(+1.0)

    def _on_channel_nudge_down(self):
        self._nudge_active(-1.0)

    def _nudge_active(self, divs: float):
        self._plot_area.shift_active_offset(divs)

    def _on_plot_context(self, action: str):
        if action == 'fit':
            self._on_show_block()
        elif action == 'auto_y':
            self._plot_area.set_y_autorange(True)
            self._act_auto_y.setChecked(True)
        elif action == 'png':
            self._on_export_png()

    def _on_autorange_changed(self, enabled: bool):
        self._act_auto_y.blockSignals(True)
        self._act_auto_y.setChecked(bool(enabled))
        self._act_auto_y.blockSignals(False)

    def _on_y_per_div_edited(self, idx: int, y_per_div: float):
        self._plot_area.set_active_channel(idx)
        self._channel_panel.set_active(idx)
        self._plot_area.set_y_per_div(idx, y_per_div)

    def _sync_channel_panel(self, _idx: int = -1):
        for state in self._plot_area.channel_states():
            self._channel_panel.apply_state(state)
            if 0 <= state.index < len(self._channel_names):
                pass
        self._channel_panel.set_active(self._plot_area.active_channel)

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
        self._lbl_errors.setCursor(Qt.PointingHandCursor)
        self._lbl_errors.mousePressEvent = lambda _e: self._show_error_log()
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
            label = _SOURCE_LABELS.get(self._source_type, 'источник')
            detail = self._last_error or 'Источник не сообщил причину.'
            QMessageBox.warning(
                self, 'Источник',
                f'Не удалось запустить «{label}».\n{detail}',
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
            self._plot_area.freeze_live_view()

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
        elif st == SourceType.FG_NET:
            src = FgNetSource(self._fgnet_config)
            cfg_n_ch = -1
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
            port = getattr(getattr(src, '_config', None), 'port', 'COM')
            self._plot_area.clear_everything()
            self._plot_area.show_placeholder(f'Ожидание данных с {port}…')
            self._channel_panel.setup([], [])
            self._stats_panel.setup([], [])
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

        self._act_run.setText(tr('start') if idle else tr('stop'))
        self._nav_bar.set_recording(active)
        self._plot_area.set_live_edit(not idle)
        if hasattr(self, '_cb_source'):
            self._cb_source.setEnabled(idle)
        for key in ('open', 'import'):
            self._actions[key].setEnabled(idle)
        if hasattr(self, '_menu_recent'):
            self._menu_recent.setEnabled(idle)
        self._actions['src_cfg'].setEnabled(idle)
        self._act_follow.setEnabled(not self._plot_area.is_static)

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
        self._channel_names = list(names)
        self._channel_units = [''] * n
        self._channel_panel.setup(names, colors)
        self._nav_bar.setup_channels(n, colors)
        self._stats_panel.setup(names, colors)
        self._sync_channel_panel()
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
        self._samples_win += int(len(times))
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
        self._last_error = msg
        self._error_log.append(msg)
        self._error_log = self._error_log[-40:]
        low = msg.lower()
        reconnect = low.startswith('попытка') or 'реконнект' in low
        if reconnect:
            self._lbl_source.setText(msg)
            return
        self._error_count += 1
        self._lbl_errors.setText(f'Ошибок: {self._error_count}')
        self._lbl_errors.setToolTip(msg)

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
        self._mark_modified()
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

    def _confirm_discard(self) -> bool:
        """Спросить про запись и несохранённую сессию. False — пользователь отменил."""
        if self._state != AppState.IDLE:
            answer = QMessageBox.question(
                self, 'Запись',
                'Идёт запись. Остановить?',
                QMessageBox.Yes | QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return False
            self._on_stop()
        if self._session.modified:
            answer = QMessageBox.question(
                self, 'Несохранённые данные',
                'Сохранить изменения?',
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if answer == QMessageBox.Cancel:
                return False
            if answer == QMessageBox.Yes and not self._on_save():
                return False
        return True

    def _reset_ui(self):
        self._session = Session()
        self._current_block_idx = -1
        self._block_global_offsets = []
        self._channel_names = []
        self._channel_units = []
        self._plot_area.clear_everything()
        self._plot_area.show_markers(False)
        self._act_markers.setChecked(False)
        self._plot_area.set_following(True)
        self._act_follow.setChecked(True)
        self._plot_area.set_y_autorange(True)
        self._act_auto_y.setChecked(True)
        self._plot_area.show_placeholder('Нет данных. Выберите источник и нажмите Старт.')
        self._nav_bar.setup_channels(0, [])
        self._nav_bar.update_overview(np.array([0.0, 1.0]), np.zeros((2, 1), dtype=np.float32))
        self._channel_panel.setup([], [])
        self._stats_panel.setup([], [])
        self._stats_panel.clear()
        self._refresh_block_list()
        self._lbl_markers.setText('')
        self._lbl_coords.setText('')
        self._lbl_sel.setText('')
        self._lbl_errors.setText('Ошибок: 0')
        self._lbl_errors.setToolTip('')
        self._error_count = 0
        self._lbl_source.setText('Источник: нет')
        self._lbl_mode.setText('')
        self._update_title()

    def _load_session(self, path: str) -> bool:
        if not os.path.exists(path):
            files = list(config.get('recent_files', default=[]))
            if path in files:
                files.remove(path)
                config.set('recent_files', value=files)
                config.save()
                self._update_recent_menu()
            QMessageBox.warning(self, 'Файл', f'Файл не найден и убран из списка:\n{path}')
            return False
        try:
            session = file_io.load(path)
        except Exception as exc:
            QMessageBox.critical(self, 'Ошибка открытия', str(exc))
            self._reset_ui()
            return False
        self._reset_ui()
        self._session = session
        self._session.modified = False
        config.add_recent_file(path)
        self._update_recent_menu()
        self._refresh_block_list()
        if self._session.n_blocks > 0:
            self._display_block(0)
            self._load_sidecar_calibration()
        else:
            self._plot_area.show_placeholder('В файле нет блоков.')
        self._update_title()
        return True

    def _on_open(self):
        if self._state != AppState.IDLE:
            return
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Открыть файл', '', FILE_FILTER)
        if path:
            self._load_session(path)

    def _on_import_pgc(self):
        if self._state != AppState.IDLE:
            return
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
        self._mark_modified()
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

    def _display_block(self, idx: int, *, fit: bool = True):
        if not (0 <= idx < self._session.n_blocks):
            return
        block = self._session.blocks[idx]
        self._current_block_idx = idx
        names  = [ch.name for ch in block.channels]
        units  = [ch.unit for ch in block.channels]
        self._plot_area.load_block(block, fit=fit)
        colors = self._plot_area.get_channel_colors()
        self._channel_names = names
        self._channel_units = units
        self._channel_panel.setup(names, colors)
        self._stats_panel.setup(names, colors)
        for i, unit in enumerate(units):
            self._plot_area.set_channel_meta(i, unit=unit)
            self._channel_panel.update_unit(i, unit)
        self._sync_channel_panel()
        self._act_follow.setEnabled(False)
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
        if 0 <= self._current_block_idx < len(offsets):
            b = self._session.blocks[self._current_block_idx]
            t0 = offsets[self._current_block_idx]
            self._nav_bar.highlight_block(t0, t0 + b.duration)

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
        if self._state == AppState.RECORDING:
            self._nav_bar.update_overview(times, values)

    def _on_view_range_changed(self, t0: float, t1: float):
        """Трансляция диапазона вида PlotArea в координаты NavBar."""
        if self._state == AppState.RECORDING:
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
        """Навигация из NavBar: блок выбирается по центру окна, масштаб не сбрасывается."""
        if self._state == AppState.RECORDING:
            self._plot_area.set_view_range(global_t0, global_t1)
            return
        if not self._block_global_offsets:
            self._plot_area.set_view_range(global_t0, global_t1)
            return

        blocks  = self._session.blocks
        offsets = self._block_global_offsets
        center  = (global_t0 + global_t1) / 2.0
        target  = len(offsets) - 1
        for i in range(len(offsets) - 1):
            if center < offsets[i + 1]:
                target = i
                break

        if target != self._current_block_idx:
            self._display_block(target, fit=False)

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
        """Показать весь активный блок: 1-2-5 совпадает с окном."""
        if not (0 <= self._current_block_idx < self._session.n_blocks):
            return
        b = self._session.blocks[self._current_block_idx]
        self._plot_area.fit_to_span(b.t_start, b.t_end, anchor='start')

    def _on_prev_block(self):
        if self._current_block_idx > 0:
            self._display_block(self._current_block_idx - 1)

    def _on_next_block(self):
        if self._current_block_idx < self._session.n_blocks - 1:
            self._display_block(self._current_block_idx + 1)

    def _on_block_row_changed(self, row: int):
        if row >= 0 and row != self._current_block_idx and self._state == AppState.IDLE:
            self._display_block(row)

    def _on_block_menu(self, pos):
        item = self._block_list.itemAt(pos)
        if item is None:
            return
        idx = item.data(Qt.UserRole)
        menu = QMenu(self)
        menu.addAction('Описание…', lambda: self._edit_block_description(idx))
        menu.addAction('Удалить', lambda: self._delete_block(idx))
        menu.addAction('Экспорт CSV…', self._on_export_csv)
        menu.exec(self._block_list.mapToGlobal(pos))

    def _edit_block_description(self, idx: int):
        if not (0 <= idx < self._session.n_blocks):
            return
        block = self._session.blocks[idx]
        text, ok = QInputDialog.getText(self, 'Описание блока', 'Текст:', text=block.description)
        if ok:
            block.description = text
            self._mark_modified()
            self._refresh_block_list()

    def _delete_block(self, idx: int):
        if not (0 <= idx < self._session.n_blocks):
            return
        answer = QMessageBox.question(
            self, 'Удалить блок',
            f'Удалить блок {idx + 1}?',
            QMessageBox.Yes | QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._session.remove_block(idx)
        self._mark_modified()
        if self._session.n_blocks == 0:
            self._current_block_idx = -1
            self._plot_area.clear_everything()
            self._plot_area.show_placeholder('Нет данных.')
            self._refresh_block_list()
            return
        self._display_block(min(idx, self._session.n_blocks - 1))

    def _refresh_block_list(self):
        from PySide6.QtCore import QLocale
        loc = QLocale()
        self._block_list.blockSignals(True)
        self._block_list.clear()
        for b in self._session.blocks:
            dt = b.start_datetime().strftime('%H:%M:%S')
            samples = loc.toString(int(b.n_samples))
            dur = loc.toString(float(b.duration), 'f', 2)
            label = (f'{tr("block_label")} {b.index + 1}  [{dt}]  '
                     f'{dur} с  {samples} отсч.  {b.n_channels} кан.')
            if b.description:
                label += f'  {b.description}'
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, b.index)
            item.setToolTip(b.description or b.source_name)
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
        y1 = self._plot_area.get_values_at(t1, physical=True)
        y2 = self._plot_area.get_values_at(t2, physical=True)
        dt = abs(t2 - t1)
        parts = [f'M1={fmt_span(t1)}  M2={fmt_span(t2)}  ΔT={fmt_span(dt)}']
        if dt > 1e-12:
            parts.append(f'1/ΔT={1.0 / dt:.4g} Гц')
        idx = self._plot_area.active_channel
        if y1 is not None and y2 is not None and idx < len(y1):
            dy = float(y1[idx] - y2[idx])
            states = self._plot_area.channel_states()
            unit = states[idx].unit if idx < len(states) else ''
            parts.append(f'ΔY={dy:+.4g}' + (f' {unit}' if unit else ''))
            tips = []
            for i, (a, b) in enumerate(zip(y1, y2)):
                name = states[i].name if i < len(states) else f'CH{i + 1}'
                tips.append(f'{name}: {float(a - b):+.4g}')
            self._lbl_markers.setToolTip('\n'.join(tips))
        self._lbl_markers.setText('  '.join(parts))

    # ==================================================================
    # Шкала амплитуды: курсор и активный канал
    # ==================================================================

    def _on_cursor_moved(self, x: float, y_vals):
        import math
        if math.isnan(x):
            self._channel_panel.clear_values()
            self._lbl_coords.setText('')
        else:
            self._channel_panel.update_values(y_vals)
            self._lbl_coords.setText(f'X = {fmt_span(x)}')

    def _on_channel_activated(self, idx: int):
        self._plot_area.set_active_channel(idx)
        self._channel_panel.set_active(idx)

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
            states = self._plot_area.channel_states()
            if idx >= len(states):
                return
            name, unit = states[idx].name, states[idx].unit
            coeff, offset = states[idx].calib_a, states[idx].calib_b

        dlg = ChannelCalibDialog(idx, name, unit, coeff, offset, parent=self)
        if dlg.exec() != ChannelCalibDialog.Accepted:
            return

        new_name = dlg.get_name()
        if not new_name:
            QMessageBox.warning(self, 'Калибровка', 'Имя канала не может быть пустым.')
            return
        new_unit   = dlg.get_unit()
        new_coeff  = dlg.get_coeff()
        new_offset = dlg.get_offset()

        self._plot_area.set_channel_calib(idx, new_coeff, new_offset)
        self._plot_area.set_channel_meta(idx, name=new_name, unit=new_unit)
        self._channel_panel.update_unit(idx, new_unit)
        self._channel_panel.update_name(idx, new_name)

        if block is not None and idx < len(block.channels):
            block.channels[idx].name   = new_name
            block.channels[idx].unit   = new_unit
            block.channels[idx].scale  = new_coeff
            block.channels[idx].offset = new_offset
            self._mark_modified()

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
        for i, ch in enumerate(block.channels):
            self._plot_area.set_channel_meta(i, name=ch.name, unit=ch.unit)
            self._channel_panel.update_unit(i, ch.unit)
            self._channel_panel.update_name(i, ch.name)

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
        if not self._confirm_discard():
            return
        self._reset_ui()

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

    def _on_stats_toggle(self, checked: bool):
        self._dock_stats.setVisible(checked)

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
        elif st == SourceType.FG_NET:
            self._configure_fgnet()

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
                self._stats_panel.setup(names, colors)
                self._nav_bar.setup_channels(self._gen_config.n_channels, colors)
                self._sync_channel_panel()

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

    def _configure_fgnet(self):
        if self._state != AppState.IDLE:
            return
        dlg = FgNetConfigDialog(self._fgnet_config, parent=self)
        if dlg.exec():
            self._fgnet_config = dlg.get_config()

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
        losses = ''
        src = self._source
        if src is not None:
            st = getattr(src, 'stats', None)
            if callable(st):
                st = st()
            if isinstance(st, dict) and st.get('pkt_lost'):
                losses = f'  потерь {st["pkt_lost"]}'
        self._lbl_rate.setText(f'{self._samples_win} отсч/с{losses}')
        self._lbl_rate.setToolTip(f'пакетов интерфейса: {self._pkt_count}')
        self._pkt_count = 0
        self._samples_win = 0

    def _mark_modified(self):
        self._session.modified = True
        self.setWindowModified(True)
        self._update_title()

    def _update_title(self):
        fp   = self._session.file_path
        name = Path(fp).name if fp else 'новый файл'
        mark = ' *' if self._session.modified else ''
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION_FULL} — {name}{mark}")
        self.setWindowModified(bool(self._session.modified))

    def _load_sidecar_calibration(self):
        self._load_calibration_file()

    def _update_recent_menu(self):
        self._menu_recent.clear()
        files: list = config.get('recent_files', default=[])
        if not files:
            self._menu_recent.addAction('(пусто)').setEnabled(False)
        for path in files:
            self._menu_recent.addAction(path, lambda p=path: self._load_recent(p))

    def _load_recent(self, path: str):
        if self._state != AppState.IDLE:
            return
        if not self._confirm_discard():
            return
        self._load_session(path)

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
        """Статистика по физическим отсчётам, без визуальных scale/offset."""
        _t, phys = self._plot_area.get_range_data(t0, t1)
        if phys is None or len(phys) == 0:
            self._stats_panel.clear()
            return
        states = self._plot_area.channel_states()
        channel_data = []
        units = []
        visible = []
        for i in range(phys.shape[1]):
            channel_data.append(phys[:, i])
            if i < len(states):
                units.append(states[i].unit)
                visible.append(states[i].visible)
            else:
                units.append('')
                visible.append(True)
        self._stats_panel.update_stats(channel_data, units, visible)

    def _on_selection_action(self, action: str, t0: float, t1: float):
        if action == 'copy':
            self._copy_selection_to_block(t0, t1)
        elif action == 'delete':
            self._delete_selection_from_block(t0, t1)

    def _copy_selection_to_block(self, t0: float, t1: float):
        block = self._current_block()
        if block is None:
            return
        t = block.times
        i0, i1 = index_range(t, t0, t1)
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
        self._mark_modified()
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
        t = block.times
        i0, i1 = index_range(t, t0, t1)
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
        self._mark_modified()

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
        if self._state != AppState.IDLE:
            return None
        if 0 <= self._current_block_idx < self._session.n_blocks:
            return self._session.blocks[self._current_block_idx]
        return None

    def _current_view_range(self) -> tuple[float, float] | None:
        return self._plot_area.view_range()

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

    def _export_png_to(self, path: str):
        import pyqtgraph.exporters
        exporter = pyqtgraph.exporters.ImageExporter(self._plot_area.plot_item())
        exporter.parameters()['width'] = 1920
        exporter.export(path)

    def _on_export_png(self):
        path, _ = QFileDialog.getSaveFileName(
            self, 'Сохранить график как PNG',
            'flowergraph_plot.png',
            'PNG изображение (*.png);;Все файлы (*)'
        )
        if not path:
            return
        try:
            self._export_png_to(path)
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
        QMessageBox.information(
            self, tr('language'),
            'Язык будет применён после перезапуска FlowerGraph.',
        )

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
            self._mark_modified()
        coeffs  = [e['coeff']  for e in entries]
        offsets = [e['offset'] for e in entries]
        self._plot_area.set_all_calibrations(coeffs, offsets)
        if block:
            for i, e in enumerate(entries):
                if i >= len(block.channels):
                    break
                self._plot_area.set_channel_meta(i, name=e['name'], unit=e['unit'])
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
            f'<b>{APP_NAME}</b> v{APP_VERSION_FULL}<br>'
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

    def keyPressEvent(self, event):
        super().keyPressEvent(event)

    def _show_error_log(self):
        text = '\n'.join(self._error_log) or 'Ошибок не было.'
        QMessageBox.information(self, 'Журнал источника', text)

    def closeEvent(self, event):
        if self._state != AppState.IDLE and not self._quit_from_tray:
            if getattr(self, '_tray', None) is not None and self._tray.isVisible():
                answer = QMessageBox.question(
                    self, 'Запись',
                    'Идёт запись. Свернуть в трей и продолжить?\n'
                    '«Нет» — остановить и спросить про сохранение.\n'
                    '«Отмена» — вернуться в окно.',
                    QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                )
                if answer == QMessageBox.Cancel:
                    event.ignore()
                    return
                if answer == QMessageBox.Yes:
                    event.ignore()
                    self.hide()
                    self._tray.showMessage(APP_NAME, 'Запись продолжается в трее.')
                    return
            else:
                answer = QMessageBox.question(
                    self, 'Запись',
                    'Идёт запись. Остановить и выйти?',
                    QMessageBox.Yes | QMessageBox.Cancel,
                )
                if answer != QMessageBox.Yes:
                    event.ignore()
                    return
            self._on_stop()
        if self._session.modified:
            answer = QMessageBox.question(
                self, 'Несохранённые данные',
                'Сохранить изменения перед выходом?',
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if answer == QMessageBox.Cancel:
                event.ignore()
                return
            if answer == QMessageBox.Yes and not self._on_save():
                event.ignore()
                return
        config.set('window_geometry_b64',
                   value=bytes(self.saveGeometry().toBase64()).decode('ascii'))
        config.set('window_state_b64',
                   value=bytes(self.saveState().toBase64()).decode('ascii'))
        config.set('window', 'maximized', value=self.isMaximized())
        config.set('source_type', value=self._source_type.value)
        config.set('source_ascii', value=self._com_config.to_dict())
        config.set('source_cobs', value=self._cobs_config.to_dict())
        config.set('source_mcobs', value=self._mcobs_config.to_dict())
        config.set('source_fgnet', value=self._fgnet_config.to_dict())
        config.set('source_generator', value=self._gen_config.to_dict())
        config.save()
        if getattr(self, '_tray', None) is not None:
            self._tray.hide()
        super().closeEvent(event)


def _sep() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.VLine)
    line.setFrameShadow(QFrame.Sunken)
    return line
