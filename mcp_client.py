# -*- coding: utf-8 -*-
"""小智 MCP 客户端（插件内置版）。

职责：连接 xiaozhi.me 的 MCP 接入点，把「跟我说话」包装成工具 ask_pengyuhan，
      官方云端智能体只当传声筒，人设/记忆/工具全走本机 AstrBot。

对外工具：
  - ask_pengyuhan        把用户原话交给 AstrBot，返回真实回复
  - send_wechat_message  把一段文字发到绑定的微信
  - list_astrbot_bots    查看在线机器人（排障）
"""

from __future__ import annotations

import asyncio
import json
from astrbot.api import logger as _alogger
import re
import time

import httpx

log = _alogger

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "pengyuhan-xiaozhi", "version": "1.1.0"}


def build_ws_url(token_or_url: str) -> str:
    """支持两种填法：完整 wss 地址，或只填 token。"""
    raw = (token_or_url or "").strip()
    if not raw:
        return ""
    if raw.startswith("ws://") or raw.startswith("wss://"):
        return raw
    return f"wss://api.xiaozhi.me/mcp/?token={raw}"


class XiaoZhiMCPClient:
    """一个自愈的 MCP over WebSocket 客户端。"""

    def __init__(self, cfg: dict, ask_fn, mirror_fn=None, send_fn=None, bots_fn=None, play_fn=None,
                 extra_provider=None, extra_caller=None):
        self.cfg = cfg or {}
        self.ask_fn = ask_fn
        self.mirror_fn = mirror_fn
        self.send_fn = send_fn
        self.bots_fn = bots_fn
        self.play_fn = play_fn
        self.extra_provider = extra_provider   # () -> dict[str, dict]
        self.extra_caller = extra_caller       # async (name, args) -> str
        self._task: asyncio.Task | None = None
        self._stop = False
        self.connected_at = 0.0
        self._bg: set = set()   # 后台补写/推送任务，防止被回收
        self.ws_url = build_ws_url(self.cfg.get("xiaozhi_token") or "")

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if not self.ws_url:
            log.warning("[xiaozhi] 没填 token/接入点地址，MCP 客户端不启动")
            return
        if self._task is None or self._task.done():
            self._stop = False
            self._task = asyncio.create_task(self._run(), name="xiaozhi-mcp")

    async def stop(self) -> None:
        self._stop = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # ---------- 工具清单 ----------
    @property
    def tools(self) -> list[dict]:
        base = self._base_tools()
        extra = {}
        try:
            if self.extra_provider:
                extra = self.extra_provider() or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("[xiaozhi] 读取扩展工具失败：%s", exc)
        for name, item in extra.items():
            base.append({
                "name": name,
                "description": item.get("description") or "",
                "inputSchema": item.get("inputSchema") or {"type": "object", "properties": {}},
            })
        return base

    def _base_tools(self) -> list[dict]:
        return [
            {
                "name": "ask_pengyuhan",
                "description": (
                    "把用户说的话原封不动转交给本机 AstrBot 中的彭语涵，返回她的真实回复。"
                    "凡用户想聊天、提问、闲聊、倾诉，都必须先调本工具；"
                    "拿到结果后一字不改、不总结、不改写地念出来。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "用户刚说的原话，逐字传入，不要改写或概括",
                        },
                        "session_id": {
                            "type": "string",
                            "description": "可选会话标识，同一设备固定传同一个值以保持上下文",
                        },
                    },
                    "required": ["message"],
                },
            },
            {
                "name": "send_wechat_message",
                "description": "把一段文字发到绑定的微信（由彭语涵代发），用于转达、提醒、留言。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "要发送的文字，不要带 markdown"}
                    },
                    "required": ["text"],
                },
            },
            {
                "name": "play_music",
                "description": (
                    "按歌名点歌并返回可直接播放的音频直链。"
                    "用户说想听歌、放首歌、来一首XXX 时必须调用本工具；"
                    "拿到结果后把歌名和链接念出来，不要自己编链接。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "keyword": {
                            "type": "string",
                            "description": "歌名，可带歌手，例如「起风了」「晴天 周杰伦」",
                        },
                        "platform": {
                            "type": "string",
                            "description": "可选音源：netease、qqmusic、kugou、kuwo、apple 等，留空自动按优先级",
                        },
                    },
                    "required": ["keyword"],
                },
            },
            {
                "name": "list_astrbot_bots",
                "description": "查看当前在线消息机器人（平台、账号），用于排障。",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]

    # ---------- 工具执行 ----------
    async def call_tool(self, name: str, args: dict) -> dict:
        args = args or {}
        try:
            if name == "ask_pengyuhan":
                question = str(args.get("message") or "").strip()
                if not question:
                    return self._err("message 不能为空")

                t0 = time.time()
                answer = await self.ask_fn(question, args.get("session_id"))
                log.info("[xiaozhi] ask 完成 %.1fs", time.time() - t0)

                text_now = (answer or "").strip()
                if text_now == "[SELF]":
                    # 未命中召唤词：不给内容，让模型自己回答
                    log.info("[xiaozhi] 本轮交回本体回答")
                    return {"content": [], "isError": False}

                silent = (not text_now) or text_now in ("\u200b", "[SKIP]")
                if silent:
                    # 返回空 content，让客户端本轮不产出任何语音
                    return {"content": [], "isError": False}

                # 先把回复交给设备，记录/推送扔后台，别拖慢开口时间
                if self.mirror_fn and answer:
                    task = asyncio.create_task(self._mirror_bg(question, answer))
                    self._bg.add(task)
                    task.add_done_callback(self._bg.discard)
                return self._ok(answer)

            if name == "send_wechat_message":
                if not self.send_fn:
                    return self._err("未配置微信发送能力")
                text = str(args.get("text") or "").strip()
                if not text:
                    return self._err("text 不能为空")
                return self._ok(await self.send_fn(text))

            if name == "play_music":
                keyword = str(args.get("keyword") or "").strip()
                if not keyword:
                    return self._err("keyword 不能为空")
                if not self.play_fn:
                    return self._err("未接入点歌能力")
                text = await self.play_fn(keyword, str(args.get("platform") or "").strip())
                return self._ok(text) if "\n" in text else self._err(text)

            if name == "list_astrbot_bots":
                if not self.bots_fn:
                    return self._err("未配置机器人查询能力")
                return self._ok(await self.bots_fn())

            if self.extra_caller:
                try:
                    out = await self.extra_caller(name, args)
                    return self._ok(str(out))
                except KeyError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    log.warning("[xiaozhi] 扩展工具 %s 失败：%s", name, exc)
                    return self._err(f"{name} 执行失败：{exc}")
            return self._err(f"未知工具：{name}")
        except Exception as exc:  # noqa: BLE001
            log.exception("[xiaozhi] 工具执行异常")
            return self._err(f"执行失败：{exc}")

    async def _mirror_bg(self, question: str, answer: str) -> None:
        """后台补写记录 + 推送，失败只记日志，不影响设备。"""
        try:
            await self.mirror_fn(question, answer)
        except Exception as exc:  # noqa: BLE001
            log.warning("[xiaozhi] 补写记录失败（忽略）：%s", exc)

    @staticmethod
    def _ok(text: str) -> dict:
        return {"content": [{"type": "text", "text": text}], "isError": False}

    @staticmethod
    def _err(text: str) -> dict:
        return {"content": [{"type": "text", "text": text}], "isError": True}

    # ---------- JSON-RPC ----------
    async def handle(self, msg: dict) -> dict | None:
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}
        if mid is None:
            return None

        def ok(result):
            return {"jsonrpc": "2.0", "id": mid, "result": result}

        def err(code: int, message: str):
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

        if method == "initialize":
            return ok(
                {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                }
            )
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": self.tools})
        if method == "tools/call":
            return ok(await self.call_tool(params.get("name") or "", params.get("arguments") or {}))
        if method == "resources/list":
            return ok({"resources": []})
        if method == "prompts/list":
            return ok({"prompts": []})
        return err(-32601, f"Method not found: {method}")

    # ---------- 连接循环 ----------
    async def _run(self) -> None:
        import websockets

        backoff = 1.0
        while not self._stop:
            try:
                log.info("[xiaozhi] 连接接入点 %s", self.ws_url.split("?")[0])
                async with websockets.connect(
                    self.ws_url, ping_interval=25, ping_timeout=20,
                    max_size=None, open_timeout=20,
                ) as ws:
                    backoff = 1.0
                    self.connected_at = time.time()
                    log.info("[xiaozhi] 已连接，等待 JSON-RPC")
                    async for raw in ws:
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", "replace")
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        for item in payload if isinstance(payload, list) else [payload]:
                            if not isinstance(item, dict):
                                continue
                            resp = await self.handle(item)
                            if resp is not None:
                                await ws.send(json.dumps(resp, ensure_ascii=False))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("[xiaozhi] 断开（%s），%.0fs 后重连", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
