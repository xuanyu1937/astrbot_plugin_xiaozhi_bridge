"""点歌：识别意图、找点歌插件搜索、下载到插件数据目录、按天数与容量清理。"""
from __future__ import annotations

import os
import re
import time

import httpx

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_NAME = "astrbot_plugin_xiaozhi_bridge"
PATTERNS = [
    re.compile(r"^(?:帮我)?点(?:一)?首歌?[：: ]?(.+)$"),
    re.compile(r"^(?:放|来)(?:一)?首歌?[：: ]?(.+)$"),
    re.compile(r"^我想听[：: ]?(.+)$"),
]
BAD_WORDS = ("翻唱", "片段", "伴奏", "demo", "cover", "剪辑", "铃声")


def data_dir() -> str:
    """插件数据目录；本模块不依赖其它本地模块，便于单独加载。"""
    base = os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)
    base = os.path.abspath(base)
    os.makedirs(base, exist_ok=True)
    return base


def extract_keyword(text: str) -> str:
    """从「点歌/放首歌/我想听/来一首」里抽出歌名。"""
    raw = str(text or "").strip()
    for pattern in PATTERNS:
        match = pattern.search(raw)
        if match:
            return match.group(1).strip()
    return ""


def score_song(song: dict, keyword: str) -> float:
    """按歌名精确度、歌手匹配、时长、关键词扣分。"""
    name = str(song.get("name") or song.get("title") or "")
    singer = str(song.get("singer") or song.get("artist") or "")
    score = 0.0
    if keyword and keyword in name:
        score += 3
    if name and keyword and name.strip() == keyword.strip():
        score += 4
    if singer and singer in keyword:
        score += 2
    try:
        duration = int(song.get("duration") or song.get("interval") or 0)
        if 60 <= duration <= 600:
            score += 1
    except (TypeError, ValueError):
        pass
    lowered = name.lower()
    for bad in BAD_WORDS:
        if bad in lowered:
            score -= 2
    return score


def songs_of(result) -> list[dict]:
    """不同音源返回结构不一样，尽量都捞出来。"""
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]
    if isinstance(result, dict):
        for key in ("songs", "data", "result", "list"):
            value = result.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        return [result]
    return []


async def search(context, keyword: str):
    """找点歌插件，按优先级逐个音源搜，打分选最优。"""
    star = None
    try:
        star = context.get_registered_star("astrbot_plugin_music")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[xiaozhi_bridge] 取点歌插件失败：%s", exc)
    if not star:
        return None

    plugin = getattr(star, "star_cls", None) or getattr(star, "instance", None)
    if plugin is None:
        return None

    try:
        players = plugin.get_players_by_priority()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[xiaozhi_bridge] 取音源失败：%s", exc)
        return None

    best, best_score = None, 0.0
    for player in players:
        try:
            result = await player.search(keyword)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[xiaozhi_bridge] 音源搜索失败：%s", exc)
            continue
        for song in songs_of(result):
            current = score_song(song, keyword)
            if current > best_score:
                best, best_score = song, current
    return best


def cache_dir() -> str:
    path = os.path.join(data_dir(), "music")
    os.makedirs(path, exist_ok=True)
    return path


async def download(song: dict, ttl: int = 900) -> str:
    """下载音频到插件数据目录，返回本地路径；失败返回空串。"""
    url = song.get("url") or song.get("audio") or song.get("play_url")
    if not url:
        return ""
    name = re.sub(r"[^\w\-.]", "_", str(song.get("name") or "music"))[:60]
    path = os.path.join(cache_dir(), f"{name}.mp3")
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl:
            return path
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return ""
                with open(path, "wb") as f:
                    async for chunk in resp.aiter_bytes(65536):
                        f.write(chunk)
        return path
    except Exception as exc:  # noqa: BLE001
        logger.warning("[xiaozhi_bridge] 下载音频失败：%s", exc)
        return ""


def cleanup(keep_days: int = 3, max_mb: int = 200):
    """按天数与总容量清理缓存，返回（删了几个文件, 释放多少字节）。"""
    folder = cache_dir()
    removed = freed = 0
    try:
        files = [os.path.join(folder, f) for f in os.listdir(folder)]
        files = [p for p in files if os.path.isfile(p)]
        deadline = time.time() - keep_days * 86400
        for path in list(files):
            if os.path.getmtime(path) < deadline:
                freed += os.path.getsize(path)
                os.remove(path)
                removed += 1
                files.remove(path)
        total = sum(os.path.getsize(p) for p in files)
        limit = max_mb * 1024 * 1024
        for path in sorted(files, key=os.path.getmtime):
            if total <= limit:
                break
            size = os.path.getsize(path)
            os.remove(path)
            removed += 1
            freed += size
            total -= size
    except Exception as exc:  # noqa: BLE001
        logger.warning("[xiaozhi_bridge] 清理缓存失败：%s", exc)
    return removed, freed
