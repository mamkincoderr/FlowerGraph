"""Иконка приложения: PyInstaller кладёт assets в _MEIPASS, в dev — рядом с репо."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtCore import QSize, Qt


def _roots() -> list[Path]:
    roots: list[Path] = []
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        roots.append(Path(meipass))
    roots.append(Path(__file__).resolve().parent.parent)
    return roots


def icon_path(*names: str) -> Path | None:
    if not names:
        names = ('icon.ico', 'icon.png')
    for root in _roots():
        for name in names:
            path = root / 'assets' / name
            if path.is_file():
                return path
    return None


def app_icon() -> QIcon:
    icon = QIcon()
    png = icon_path('icon.png')
    ico = icon_path('icon.ico')
    if png:
        pix = QPixmap(str(png))
        if not pix.isNull():
            for size in (16, 24, 32, 48, 64, 128, 256):
                icon.addPixmap(pix.scaled(
                    QSize(size, size),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                ))
    if ico:
        icon.addFile(str(ico))
    return icon
