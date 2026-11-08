"""会话同步：回答前合并上下文，回答后补写记录，全部走 ConversationManager。"""
from __future__ import annotations

import json

from astrbot.api import logger


def history_of(conv) -> list[dict]:
    """history 可能是 JSON 字符串也可能是列表，两种都兼容。"""
    raw = getattr(conv, "history", None)
    if raw is None:
        raw = getattr(conv, "content", None)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except json.JSONDecodeError:
            return []
    return raw if isinstance(raw, list) else []


def text_of(message) -> str:
    parts = (message or {}).get("content")
    if isinstance(parts, str):
        return parts.strip()
    if isinstance(parts, list):
        return "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)).strip()
    return ""


def compact(messages, limit):
    """只留 user/assistant 的纯文本，按 role + 文本去重。"""
    seen = set()
    out = []
    for msg in messages[-limit:]:
        if not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant"):
            continue
        text = text_of(msg)
        if not text:
            continue
        key = (msg["role"], text)
        if key in seen:
            continue
        seen.add(key)
        out.append({"role": msg["role"], "content": [{"type": "text", "text": text}]})
    return out


class SessionSync:
    """设备会话与目标会话之间的双向同步。"""

    def __init__(self, context, settings, state):
        self.ctx = context
        self.cfg = settings
        self.state = state
        self.mgr = context.conversation_manager

    async def _cid(self, umo: str, create: bool = False):
        try:
            cid = await self.mgr.get_curr_conversation_id(umo)
            if not cid and create:
                cid = await self.mgr.new_conversation(umo, title="小智设备")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[xiaozhi_bridge] 取会话失败：%s", exc)
            return None
        return cid

    async def _stamp(self, umo: str, cid: str) -> str:
        """指纹用「更新时间 + 条数」，没变化就跳过重复同步。"""
        try:
            conv = await self.mgr.get_conversation(umo, cid)
            updated = getattr(conv, "updated_at", "") or ""
            return f"{updated}|{len(history_of(conv))}"
        except Exception:  # noqa: BLE001
            return ""

    async def seed(self, device_umo: str) -> None:
        """回答前：把目标会话最近内容合并进设备会话。"""
        if not self.cfg.bool("context_seed_enable"):
            return
        target = self.cfg.push_umo
        if not target:
            return

        src_cid = await self._cid(target)
        if not src_cid:
            return
        stamp = await self._stamp(target, src_cid)
        if stamp and stamp == self.state.get("seed_stamp"):
            return

        try:
            src = await self.mgr.get_conversation(target, src_cid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[xiaozhi_bridge] 读目标会话失败：%s", exc)
            return

        take = self.cfg.int("context_seed_messages", 1, 500)
        incoming = compact(history_of(src), take)
        if not incoming:
            return

        dst_cid = await self._cid(device_umo, create=True)
        if not dst_cid:
            return

        existing = []
        try:
            dst = await self.mgr.get_conversation(device_umo, dst_cid)
            existing = compact(history_of(dst), self.cfg.int("context_max_messages", 50, 2000))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[xiaozhi_bridge] 读设备会话失败：%s", exc)

        keep = self.cfg.int("context_max_messages", 50, 2000)
        merged = compact(existing + incoming, keep)
        try:
            await self.mgr.update_conversation(device_umo, dst_cid, history=merged)
            self.state.set("seed_stamp", stamp)
            logger.info("[xiaozhi_bridge] 上下文合并 %d 条，共 %d 条", len(incoming), len(merged))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[xiaozhi_bridge] 写合并上下文失败：%s", exc)

    async def record(self, question: str, answer: str) -> None:
        """回答后：把这一问一答补写进目标会话，重复内容不再写。"""
        if not self.cfg.bool("mirror_enable"):
            return
        target = self.cfg.push_umo
        if not target or not answer:
            return

        try:
            cid = await self._cid(target, create=True)
            if not cid:
                return
            conv = await self.mgr.get_conversation(target, cid)
            history = history_of(conv)
            tail = [(m.get("role"), text_of(m)) for m in history[-2:] if isinstance(m, dict)]
            if tail == [("user", question), ("assistant", answer)]:
                return

            user_msg = {"role": "user", "content": [{"type": "text", "text": question}]}
            bot_msg = {"role": "assistant", "content": [{"type": "text", "text": answer}]}
            try:
                # 优先用官方成对写入接口
                await self.mgr.add_message_pair(cid, user_msg, bot_msg)
            except Exception:  # noqa: BLE001
                history = history + [user_msg, bot_msg]
                await self.mgr.update_conversation(target, cid, history=history[-400:])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[xiaozhi_bridge] 补写记录失败：%s", exc)
