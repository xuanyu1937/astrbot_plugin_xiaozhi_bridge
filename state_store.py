"""运行时状态：只存 plugin_data 目录，原子写入，旧文件迁移一次。"""
from __future__ import annotations

import json
import os
import tempfile

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_NAME = "astrbot_plugin_xiaozhi_bridge"


def data_dir() -> str:
    """插件数据目录，按官方要求放在 data/plugin_data/<插件名>/。"""
    base = os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)
    base = os.path.abspath(base)
    os.makedirs(base, exist_ok=True)
    return base


class StateStore:
    """推送开关、召唤模式、同步指纹这类易变状态。"""

    def __init__(self, legacy_paths=None):
        self.path = os.path.join(data_dir(), "state.json")
        self._legacy = legacy_paths or []
        self._cache = self._load()

    def _load(self):
        candidates = [self.path, *self._existing_legacy()]
        for path in candidates:
            try:
                if not os.path.exists(path):
                    continue
                with open(path, encoding="utf-8") as f:
                    data = json.load(f) or {}
                if path != self.path:
                    self._write(data)
                    logger.info("[xiaozhi_bridge] 旧状态已迁到 %s", self.path)
                return data
            except Exception as exc:  # noqa: BLE001
                logger.warning("[xiaozhi_bridge] 读状态失败(%s)：%s", path, exc)
        return {}

    def _existing_legacy(self):
        out = []
        for path in self._legacy:
            try:
                if path and os.path.exists(path):
                    out.append(path)
            except Exception:  # noqa: BLE001
                continue
        return out

    def _write(self, data):
        """临时文件 + os.replace，避免写一半留下坏文件。"""
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[xiaozhi_bridge] 写状态失败：%s", exc)

    # ---------- 常用读写 ----------
    def get(self, key, default=None):
        return self._cache.get(key, default)

    def set(self, key, value):
        if self._cache.get(key) == value:
            return
        self._cache[key] = value
        self._write(self._cache)

    def update(self, **kwargs):
        changed = False
        for key, value in kwargs.items():
            if self._cache.get(key) != value:
                self._cache[key] = value
                changed = True
        if changed:
            self._write(self._cache)

    @property
    def all(self):
        return dict(self._cache)
