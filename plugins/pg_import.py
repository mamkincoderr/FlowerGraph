"""
Импорт файлов PowerGraph (.pgc) версии 3.x в FlowerGraph Block.

Формат .pgc — бинарный little-endian:
  0x0000  "PGC\0" + uint16 ver_major + uint16 ver_minor
  0x0008  uint16 n_channels_configured
  0x000A  uint16 n_slots (32)
  0x000C  uint16 n_channels_active
  0x0014  uint32 ring_buffer_depth  (сэмплов на канал в буфере)
  0x0018  uint32 total_samples_recorded
  0x0024  float32 time_window_sec (100.0 = 100 s окно)
  0x0028  uint16 source_name_len
  0x002A  char[] source_name (ASCII)
  -----
  Секция PGGr: 32 × 32 байта, начало = 0x0088 + 1
    [+4]  int16  channel_index
    [+6]  int16  source_type   (6 = COM-ASCII)
    [+12] int32  last_value    (последнее значение в физ. единицах)
    [+16] float32 sample_rate  (Гц)
  -----
  Секция ADCh: 32 × 72 байта
    [+4]  int16  channel_index
    [+6]  uint16 flags  (0x0001 = enabled)
    [+16] float32 vertical_scale   (default 1.0)
    [+20] float32 vertical_offset  (default 0.0)
    [+24] float32 range_max  (+1.0)
    [+28] float32 range_min  (-1.0)
  -----
  Блок ADBl:
    [+0]  "ADBl"
    [+4]  uint32 ring_buffer_depth
    [+8]  float32 sample_rate_hz   (например 1282.0)
  -----
  Тег "data":
    [+0]  "data"
    [+4]  uint32 data_byte_length
    [+8]  int16[] interleaved — [ch0,ch1,ch2,...] × n_samples
          Sentinel = 0x7F88 (32648) — нет данных для канала.
"""

import struct
import time as _time
from pathlib import Path

import numpy as np

from core.session import Block, ChannelInfo


_MAGIC        = b'PGC\x00'
_SIG_PGGR     = b'PGGr'
_SIG_ADCH     = b'ADCh'
_SIG_ADBL     = b'ADBl'
_SIG_DATA     = b'data'
_PGGR_N_SLOTS = 32
_ADCH_N_SLOTS = 32
_PGGR_STRIDE  = 32
_ADCH_STRIDE  = 72
_SENTINEL     = 32648   # 0x7F88 — насыщение / нет данных (хранится как есть)


def load(path: str | Path) -> Block:
    """Загружает .pgc файл, возвращает Block."""
    path = Path(path)
    data = path.read_bytes()

    if data[:4] != _MAGIC:
        raise ValueError(f'Не является PGC-файлом: {path.name!r}')

    # --- Глобальный заголовок ---
    ver_major, ver_minor = struct.unpack_from('<HH', data, 4)
    n_active      = struct.unpack_from('<H', data, 0x0C)[0]
    ring_depth    = struct.unpack_from('<I', data, 0x14)[0]
    src_name_len  = struct.unpack_from('<H', data, 0x28)[0]
    src_name      = data[0x2A : 0x2A + src_name_len].decode('ascii', errors='replace')

    # --- ADCh: масштаб/смещение для каждого активного канала ---
    adch_off = data.index(_SIG_ADCH)
    scales  = {}
    offsets = {}
    for slot in range(_ADCH_N_SLOTS):
        off = adch_off + slot * _ADCH_STRIDE
        if data[off : off + 4] != _SIG_ADCH:
            break
        ch_idx = struct.unpack_from('<h', data, off + 4)[0]
        scale  = struct.unpack_from('<f', data, off + 16)[0]
        offset = struct.unpack_from('<f', data, off + 20)[0]
        scales[ch_idx]  = scale if scale != 0.0 else 1.0
        offsets[ch_idx] = offset

    # --- ADBl: частота дискретизации ---
    adbl_off    = data.index(_SIG_ADBL)
    sample_rate_f = struct.unpack_from('<f', data, adbl_off + 8)[0]
    sample_rate = max(1, round(sample_rate_f))

    # --- data: сырые int16 сэмплы ---
    dtag_off  = data.index(_SIG_DATA)
    data_len  = struct.unpack_from('<I', data, dtag_off + 4)[0]
    data_start = dtag_off + 8

    n_samples = data_len // (n_active * 2)
    raw = np.frombuffer(
        data[data_start : data_start + n_samples * n_active * 2],
        dtype='<i2',
    ).reshape(n_samples, n_active)

    # --- Сборка каналов и значений ---
    values = np.empty((n_samples, n_active), dtype=np.float32)
    channels: list[ChannelInfo] = []
    for c in range(n_active):
        col = raw[:, c].astype(np.float32)
        s = scales.get(c, 1.0)
        o = offsets.get(c, 0.0)
        values[:, c] = col * s + o
        channels.append(ChannelInfo(name=f'CH{c + 1}'))

    dt    = 1.0 / sample_rate
    times = np.arange(n_samples, dtype=np.float64) * dt

    return Block(
        start_time  = _time.time(),
        source_name = f'PGC:{src_name}',
        sample_rate = sample_rate,
        channels    = channels,
        times       = times,
        values      = values,
        description = (
            f'Импорт PowerGraph v{ver_major}.{ver_minor} — {path.name}'
        ),
    )
