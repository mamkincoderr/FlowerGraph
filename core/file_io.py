"""
Формат .fgd — ZIP-архив (deflate) содержащий:
  _meta.json          — метаданные сессии
  b{i}_meta.json      — метаданные блока i
  b{i}_times.npy      — float64 временны́е метки блока i
  b{i}_values.npy     — float32 значения каналов блока i
"""

import io
import json
import os
import zipfile
from pathlib import Path

import numpy as np

from core.session import Session, Block, ChannelInfo, Annotation

FILE_EXT = '.fgd'
FORMAT_VERSION = 1


# ------------------------------------------------------------------
# Save
# ------------------------------------------------------------------

def save(session: Session, path: str | Path) -> None:
    path = Path(path)
    if path.suffix.lower() != FILE_EXT:
        path = path.with_suffix(FILE_EXT)

    meta = {
        'version':  FORMAT_VERSION,
        'created':  session.created,
        'n_blocks': session.n_blocks,
    }

    tmp = path.with_suffix(path.suffix + '.tmp')
    with zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('_meta.json', json.dumps(meta, ensure_ascii=False))

        for i, block in enumerate(session.blocks):
            bm = {
                'index':       block.index,
                'start_time':  block.start_time,
                'description': block.description,
                'source_name': block.source_name,
                'sample_rate': block.sample_rate,
                'channels': [
                    {'name': c.name, 'unit': c.unit,
                     'scale': c.scale, 'offset': c.offset}
                    for c in block.channels
                ],
                'annotations': [
                    {'t': a.t, 'text': a.text} for a in block.annotations
                ],
            }
            zf.writestr(f'b{i}_meta.json', json.dumps(bm, ensure_ascii=False))
            zf.writestr(f'b{i}_times.npy',  _arr_to_bytes(block.times.astype(np.float64)))
            zf.writestr(f'b{i}_values.npy', _arr_to_bytes(block.values.astype(np.float32)))

    os.replace(tmp, path)
    session.file_path = str(path)
    session.modified  = False


# ------------------------------------------------------------------
# Load
# ------------------------------------------------------------------

def load(path: str | Path) -> Session:
    path = Path(path)

    with zipfile.ZipFile(path, 'r') as zf:
        meta = json.loads(zf.read('_meta.json'))
        _require_version(meta.get('version', 0))

        session = Session(created=meta['created'])
        session.file_path = str(path)

        for i in range(meta['n_blocks']):
            bm      = json.loads(zf.read(f'b{i}_meta.json'))
            times   = _bytes_to_arr(zf.read(f'b{i}_times.npy'))
            values  = _bytes_to_arr(zf.read(f'b{i}_values.npy'))
            if times.ndim != 1 or values.ndim != 2 or times.shape[0] != values.shape[0]:
                raise ValueError(f'.fgd: блок {i} — несовпадение размеров times/values')

            channels = [
                ChannelInfo(
                    name   = c['name'],
                    unit   = c.get('unit', ''),
                    scale  = c.get('scale', 1.0),
                    offset = c.get('offset', 0.0),
                )
                for c in bm['channels']
            ]
            if values.shape[1] != len(channels):
                raise ValueError(f'.fgd: блок {i} — число каналов не совпадает')
            annotations = [
                Annotation(t=a['t'], text=a['text'])
                for a in bm.get('annotations', [])
            ]
            block = Block(
                index       = bm['index'],
                start_time  = bm['start_time'],
                description = bm.get('description', ''),
                source_name = bm.get('source_name', ''),
                sample_rate = bm['sample_rate'],
                channels    = channels,
                times       = times,
                values      = values,
                annotations = annotations,
            )
            session.blocks.append(block)

    session.modified = False
    return session


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _arr_to_bytes(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    np.save(buf, arr)
    return buf.getvalue()


def _bytes_to_arr(data: bytes) -> np.ndarray:
    return np.load(io.BytesIO(data), allow_pickle=False)


def _require_version(v: int):
    if v > FORMAT_VERSION:
        raise ValueError(
            f'Файл создан более новой версией FlowerGraph (формат {v}). '
            f'Обновите программу.'
        )
