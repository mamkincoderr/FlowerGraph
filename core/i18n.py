"""Простая система локализации RU/EN."""

_LANG = 'ru'

_STRINGS = {
    'ru': {
        'app_name': 'FlowerGraph',
        'menu_file': 'Файл',
        'menu_edit': 'Правка',
        'menu_view': 'Вид',
        'menu_record': 'Запись',
        'menu_block': 'Блок',
        'menu_help': 'Справка',
        'menu_tools': 'Инструменты',
        'new': 'Новый',
        'open': 'Открыть…',
        'save': 'Сохранить',
        'save_as': 'Сохранить как…',
        'import': 'Импорт',
        'export': 'Экспорт',
        'recent': 'Последние файлы',
        'quit': 'Выход',
        'start': '▶ Старт',
        'stop': '■ Стоп',
        'mark': '◆ Метка',
        'follow': '⟳ Следить',
        'auto_y': '↕ Авто Y',
        'markers': 'M1|M2',
        'source': 'Источник',
        'no_source': 'Источник: нет',
        'errors': 'Ошибок',
        'recording': '⏺ ЗАПИСЬ',
        'stats_title': 'Статистика фрагмента',
        'channels_title': 'Каналы',
        'blocks_title': 'Блоки данных',
        'no_selection': 'Нет выделения',
        'block_label': 'Блок',
        'language': 'Язык',
        'lang_ru': 'Русский',
        'lang_en': 'English',
        'export_pgc': 'Экспорт в .pgc…',
        'export_csv': 'Данные блока в CSV/TXT…',
        'export_png': 'График в PNG…',
        'import_pgc': 'Файл .pgc…',
        'about': 'О программе',
        'settings': 'Настройки',
        'pkt_per_sec': 'пак/с',
    },
    'en': {
        'app_name': 'FlowerGraph',
        'menu_file': 'File',
        'menu_edit': 'Edit',
        'menu_view': 'View',
        'menu_record': 'Record',
        'menu_block': 'Block',
        'menu_help': 'Help',
        'menu_tools': 'Tools',
        'new': 'New',
        'open': 'Open…',
        'save': 'Save',
        'save_as': 'Save As…',
        'import': 'Import',
        'export': 'Export',
        'recent': 'Recent Files',
        'quit': 'Exit',
        'start': '▶ Start',
        'stop': '■ Stop',
        'mark': '◆ Mark',
        'follow': '⟳ Follow',
        'auto_y': '↕ Auto Y',
        'markers': 'M1|M2',
        'source': 'Source',
        'no_source': 'Source: none',
        'errors': 'Errors',
        'recording': '⏺ RECORDING',
        'stats_title': 'Fragment Statistics',
        'channels_title': 'Channels',
        'blocks_title': 'Data Blocks',
        'no_selection': 'No selection',
        'block_label': 'Block',
        'language': 'Language',
        'lang_ru': 'Russian',
        'lang_en': 'English',
        'export_pgc': 'Export to .pgc…',
        'export_csv': 'Block Data as CSV/TXT…',
        'export_png': 'Plot as PNG…',
        'import_pgc': '.pgc file…',
        'about': 'About',
        'settings': 'Settings',
        'pkt_per_sec': 'pkt/s',
    },
}


def tr(key: str) -> str:
    return _STRINGS.get(_LANG, _STRINGS['ru']).get(key, key)


def set_lang(lang: str):
    global _LANG
    if lang in _STRINGS:
        _LANG = lang


def get_lang() -> str:
    return _LANG


def available_langs() -> list[str]:
    return list(_STRINGS.keys())
