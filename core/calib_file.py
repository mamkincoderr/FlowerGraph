"""
Сохранение / загрузка калибровки каналов в текстовый файл (.cal).

Формат — INI-подобный текст (UTF-8):
  [Channel_0]
  name = CH1
  unit = mV
  coeff = 0.1
  offset = 0.0

  [Channel_1]
  ...
"""

import configparser
from pathlib import Path


def save_calibration(path: str | Path,
                     names:   list[str],
                     units:   list[str],
                     coeffs:  list[float],
                     offsets: list[float]) -> None:
    """Сохранить калибровку в текстовый .cal файл."""
    path = Path(path)
    cfg = configparser.ConfigParser()
    for i in range(len(names)):
        sec = f'Channel_{i}'
        cfg[sec] = {
            'name':   names[i],
            'unit':   units[i],
            'coeff':  str(coeffs[i]),
            'offset': str(offsets[i]),
        }
    with open(path, 'w', encoding='utf-8') as f:
        f.write('# FlowerGraph calibration file\n')
        cfg.write(f)


def load_calibration(path: str | Path) -> list[dict]:
    """
    Загрузить калибровку из .cal файла.
    Возвращает список dict с ключами: name, unit, coeff, offset.
    """
    path = Path(path)
    if not path.exists():
        return []
    cfg = configparser.ConfigParser()
    try:
        cfg.read(path, encoding='utf-8')
    except Exception:
        return []

    result = []
    i = 0
    while True:
        sec = f'Channel_{i}'
        if not cfg.has_section(sec):
            break
        s = cfg[sec]
        result.append({
            'name':   s.get('name', f'CH{i+1}'),
            'unit':   s.get('unit', ''),
            'coeff':  _float(s.get('coeff', '1.0'), 1.0),
            'offset': _float(s.get('offset', '0.0'), 0.0),
        })
        i += 1
    return result


def cal_path_for(fgd_path: str | Path) -> Path:
    """Путь к .cal файлу рядом с .fgd файлом."""
    return Path(fgd_path).with_suffix('.cal')


def _float(s: str, default: float) -> float:
    try:
        return float(s)
    except (ValueError, TypeError):
        return default
