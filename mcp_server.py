"""MCP 客户端：连小智的 wss，处理 JSON-RPC 2.0 的握手、工具列表与调用。"""
from __future__ import annotations

import asyncio
import json
import time

import websockets

from astrbot.api import logger

METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


class MCPClient:
    """主动向外连小智的 MCP 接入点，断线按指数退避重连。"""

    def __init__(self, token: str, handlers: dict, tool_provider):
        self.url = self._build_url(token)
        self.handlers = handlers
        self.tools = tool_provider
        self.ws = None
        self.connected_at = 0.0
        self.last_error = ""
        self._task = None
        self._stopping = False
        self._extra_tools: dict[str, dict] = {}

    @staticmethod
    def _build_url(token: str) -> str:
        token = (token or "").strip()
        if token.startswith(("ws://", "wss://")):
            return token
        return f"wss://api.xiaozhi.me/mcp/?token={token}"

    # ---------- 生命周期 ----------
    def start(self):
        if self._task and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run_forever())

    async def stop(self):
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._close()

    async def _close(self):
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:  # noqa: BLE001
                pass
            self.ws = None

    async def reconnect(self):
        await self._close()
        self.start()

    @property
    def online(self) -> bool:
        """最近 10 分钟内有成功连接，就算在线。"""
        return bool(self.connected_at) and (time.time() - self.connected_at < 600)

    async def _run_forever(self):
        """指数退避重连：1s 起，最长 30s。"""
        delay = 1.0
        while not self._stopping:
            try:
                async with websockets.connect(self.url, ping_interval=20) as ws:
                    self.ws = ws
                    self.connected_at = time.time()
                    self.last_error = ""
                    delay = 1.0
                    logger.info("[xiaozhi_bridge] MCP 已连接")
                    async for raw in ws:
                        await self._dispatch(ws, raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)[:160]
                logger.warning("[xiaozhi_bridge] MCP 断开：%s（%.0fs 后重连）", exc, delay)
            finally:
                self.ws = None
            if self._stopping:
                break
            await asyncio.sleep(delay)
            delay = min(30.0, delay * 2)

    # ---------- JSON-RPC 分发 ----------
    async def _dispatch(self, ws, raw):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        msg_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params") or {}
        if msg_id is None:
            return  # 通知，不用回

        try:
            result = await self._handle(method, params)
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}))
        except LookupError:
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": msg_id,
                "error": {"code": METHOD_NOT_FOUND, "message": f"未知方法：{method}"},
            }))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[xiaozhi_bridge] 处理 %s 失败：%s", method, exc)
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": msg_id,
                "error": {"code": INTERNAL_ERROR, "message": str(exc)[:160]},
            }))

    async def _handle(self, method, params):
        if method == "initialize":
            return {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "astrbot-xiaozhi-bridge", "version": "2.0.0"},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.tools() + list(self._extra_tools.values())}
        if method == "tools/call":
            return await self._call_tool(params)
        if method == "resources/list":
            return {"resources": []}
        if method == "prompts/list":
            return {"prompts": []}
        raise LookupError(method)

    async def _call_tool(self, params):
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise ValueError("arguments 必须是对象")

        handler = self.handlers.get(name)
        if handler is None:
            handler = self._extra_tools.get(name, {}).get("handler")
            if handler is None:
                raise LookupError(name)

        try:
            text = await handler(args)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[xiaozhi_bridge] 工具 %s 执行失败：%s", name, exc)
            content = [{"type": "text", "text": f"执行失败：{exc}"}]
            return {"content": content, "isError": True}

        if text is None or text == "":
            # 空内容且非错误：让云端保持安静
            return {"content": [], "isError": False}
        return {"content": [{"type": "text", "text": str(text)}], "isError": False}

    # ---------- 给别的插件用的扩展口 ----------
    def register_tool(self, name, description, schema, handler):
        if not name or not callable(handler):
            return False
        self._extra_tools[name] = {
            "name": name,
            "description": description or "",
            "inputSchema": schema or {"type": "object", "properties": {}},
            "handler": handler,
        }
        return True

    def unregister_tool(self, name):
        return self._extra_tools.pop(name, None) is not None
