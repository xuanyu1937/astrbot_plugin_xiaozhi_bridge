"""推送：节流、去重、退避重试，统一走 context.send_message。"""
from __future__ import annotations

import asyncio
import time

from astrbot.api import logger
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain


class Pusher:
    """把一段文本发到目标会话，自带节流与重试。"""

    def __init__(self, context, settings):
        self.ctx = context
        self.cfg = settings
        self._last_at = 0.0
        self._last_text = ""

    async def send(self, text: str, umo: str = "") -> str:
        target = umo or self.cfg.push_umo
        if not target:
            return "（没配置推送目标）"

        content = str(text or "").strip()
        if not content:
            return "（空内容，跳过）"

        now = time.time()
        if content == self._last_text and now - self._last_at < 10:
            return "（重复内容，跳过）"

        wait = 2.0 - (now - self._last_at)
        if wait > 0:
            await asyncio.sleep(wait)

        result = "失败"
        for attempt, backoff in enumerate((0.0, 1.0, 2.0), start=1):
            if backoff:
                await asyncio.sleep(backoff)
            try:
                chain = MessageChain([Plain(content)])
                await self.ctx.send_message(target, chain)
                self._last_at = time.time()
                self._last_text = content
                return "已发送"
            except Exception as exc:  # noqa: BLE001
                result = f"异常：{exc}"
                logger.warning("[xiaozhi_bridge] 推送失败(第%d次)：%s", attempt, exc)
        return result

    async def send_file(self, path: str, umo: str = "") -> str:
        target = umo or self.cfg.push_umo
        if not target:
            return "（没配置推送目标）"
        try:
            from astrbot.core.message.components import File

            await self.ctx.send_message(target, MessageChain([File(file=path, name="music.mp3")]))
            return "已发送音频"
        except Exception as exc:  # noqa: BLE001
            logger.warning("[xiaozhi_bridge] 发送音频失败：%s", exc)
            return f"发送音频失败：{exc}"
