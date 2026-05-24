import json
import os
from pathlib import Path


def _default_config() -> dict:
    return {
        "window": {
            "x": 100, "y": 100,
            "width": 1280, "height": 800,
            "maximized": False
        },
        "recent_files": [],
        "com_profiles": [],
        "autosave_interval_min": 5,
        "reconnect_interval_sec": 2,
        "display": {
            "update_interval_ms": 50,
            "background_color": "#1e1e1e",
            "foreground_color": "#ffffff"
        }
    }


class Config:
    def __init__(self):
        if os.name == 'nt':
            base = Path(os.environ.get("APPDATA", Path.home()))
        else:
            base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        self._path = base / "FlowerGraph" / "settings.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data = _default_config()
        self.load()

    def load(self):
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self._merge(self._data, loaded)
            except Exception:
                pass

    def save(self):
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def get(self, *keys, default=None):
        node = self._data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    def set(self, *keys, value):
        node = self._data
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value

    def add_recent_file(self, path: str):
        files: list = self._data.setdefault("recent_files", [])
        if path in files:
            files.remove(path)
        files.insert(0, path)
        self._data["recent_files"] = files[:10]

    def _merge(self, base: dict, override: dict):
        for k, v in override.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                self._merge(base[k], v)
            else:
                base[k] = v


config = Config()
