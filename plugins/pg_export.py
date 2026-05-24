"""Экспорт FlowerGraph Block в файл PowerGraph (.pgc).

Формат PGC v3.3 (на основе анализа файлов PowerGraph):
  Header  : 137 байт (0x89)
  PGGr    : 32 слота × 32 байта
  ADCh    : 32 слота × 72 байта
  ADBl    : 93 байта (включая имя источника, n_active, scale_per_div)
  data    : 8 байт заголовка + n_active×n_samples×2 байт данных (int16)
"""
import struct
from pathlib import Path
import numpy as np
from core.session import Block

_MAGIC = b'PGC\x00'
_N_SLOTS     = 32   # фиксированное число слотов PGGr/ADCh
_PGGR_SZ     = 32   # байт на слот PGGr
_ADCH_SZ     = 72   # байт на слот ADCh
_HDR_SIZE    = 0x89  # 137 байт до первого PGGr-слота
_PGGR_BASE   = 0x89
_ADCH_BASE   = _PGGR_BASE + _N_SLOTS * _PGGR_SZ   # 0x0489
_ADBL_BASE   = _ADCH_BASE + _N_SLOTS * _ADCH_SZ   # 0x0D89
_ADBL_SIZE   = 93    # фиксированный размер ADBl секции
_DEFAULT_SCALE_PER_DIV = 20.0  # масштаб по умолчанию (ед./дел.)
_TIME_WINDOW = 100.0            # окно времени по умолчанию (сек)


def save(block: Block, path: str | Path) -> None:
    path = Path(path)
    if path.suffix.lower() != '.pgc':
        path = path.with_suffix('.pgc')

    n_ch      = block.n_channels
    n_samples = block.n_samples
    sr        = float(block.sample_rate)
    src_name  = block.source_name[:63].encode('ascii', errors='replace')

    buf = bytearray()

    # -------------------------------------------------------------------------
    # Заголовок (137 байт = 0x89)
    # -------------------------------------------------------------------------
    buf += _MAGIC                               # [0:4]
    buf += struct.pack('<HH', 3, 3)             # [4:8]  версия 3.3
    buf += struct.pack('<H', n_ch)              # [8:10] n_channels_configured
    buf += struct.pack('<H', _N_SLOTS)          # [10:12] n_slots = 32
    buf += struct.pack('<H', n_ch)              # [12:14] n_active
    buf += struct.pack('<HHH', 1, 1, 0)         # [14:20] reserved
    buf += struct.pack('<I', n_samples)         # [20:24] ring_buffer_depth  (0x14)
    buf += struct.pack('<I', n_samples)         # [24:28] total_samples       (0x18)
    buf += struct.pack('<I', 0)                 # [28:32] ring_start_idx      (0x1C)
    buf += struct.pack('<I', 0)                 # [32:36] reserved             (0x20)
    buf += struct.pack('<f', _TIME_WINDOW)      # [36:40] time_window_sec     (0x24)
    buf += struct.pack('<H', len(src_name))     # [40:42] длина имени источника (0x28)
    buf += src_name                             # [42:42+len]

    # Дополнить до смещения 0x40
    while len(buf) < 0x40:
        buf += b'\x00'

    # Карта каналов: 36 × uint16 big-endian (0, 1, 2, …, 31, 0, 0, 0, 0)
    for i in range(36):
        buf += struct.pack('>H', i if i < _N_SLOTS else 0)

    # Один байт до 0x89
    buf += b'\x00'

    assert len(buf) == _HDR_SIZE, f'Неверный размер заголовка: {len(buf)}'

    # -------------------------------------------------------------------------
    # PGGr-секция: 32 слота × 32 байта
    # -------------------------------------------------------------------------
    raw_ranges = []
    for c in range(min(n_ch, _N_SLOTS)):
        col = block.values[:, c]
        raw_min = int(np.clip(float(np.min(col)), -32767, 32767))
        raw_max = int(np.clip(float(np.max(col)), -32767, 32767))
        raw_ranges.append((raw_min, raw_max))

    for slot in range(_N_SLOTS):
        sb = bytearray(_PGGR_SZ)
        struct.pack_into('4s', sb, 0, b'PGGr')
        struct.pack_into('<h', sb, 4, slot)
        struct.pack_into('<H', sb, 6, 6)                          # source_type = COM-ASCII
        if slot < n_ch:
            rmin, rmax = raw_ranges[slot]
            struct.pack_into('<i', sb, 8,  rmax)
            struct.pack_into('<i', sb, 12, rmin)
            struct.pack_into('<f', sb, 16, _DEFAULT_SCALE_PER_DIV)
        else:
            struct.pack_into('<f', sb, 16, _DEFAULT_SCALE_PER_DIV)
        buf += bytes(sb)

    assert len(buf) == _ADCH_BASE

    # -------------------------------------------------------------------------
    # ADCh-секция: 32 слота × 72 байта
    # -------------------------------------------------------------------------
    for slot in range(_N_SLOTS):
        ab = bytearray(_ADCH_SZ)
        struct.pack_into('4s', ab, 0, b'ADCh')
        struct.pack_into('<h', ab, 4, slot)
        struct.pack_into('<H', ab, 6, 0x0001)         # flags = enabled
        struct.pack_into('<i', ab, 8, 6)              # mode = COM-ASCII
        struct.pack_into('<i', ab, 12, 0)
        if slot < n_ch:
            ch = block.channels[slot]
            A  = float(ch.scale)  if ch.scale  else 1.0
            B  = float(ch.offset) if ch.offset else 0.0
            struct.pack_into('<f', ab, 16, A)
            struct.pack_into('<f', ab, 20, B)
        else:
            struct.pack_into('<f', ab, 16, 1.0)
            struct.pack_into('<f', ab, 20, 0.0)
        struct.pack_into('<f', ab, 24, 1.0)
        struct.pack_into('<f', ab, 28, -1.0)
        struct.pack_into('<H', ab, 32, 0)             # unit_id
        if slot < n_ch:
            name_b = block.channels[slot].name[:37].encode('ascii', errors='replace')
            ab[34:34 + len(name_b)] = name_b
        buf += bytes(ab)

    assert len(buf) == _ADBL_BASE

    # -------------------------------------------------------------------------
    # ADBl: 93 байта (фиксированный размер, как в оригинальном PGC)
    # -------------------------------------------------------------------------
    adbl = bytearray(_ADBL_SIZE)
    struct.pack_into('4s', adbl, 0,  b'ADBl')
    struct.pack_into('<I', adbl, 4,  n_samples)        # ring_depth
    struct.pack_into('<f', adbl, 8,  sr)               # sample_rate
    struct.pack_into('<I', adbl, 12, 16)               # source_name buffer size
    src_name_16 = src_name[:16]
    adbl[16:16 + len(src_name_16)] = src_name_16       # source_name padded
    # [32..51] = zeros (timestamps / reserved)
    struct.pack_into('>H', adbl, 48, n_ch)             # n_active as BE uint16 (matches PGC format)
    # channel indices as big-endian uint16, starting at +52
    for i in range(min(n_ch, 4)):
        struct.pack_into('>H', adbl, 52 + i * 2, i)
    # scale_per_div per channel (zeros — PG uses PGGr for display scale)
    # [60..79]: reserved (zeros)
    # [80..92] = zeros
    buf += bytes(adbl)

    assert len(buf) == _ADBL_BASE + _ADBL_SIZE

    # -------------------------------------------------------------------------
    # data: int16 interleaved (только активные каналы = 0..n_ch-1)
    # -------------------------------------------------------------------------
    data_bytes = block.values[:, :n_ch].astype('<i2').tobytes()
    data_hdr = bytearray(8)
    struct.pack_into('4s', data_hdr, 0, b'data')
    struct.pack_into('<I', data_hdr, 4, len(data_bytes))
    buf += bytes(data_hdr)
    buf += data_bytes

    path.write_bytes(bytes(buf))
