"""小智设备桥接：把小智的 MCP 接入点接到 AstrBot 的人格。

设备上说的话经 MCP 送进来，这里转给 AstrBot 的 Open API，
拿回回复再念给设备。人设、记忆、知识库、工具全部复用 AstrBot。
"""

from __future__ import annotations

import asyncio
import os
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response

PLUGIN_NAME = "astrbot_plugin_xiaozhi_bridge"


def _load_sibling(name: str):
    """按文件路径加载同目录模块。

    AstrBot 有时把插件当包导入、有时把 main.py 当顶层模块导入，
    两种情况下兄弟模块的搜索路径都不一定对，所以这里不依赖 sys.path。
    """
    import importlib.util

    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"_xiaozhi_bridge_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_chat = _load_sibling("chat_api")
_state = _load_sibling("state_store")
_mcp = _load_sibling("mcp_server")
_music = _load_sibling("music")
_push = _load_sibling("push")
_settings = _load_sibling("settings")
_sync = _load_sibling("session_sync")

ChatError = _chat.ChatError
ask_astrbot = _chat.ask_astrbot
StateStore = _state.StateStore
data_dir = _state.data_dir
MCPClient = _mcp.MCPClient
cleanup = _music.cleanup
download = _music.download
extract_keyword = _music.extract_keyword
search = _music.search
Pusher = _push.Pusher
Settings = _settings.Settings
SessionSync = _sync.SessionSync

DATA_PREFIX = f"/{PLUGIN_NAME}/"


@register(
    PLUGIN_NAME,
    "轩宇(xuanyu)",
    "把小智（xiaozhi.me / ESP32）接入 AstrBot，设备说话由 AstrBot 的人格回答",
    "2.0.0",
)
class XiaozhiBridge(Star):
    """桥接主体：管理 MCP 连接、对话编排、推送与视图接口。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        # Star 基类不保存配置对象，这里自己留一份，保存配置时要用
        self.astrbot_config = config
        self.cfg = Settings(config or {})
        legacy = os.path.join(data_dir(), "state.json")
        self.state = StateStore(legacy_paths=[legacy])
        self.sync = SessionSync(context, self.cfg, self.state)
        self.pusher = Pusher(context, self.cfg)
        self.mcp = None
        self.started_at = time.time()
        self.last_error = ""
        self.count_today = 0
        self._cleanup_task = None
        self._bg_tasks: set[asyncio.Task] = set()

    # ================= 生命周期 =================
    async def initialize(self) -> None:
        self.state.update(push_enabled=self.cfg.bool("push_to_wechat"))
        self._register_views()
        self._start_mcp()
        if self.cfg.bool("music_cleanup_enable"):
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("[xiaozhi_bridge] 插件就绪，MCP 目标 %s", self._mask(self.cfg.token))

    async def terminate(self) -> None:
        """重载前必须收干净：取消后台任务、关连接，避免残留。"""
        for task in [self._cleanup_task, *self._bg_tasks]:
            if task and not task.done():
                task.cancel()
        if self.mcp:
            await self.mcp.stop()
            self.mcp = None
        logger.info("[xiaozhi_bridge] 已停止")

    def _start_mcp(self):
        token = self.cfg.token
        if not token:
            logger.warning("[xiaozhi_bridge] 还没填小智 Token，MCP 不启动")
            return
        self.mcp = MCPClient(token, self._tool_handlers(), self._tool_specs)
        self.mcp.start()

    @staticmethod
    def _mask(token: str) -> str:
        return f"…{token[-6:]}" if token and len(token) > 6 else "（空）"

    # ================= MCP 工具定义 =================
    def _tool_specs(self) -> list[dict]:
        ask_name = self.cfg.str("ask_tool_name", "ask_astrbot") or "ask_astrbot"
        return [
            {
                "name": ask_name,
                "description": (
                    "凡聊天、提问、倾诉都必须先调用本工具，拿到返回文本后一字不改地念出来。"
                    "如果你这轮不需要回答用户，就不要调用。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "用户原话"},
                        "session_id": {"type": "string", "description": "可选的会话标识"},
                    },
                    "required": ["message"],
                },
            },
            {
                "name": "send_wechat_message",
                "description": "把一段文本发到配置好的目标会话（通常是微信）。",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
            {
                "name": "play_music",
                "description": "按歌名点歌，返回歌名与可播放地址。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string"},
                        "platform": {"type": "string"},
                    },
                    "required": ["keyword"],
                },
            },
            {
                "name": "bridge_status",
                "description": "查看桥接自身状态，排障用。",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]

    def _tool_handlers(self) -> dict:
        ask_name = self.cfg.str("ask_tool_name", "ask_astrbot") or "ask_astrbot"
        return {
            ask_name: self._tool_ask,
            "send_wechat_message": self._tool_send,
            "play_music": self._tool_music,
            "bridge_status": self._tool_status,
        }

    async def _tool_ask(self, args: dict) -> str:
        """主链路：判断是否该接管，然后交给 AstrBot 回答。"""
        message = str(args.get("message") or "").strip()
        device_id = str(args.get("session_id") or "").strip()
        if not message:
            return ""

        if self.cfg.bool("drop_boot_greeting") and self._is_boot_greeting(message):
            if self.cfg.str("boot_greeting_mode", "skip") == "skip":
                return ""
            prompt = (
                f"（{self.cfg.alias}刚把设备打开，人就在旁边。用一两句很短的话主动跟他打个招呼，"
                "像平时那样自然，别问他要干什么，别啰嗦，不要自称是设备本身）"
            )
            return await self._answer(prompt, device_id, record=False)

        verdict, text = self._gate(message)
        if verdict != "pass":
            return ""
        return await self._answer(text, device_id, record=True)

    def _is_boot_greeting(self, message: str) -> bool:
        """设备每次上电会说的固定句子，按长度和开头粗略识别。"""
        raw = message.strip()
        if len(raw) > 20:
            return False
        keys = ("你好", "在吗", "小智", "嗨", "哈喽", "我来了")
        return any(raw.startswith(k) for k in keys)

    def _gate(self, message: str) -> tuple[str, str]:
        """决定这条消息是放行还是静默。"""
        raw = message.strip()
        for word in self.cfg.exit_words:
            if word and word in raw:
                self.state.update(mode="idle", mode_since=0.0)
                return "silent", ""

        if not self.cfg.bool("require_trigger"):
            return "pass", raw

        mode = self.state.get("mode") or "idle"
        since = float(self.state.get("mode_since") or 0)
        idle_minutes = self.cfg.int("trigger_idle_minutes", 0, 1440)
        if mode == "persona" and idle_minutes and time.time() - since > idle_minutes * 60:
            self.state.update(mode="idle", mode_since=0.0)
            mode = "idle"

        for word in self.cfg.trigger_words:
            if word and word in raw:
                self.state.update(mode="persona", mode_since=time.time())
                return "pass", self._strip_trigger(raw, word)

        if mode == "persona":
            self.state.set("mode_since", time.time())
            return "pass", raw
        return "silent", ""

    def _strip_trigger(self, text: str, word: str) -> str:
        """去掉「召唤 XX」「叫 XX」「让 XX」这类前缀，只留正文。"""
        for prefix in (f"召唤{word}", f"叫{word}", f"让{word}", word):
            if text.startswith(prefix):
                return text[len(prefix):].strip(" ，,：:")
        return text.replace(word, "", 1).strip(" ，,：:")

    # ================= 对话编排 =================
    async def _answer(self, question: str, device_id: str = "", record: bool = True) -> str:
        """回答一个问题：先同步上下文，再按长任务开关等结果。"""
        umo = self.cfg.device_umo(device_id)
        try:
            await self.sync.seed(umo)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[xiaozhi_bridge] 同步上下文失败：%s", exc)

        self.count_today += 1
        threshold = float(self.cfg.int("long_task_threshold_sec", 0, 120))
        if not self.cfg.bool("long_task_notice") or threshold <= 0:
            answer = await self._ask(question, device_id)
            if record and answer:
                await self.sync.record(question, answer)
            return answer

        task = asyncio.create_task(self._ask(question, device_id))
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=threshold)
        except asyncio.TimeoutError:
            bg = asyncio.create_task(self._finish_later(task, question, record))
            self._bg_tasks.add(bg)
            bg.add_done_callback(self._bg_tasks.discard)
            return "稍等我一下"

    def _session_for(self, device_id: str = "") -> str:
        base = self.cfg.device_session_id
        return f"{base}-{device_id}" if device_id else base

    async def _ask(self, question: str, device_id: str = "") -> str:
        try:
            answer = await ask_astrbot(
                self.cfg.base_url,
                self.cfg.api_key,
                question,
                self._session_for(device_id),
                self.cfg.sender_id or "哥哥",
                self.cfg.int("request_timeout", 10, 600),
            )
            self.last_error = ""
            return answer or "嗯，我在。"
        except ChatError as exc:
            self.last_error = str(exc)[:160]
            logger.warning("[xiaozhi_bridge] 对话失败：%s", exc)
            return "嗯，我这边有点问题，等下再说。"

    async def _finish_later(self, task: asyncio.Task, question: str, record: bool) -> None:
        """占位之后等真正的结果，期间按间隔报进度，完成后推送并补写记录。"""
        remind = float(self.cfg.int("long_task_remind_sec", 10, 600))
        while True:
            try:
                answer = await asyncio.wait_for(asyncio.shield(task), timeout=remind)
                break
            except asyncio.TimeoutError:
                if self.cfg.bool("push_to_wechat"):
                    await self.pusher.send("还在弄 稍等一下")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[xiaozhi_bridge] 长任务失败：%s", exc)
                return

        text = str(answer or "").strip()
        if not text:
            return
        if record:
            await self.sync.record(question, text)
        await self._push_pair(question, text)

    async def _push_pair(self, question: str, answer: str) -> None:
        """推送到目标会话：按配置决定是否带原话，静音时段跳过。"""
        if not self.cfg.bool("push_to_wechat"):
            return
        if self.state.get("push_enabled") is False:
            return
        if self.cfg.in_quiet_hours():
            return
        try:
            if self.cfg.bool("push_with_question"):
                head = self.cfg.str("push_bot_tag", "【设备】") + question
                tail = self.cfg.str("push_user_tag", "【我】") + answer
                text = f"{head}\n{tail}"
            else:
                text = answer
            await self.pusher.send(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[xiaozhi_bridge] 推送失败：%s", exc)

    # ================= 其余三个工具 =================
    async def _tool_send(self, args: dict) -> str:
        text = str(args.get("text") or "").strip()
        if not text:
            return "要发送的内容是空的"
        return await self.pusher.send(text)

    async def _tool_music(self, args: dict) -> str:
        if not self.cfg.bool("music_enable"):
            return "点歌功能已关闭"
        raw = str(args.get("keyword") or "").strip()
        keyword = extract_keyword(raw) or raw
        if not keyword:
            return "要点什么歌？"
        song = await search(self.context, keyword)
        if not song:
            return "没找到这首歌，或者点歌插件没装"
        name = str(song.get("name") or song.get("title") or keyword)
        url = str(song.get("url") or song.get("audio") or "")
        lines = [f"找到《{name}》"]
        if self.cfg.bool("music_send_file"):
            path = await download(song, self.cfg.int("music_cache_ttl", 60, 86400))
            if path:
                await self.pusher.send_file(path)
        if self.cfg.bool("music_reply_with_url") and url:
            lines.append(url)
        return "\n".join(lines)

    async def _tool_status(self, args: dict) -> str:
        info = self.status_payload()
        state = "在线" if info["connected"] else "离线"
        return (
            f"连接：{state}｜模式：{info['mode']}｜今日：{info['count']}"
            f"｜最近错误：{info['last_error'] or '无'}"
        )

    # ================= 指令 =================
    @filter.command("xiaozhi")
    async def xiaozhi_cmd(self, event: AstrMessageEvent, arg: str = ""):
        """主指令：status / push on / push off / reconnect。"""
        sub = (arg or "").strip().lower()
        if sub in ("status", ""):
            info = self.status_payload()
            state = "在线" if info["connected"] else "离线"
            push = "开" if info["push"] else "关"
            lines = [
                f"连接：{state}",
                f"模式：{info['mode']}｜今日 {info['count']} 次",
                f"推送：{push}｜会话：{info['session']}",
                f"最近错误：{info['last_error'] or '无'}",
            ]
            yield event.plain_result("\n".join(lines))
            return
        if sub == "push on":
            self.state.update(push_enabled=True)
            yield event.plain_result("推送已开启")
            return
        if sub == "push off":
            self.state.update(push_enabled=False)
            yield event.plain_result("推送已关闭")
            return
        if sub == "reconnect":
            if not self.mcp:
                self._start_mcp()
            else:
                await self.mcp.reconnect()
            yield event.plain_result("已发起重连")
            return
        yield event.plain_result("用法：/xiaozhi status｜push on｜push off｜reconnect")

    @filter.command("小智推送")
    async def legacy_push(self, event: AstrMessageEvent, arg: str = ""):
        """兼容旧入口：/小智推送 开|关|状态。"""
        sub = (arg or "状态").strip()
        if sub in ("开", "on"):
            self.state.update(push_enabled=True)
            yield event.plain_result("推送已开启")
        elif sub in ("关", "off"):
            self.state.update(push_enabled=False)
            yield event.plain_result("推送已关闭")
        else:
            info = self.status_payload()
            yield event.plain_result(f"推送：{'开' if info['push'] else '关'}")

    # ================= 状态与视图接口 =================
    def status_payload(self) -> dict:
        connected = bool(self.mcp and self.mcp.online)
        mcp_err = self.mcp.last_error if self.mcp else ""
        diag = [
            {
                "name": "Token",
                "ok": bool(self.cfg.token),
                "msg": "已填写" if self.cfg.token else "没填",
                "action": "" if self.cfg.token else "去插件配置填小智接入点 Token",
            },
            {
                "name": "连接",
                "ok": connected,
                "msg": "在线" if connected else (mcp_err or "未启动"),
                "action": "" if connected else "检查网络或点重新连接",
            },
            {
                "name": "身份",
                "ok": bool(self.cfg.sender_id),
                "msg": self.cfg.sender_id or "未设置",
                "action": "" if self.cfg.sender_id else "去插件配置选发送人身份",
            },
            {
                "name": "推送目标",
                "ok": bool(self.cfg.push_umo),
                "msg": self.cfg.push_umo or "未设置",
                "action": "" if self.cfg.push_umo else "去插件配置选推送目标",
            },
        ]
        level = "green" if all(d["ok"] for d in diag) else "yellow"
        if not self.cfg.token:
            level = "red"
        return {
            "level": level,
            "connected": connected,
            "mode": self.state.get("mode") or "idle",
            "push": self.state.get("push_enabled", True) is not False,
            "session": self.cfg.device_session_id,
            "sender": self.cfg.sender_id,
            "target": self.cfg.push_umo,
            "trigger_words": self.cfg.trigger_words,
            "count": self.count_today,
            "uptime": int(time.time() - self.started_at),
            "last_error": self.last_error or mcp_err,
            "quiet_hours": self.cfg.str("quiet_hours"),
            "diag": diag,
        }

    def _register_views(self) -> None:
        """注册 5 条视图接口，路由前缀必须是插件名。"""
        from astrbot.api.web import request as web_request

        async def _status():
            return json_response(self.status_payload())

        async def _toggle():
            try:
                data = await web_request.json(default={})
            except Exception:  # noqa: BLE001
                return error_response("请求体必须是 JSON")
            name = str((data or {}).get("name") or "")
            if name not in ("push", "trigger"):
                return error_response("只能切换 push 或 trigger")
            enabled = bool((data or {}).get("enabled"))
            if name == "push":
                self.state.update(push_enabled=enabled)
            else:
                mode = "persona" if enabled else "idle"
                self.state.update(mode=mode, mode_since=time.time())
            return json_response({"ok": True, "name": name, "enabled": enabled})

        async def _quiet():
            try:
                data = await web_request.json(default={})
            except Exception:  # noqa: BLE001
                return error_response("请求体必须是 JSON")
            span = str((data or {}).get("quiet_hours") or "").strip()
            if span and "-" not in span:
                return error_response("静音时段要写成 23:30-07:30")
            try:
                # 配置只通过注入的 AstrBotConfig 写，保存走它自己的 save_config
                target = self.astrbot_config
                if hasattr(target, "save_config"):
                    target.save_config({"quiet_hours": span})
                else:
                    target["quiet_hours"] = span
                self.cfg = Settings(target)
                return json_response({"ok": True, "quiet_hours": span})
            except Exception as exc:  # noqa: BLE001
                logger.warning("[xiaozhi_bridge] 保存静音时段失败：%s", exc)
                return error_response(f"保存失败：{exc}")

        async def _reconnect():
            try:
                if not self.mcp:
                    self._start_mcp()
                else:
                    await self.mcp.reconnect()
                return json_response({"ok": True})
            except Exception as exc:  # noqa: BLE001
                return error_response(f"重连失败：{exc}")

        async def _cleanup():
            try:
                days = self.cfg.int("music_keep_days", 1, 365)
                limit = self.cfg.int("music_max_mb", 10, 100000)
                removed, freed = cleanup(days, limit)
                freed_mb = round(freed / 1048576, 2)
                return json_response({"ok": True, "removed": removed, "freed_mb": freed_mb})
            except Exception as exc:  # noqa: BLE001
                return error_response(f"清理失败：{exc}")

        routes = [
            ("status", _status, ["GET"], "桥接状态"),
            ("toggle", _toggle, ["POST"], "切换推送与召唤"),
            ("quiet", _quiet, ["POST"], "保存静音时段"),
            ("reconnect", _reconnect, ["POST"], "重新连接"),
            ("cleanup", _cleanup, ["POST"], "清理点歌缓存"),
        ]
        for path, handler, methods, desc in routes:
            try:
                self.context.register_web_api(DATA_PREFIX + path, handler, methods, desc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[xiaozhi_bridge] 注册接口 %s 失败：%s", path, exc)

    # ================= 缓存清理循环 =================
    async def _cleanup_loop(self) -> None:
        hours = self.cfg.int("music_cleanup_hours", 1, 168)
        while True:
            try:
                await asyncio.sleep(hours * 3600)
                days = self.cfg.int("music_keep_days", 1, 365)
                limit = self.cfg.int("music_max_mb", 10, 100000)
                removed, freed = cleanup(days, limit)
                if removed:
                    logger.info(
                        "[xiaozhi_bridge] 清理缓存 %d 个，释放 %.1f MB", removed, freed / 1048576
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[xiaozhi_bridge] 清理循环异常：%s", exc)
