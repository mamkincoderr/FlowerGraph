import sys
import os
import atexit
import logging
import traceback
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtWidgets import QApplication, QSplashScreen
from PySide6.QtGui import QPixmap, QPalette, QColor
from PySide6.QtCore import Qt, QTimer
from core.app_icon import app_icon
from ui.main_window import MainWindow, APP_NAME, APP_VERSION

pg.setConfigOption('background', 'w')
pg.setConfigOption('foreground', 'k')
pg.setConfigOption('antialias', True)

_SPLASH_MS = 300

# В PyInstaller-сборке ресурсы лежат в sys._MEIPASS, при обычном запуске — рядом с main.py
if getattr(sys, 'frozen', False):
    _BASE_DIR = Path(sys._MEIPASS)
else:
    _BASE_DIR = Path(__file__).parent

_SPLASH_FILE = _BASE_DIR / 'assets' / 'Splash.png'


def _log_path() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent / 'flowergraph.log'
    return Path(__file__).parent / 'flowergraph.log'


class _NullStream:
    def write(self, data: str):
        return 0

    def flush(self):
        pass

    def isatty(self):
        return False


class _Tee:
    """Пишет в оригинальный поток (если есть) и в общий лог-файл."""

    def __init__(self, original, log_file):
        self._orig = original if original is not None else _NullStream()
        self._file = log_file

    def write(self, data: str):
        try:
            self._orig.write(data)
        except Exception:
            pass
        try:
            self._file.write(data)
        except Exception:
            pass

    def flush(self):
        try:
            self._orig.flush()
        except Exception:
            pass
        try:
            self._file.flush()
        except Exception:
            pass

    def isatty(self):
        return False


_LOG_MAX_LINES = 2000   # максимум строк в лог-файле между сессиями


def _setup_logging():
    from datetime import datetime

    log_file_path = _log_path()
    if log_file_path.exists():
        try:
            lines = log_file_path.read_text(encoding='utf-8', errors='replace').splitlines()
            if len(lines) > _LOG_MAX_LINES:
                log_file_path.write_text(
                    '\n'.join(lines[-_LOG_MAX_LINES:]) + '\n',
                    encoding='utf-8',
                )
        except Exception:
            pass

    log_fh = open(log_file_path, 'a', encoding='utf-8', buffering=1)
    atexit.register(log_fh.close)
    sep = '=' * 72
    log_fh.write(f'\n{sep}\n')
    log_fh.write(f'  FlowerGraph  {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n')
    log_fh.write(f'{sep}\n')

    sys.stdout = _Tee(sys.stdout, log_fh)
    sys.stderr = _Tee(sys.stderr, log_fh)

    def _exc_hook(exc_type, exc_value, exc_tb):
        msg = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
        sys.stderr.write(f'[UNHANDLED EXCEPTION]\n{msg}')
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _exc_hook

    logging.basicConfig(
        stream=log_fh,
        level=logging.WARNING,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )


# ---------------------------------------------------------------------------

def main():
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                f'FlowerGraph.Desktop.{APP_VERSION}'
            )
        except Exception:
            pass

    _setup_logging()

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    use_gl = '--opengl' in sys.argv
    try:
        from core.config import config
        use_gl = use_gl or bool(config.get('display', 'use_opengl', default=False))
    except Exception:
        pass
    if use_gl:
        try:
            pg.setConfigOption('useOpenGL', True)
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor('#f3f3f3'))
    palette.setColor(QPalette.ColorRole.WindowText, QColor('#202020'))
    palette.setColor(QPalette.ColorRole.Base, QColor('#ffffff'))
    palette.setColor(QPalette.ColorRole.Text, QColor('#202020'))
    palette.setColor(QPalette.ColorRole.Button, QColor('#e6e6e6'))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor('#202020'))
    palette.setColor(QPalette.ColorRole.Highlight, QColor('#0070c0'))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor('#ffffff'))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor('#ffffff'))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor('#202020'))
    app.setPalette(palette)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName('FlowerGraph')
    icon = app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)

    # Сплеш-экран
    splash = None
    if _SPLASH_FILE.exists():
        pixmap = QPixmap(str(_SPLASH_FILE))
        if not pixmap.isNull():
            pixmap = pixmap.scaled(
                pixmap.width() // 2, pixmap.height() // 2,
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
            splash = QSplashScreen(pixmap)
            splash.setWindowFlag(Qt.FramelessWindowHint)
            splash.show()
            app.processEvents()

    window = MainWindow()
    window.show()

    if splash:
        QTimer.singleShot(_SPLASH_MS, lambda: splash.finish(window))

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
