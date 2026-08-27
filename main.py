import sys
import logging
import traceback
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtWidgets import QApplication, QSplashScreen
from PySide6.QtGui import QPixmap
from PySide6.QtCore import Qt, QTimer
from core.app_icon import app_icon
from ui.main_window import MainWindow, APP_NAME

pg.setConfigOption('background', 'w')
pg.setConfigOption('foreground', 'k')
pg.setConfigOption('antialias', True)

try:
    pg.setConfigOption('useOpenGL', True)
except Exception:
    pass

_SPLASH_MS = 1500

# В PyInstaller-сборке ресурсы лежат в sys._MEIPASS, при обычном запуске — рядом с main.py
if getattr(sys, 'frozen', False):
    _BASE_DIR = Path(sys._MEIPASS)
else:
    _BASE_DIR = Path(__file__).parent

_SPLASH_FILE = _BASE_DIR / 'assets' / 'Splash.png'
_LOG_FILE    = Path(__file__).parent / 'flowergraph.log'


# ---------------------------------------------------------------------------
# Tee: дублирует stdout/stderr в лог-файл
# ---------------------------------------------------------------------------

class _Tee:
    """Перехватчик потока: пишет в оригинальный поток И в файл."""

    def __init__(self, original, log_path: Path):
        self._orig = original
        self._file = open(log_path, 'a', encoding='utf-8', buffering=1)

    def write(self, data: str):
        self._orig.write(data)
        try:
            self._file.write(data)
        except Exception:
            pass

    def flush(self):
        self._orig.flush()
        try:
            self._file.flush()
        except Exception:
            pass

    def isatty(self):
        return False


_LOG_MAX_LINES = 2000   # максимум строк в лог-файле между сессиями


def _setup_logging():
    """Перенаправить stdout/stderr в flowergraph.log + консоль."""
    from datetime import datetime

    # Обрезать лог до _LOG_MAX_LINES строк (чтобы не рос бесконечно)
    if _LOG_FILE.exists():
        try:
            lines = _LOG_FILE.read_text(encoding='utf-8', errors='replace').splitlines()
            if len(lines) > _LOG_MAX_LINES:
                _LOG_FILE.write_text(
                    '\n'.join(lines[-_LOG_MAX_LINES:]) + '\n',
                    encoding='utf-8',
                )
        except Exception:
            pass

    # Записать разделитель сессии
    sep = '=' * 72
    with open(_LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(f'\n{sep}\n')
        f.write(f'  FlowerGraph  {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n')
        f.write(f'{sep}\n')

    sys.stdout = _Tee(sys.stdout, _LOG_FILE)
    sys.stderr = _Tee(sys.stderr, _LOG_FILE)

    # Глобальный перехватчик необработанных исключений
    def _exc_hook(exc_type, exc_value, exc_tb):
        msg = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
        sys.stderr.write(f'[UNHANDLED EXCEPTION]\n{msg}')
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _exc_hook

    # Направить стандартный logging тоже в файл
    logging.basicConfig(
        filename=str(_LOG_FILE),
        level=logging.WARNING,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        encoding='utf-8',
    )


# ---------------------------------------------------------------------------

def main():
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                'FlowerGraph.Desktop.0.6'
            )
        except Exception:
            pass

    _setup_logging()

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
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
            splash = QSplashScreen(pixmap, Qt.WindowStaysOnTopHint)
            splash.setWindowFlag(Qt.FramelessWindowHint)
            splash.show()
            app.processEvents()

    window = MainWindow()

    def _finish():
        window.show()
        if splash:
            splash.finish(window)

    if splash:
        QTimer.singleShot(_SPLASH_MS, _finish)
    else:
        window.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
