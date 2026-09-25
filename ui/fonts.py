"""Шрифты от системного размера, не от пикселей."""

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication


def ui_font(delta: float = 0.0) -> QFont:
    base = QFont(QApplication.font())
    pt = base.pointSizeF()
    if pt <= 0:
        pt = 10.0
    base.setPointSizeF(max(8.0, pt + delta))
    return base


def mono_font(delta: float = 0.0) -> QFont:
    f = QFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    pt = QApplication.font().pointSizeF()
    if pt <= 0:
        pt = 10.0
    f.setPointSizeF(max(8.0, pt + delta))
    return f
