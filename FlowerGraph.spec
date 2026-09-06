# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec для FlowerGraph.
Сборка: cd FlowerGraph && .venv\Scripts\pyinstaller FlowerGraph.spec --clean
"""

import os as _os
from pathlib import Path as _Path

from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

# Номер сборки (4-я компонента версии). build_number.txt в .gitignore — этот
# блок создаёт/обновляет его при каждой сборке и кладёт в дистрибутив (datas).
#   CI (GitHub Actions): берём GITHUB_RUN_NUMBER (или FG_BUILD_NUMBER) как есть
#   локально:            инкремент предыдущего значения
_bn_path = _Path('build_number.txt')
_env_bn = (_os.environ.get('FG_BUILD_NUMBER')
           or _os.environ.get('GITHUB_RUN_NUMBER') or '').strip()
if _env_bn:
    _bn_path.write_text(_env_bn)
    print(f'[spec] build_number.txt <- FG_BUILD_NUMBER {_env_bn}')
else:
    try:
        _bn = int((_bn_path.read_text().strip() or '0')) + 1
    except (OSError, ValueError):
        _bn = 1
    _bn_path.write_text(str(_bn))
    print(f'[spec] build_number.txt -> {_bn}')

# pyqtgraph: цветовые карты и шаблоны UI
pyqtgraph_datas = collect_data_files('pyqtgraph')

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=pyqtgraph_datas + [('assets', 'assets'), ('build_number.txt', '.')],
    hiddenimports=[
        'serial',
        'serial.tools.list_ports',
        'pyqtgraph.exporters',
        # наши пакеты
        'core.config',
        'core.ring_buffer',
        'core.session',
        'core.file_io',
        'core.app_icon',
        'ui.main_window',
        'ui.plot_area',
        'ui.channel_panel',
        'ui.nav_bar',
        'ui.amplitude_scale',
        'ui.com_ascii_dialog',
        'ui.export_dialog',
        'ui.calib_dialog',
        'ui.stats_panel',
        'core.calib_file',
        'plugins.base_source',
        'plugins.cobs_codec',
        'plugins.virtual_generator',
        'plugins.com_ascii_source',
        'plugins.com_cobs_source',
        'plugins.com_mcobs_source',
        'plugins.fg_net_source',
        'plugins.pg_import',
        'plugins.pg_export',
        'core.i18n',
    ],
    excludes=[
        # Тяжёлые Qt-модули, не используемые в приложении
        'PySide6.QtWebEngineCore',
        'PySide6.QtWebEngineWidgets',
        'PySide6.Qt3DCore',
        'PySide6.Qt3DRender',
        'PySide6.QtDataVisualization',
        'PySide6.QtCharts',
        'PySide6.QtMultimedia',
        'PySide6.QtMultimediaWidgets',
        'PySide6.QtPositioning',
        'PySide6.QtLocation',
        'PySide6.QtNfc',
        'PySide6.QtBluetooth',
        'PySide6.QtRemoteObjects',
        'PySide6.QtSql',
        'PySide6.QtXml',
        'tkinter',
        'matplotlib',
        'scipy',
        'cobs',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='FlowerGraph',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='FlowerGraph',
)
