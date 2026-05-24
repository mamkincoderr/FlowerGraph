from abc import ABC, abstractmethod
from typing import Callable
import numpy as np


DataCallback  = Callable[[np.ndarray, np.ndarray], None]  # (times, values)
ErrorCallback = Callable[[str], None]


class BaseSource(ABC):
    """
    Абстрактный интерфейс источника данных.
    Каждый плагин (COM-ASCII, COBS, генератор) реализует этот класс.

    Поток данных:
        источник вызывает _emit(times, values) где:
            times  — numpy float64 (n,)       временны́е метки, секунды
            values — numpy float32 (n, N_ch)  значения каналов
    """

    def __init__(self):
        self._data_cb:  DataCallback  | None = None
        self._error_cb: ErrorCallback | None = None
        self._running = False

    def set_data_callback(self, cb: DataCallback):
        self._data_cb = cb

    def set_error_callback(self, cb: ErrorCallback):
        self._error_cb = cb

    @property
    def is_running(self) -> bool:
        return self._running

    @abstractmethod
    def get_name(self) -> str:
        """Название источника, отображается в UI."""

    @abstractmethod
    def get_channel_count(self) -> int:
        """Число каналов."""

    @abstractmethod
    def get_channel_names(self) -> list[str]:
        """Имена каналов по умолчанию."""

    @abstractmethod
    def start(self):
        """Начать приём данных."""

    @abstractmethod
    def stop(self):
        """Остановить приём данных."""

    @abstractmethod
    def get_config_widget(self):
        """Вернуть QWidget с настройками источника (или None)."""

    def _emit(self, times: np.ndarray, values: np.ndarray):
        if self._data_cb:
            self._data_cb(times, values)

    def _emit_error(self, message: str):
        if self._error_cb:
            self._error_cb(message)
