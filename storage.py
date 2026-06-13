import copy
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict


class JsonStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data: Dict[str, Any] = {}
        self.load()

    def _default(self):
        return {"wallet": {}, "sign": {}, "catgirls": {}, "items": {}, "pending_adoptions": {}}

    def load(self):
        with self.lock:
            if not self.path.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.data = self._default()
                self.save()
                return
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                try:
                    backup = self.path.with_suffix(f".broken.{int(time.time())}.json")
                    self.path.replace(backup)
                except Exception:
                    pass
                self.data = self._default()
                self.save()
            for k, v in self._default().items():
                self.data.setdefault(k, v)

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            tmp.replace(self.path)

    def get(self, *keys, default=None):
        with self.lock:
            cur = self.data
            for key in keys:
                if not isinstance(cur, dict) or key not in cur:
                    return default
                cur = cur[key]
            return copy.deepcopy(cur)

    def set(self, *keys, value):
        with self.lock:
            cur = self.data
            for key in keys[:-1]:
                cur = cur.setdefault(key, {})
            cur[keys[-1]] = value
            self.save()

    def update(self, func):
        with self.lock:
            result = func(self.data)
            self.save()
            return result
