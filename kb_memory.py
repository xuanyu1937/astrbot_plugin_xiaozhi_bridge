"""知识库记忆：把「记住…」这类话写进 AstrBot 知识库，实现跨端共享事实。

为什么不用聊天记录：历史窗口只有最近几十条，换设备、换天就丢了；
写进知识库后，微信和小智哪边问都能检索到。
"""
from __future__ import annotations

import time

from astrbot.api import logger


def match_remember(text: str, words: list[str]) -> str:
    """判断是不是「要我记住」，是就返回要记的内容，否则返回空串。"""
    raw = str(text or "").strip()
    for word in words:
        if not word or word not in raw:
            continue
        idx = raw.find(word) + len(word)
        content = raw[idx:].strip(" ：:，,。.、\n")
        if content:
            return content
    return ""


async def save(context, settings, content: str) -> str:
    """写进知识库，返回一句可读结果（失败也不抛，只记日志）。"""
    if not settings.bool("kb_enable"):
        return ""
    content = str(content or "").strip()
    if not content:
        return ""

    try:
        manager = getattr(context, "kb_manager", None)
        if manager is None:
            logger.warning("[xiaozhi_bridge] 本机没有知识库管理器，跳过记忆")
            return ""

        helper = None
        wanted = settings.str("kb_name").strip()
        if wanted:
            helper = await manager.get_kb_by_name(wanted)
        if helper is None:
            items = await manager.list_kbs()
            if not items:
                logger.warning("[xiaozhi_bridge] 没有任何知识库，跳过记忆")
                return ""
            helper = await manager.get_kb(items[0].kb_id)

        if helper is None:
            logger.warning("[xiaozhi_bridge] 取知识库失败，跳过记忆")
            return ""

        stamp = time.strftime("%Y-%m-%d %H:%M")
        name = f"小智记忆-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        body = f"{stamp} 记下：{content}\n".encode("utf-8")
        await helper.upload_document(
            file_name=name,
            file_content=body,
            file_type="text/plain",
            chunk_size=256,
            chunk_overlap=20,
            pre_chunked_text=[f"{stamp} 记下：{content}"],
        )
        logger.info("[xiaozhi_bridge] 已写入知识库：%s", content[:40])
        return content
    except Exception as exc:  # noqa: BLE001
        logger.warning("[xiaozhi_bridge] 写知识库失败：%s", exc)
        return ""
