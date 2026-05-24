import time
from dataclasses import dataclass, field
from datetime import datetime
import numpy as np


@dataclass
class ChannelInfo:
    name:   str
    unit:   str   = ''
    scale:  float = 1.0
    offset: float = 0.0


@dataclass
class Annotation:
    t:    float
    text: str


@dataclass
class Block:
    start_time:  float           # Unix-время начала записи
    source_name: str
    sample_rate: int
    channels:    list[ChannelInfo]
    times:       np.ndarray      # float64 (n_samples,)  — относительное время от 0
    values:      np.ndarray      # float32 (n_samples, n_channels)
    description: str                    = ''
    annotations: list[Annotation]       = field(default_factory=list)
    index:       int                    = 0

    @property
    def n_channels(self) -> int:
        return len(self.channels)

    @property
    def n_samples(self) -> int:
        return len(self.times)

    @property
    def duration(self) -> float:
        return float(self.times[-1] - self.times[0]) if self.n_samples >= 2 else 0.0

    @property
    def t_start(self) -> float:
        return float(self.times[0]) if self.n_samples else 0.0

    @property
    def t_end(self) -> float:
        return float(self.times[-1]) if self.n_samples else 0.0

    def start_datetime(self) -> datetime:
        return datetime.fromtimestamp(self.start_time)

    def label(self) -> str:
        dt = self.start_datetime().strftime('%H:%M:%S')
        return f'Блок {self.index + 1}  [{dt}]  {self.duration:.3f} с'


@dataclass
class Session:
    created:   float             = field(default_factory=time.time)
    blocks:    list[Block]       = field(default_factory=list)
    file_path: str | None        = None
    modified:  bool              = False

    # ------------------------------------------------------------------

    def add_block(self, block: Block):
        block.index = len(self.blocks)
        self.blocks.append(block)
        self.modified = True

    def remove_block(self, idx: int):
        if 0 <= idx < len(self.blocks):
            self.blocks.pop(idx)
            for i, b in enumerate(self.blocks):
                b.index = i
            self.modified = True

    # ------------------------------------------------------------------

    @property
    def n_blocks(self) -> int:
        return len(self.blocks)

    @property
    def is_empty(self) -> bool:
        return not self.blocks or all(b.n_samples == 0 for b in self.blocks)
