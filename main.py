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
_kb = _load_sibling("kb_memory")

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
kb_match = _kb.match_remember
kb_save = _kb.save

DATA_PREFIX = f"/{PLUGIN_NAME}/"


@register(
    PLUGIN_NAME,
    "轩宇(xuanyu)",
    "把小智（xiaozhi.me / ESP32）接入 AstrBot，设备说话由 AstrBot 的人格回答",
    "2.1.2",
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
        self._watch_task = None
        self._bg_tasks: set[asyncio.Task] = set()

    # ================= 生命周期 =================
    async def initialize(self) -> None:
        # 开关只在第一次写入默认值，之后以状态文件里的为准，
        # 避免每次重载都把你手动关掉的推送又打开
        if self.state.get("push_enabled") is None:
            self.state.update(push_enabled=self.cfg.bool("push_to_wechat"))
        self._register_views()
        self._start_mcp()
        self._watch_task = asyncio.create_task(self._watchdog_loop())
        asyncio.create_task(self._import_notes())
        if self.cfg.bool("music_cleanup_enable"):
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("[xiaozhi_bridge] 插件就绪，MCP 目标 %s", self._mask(self.cfg.token))

    async def terminate(self) -> None:
        """重载前必须收干净：取消后台任务、关连接，避免残留。"""
        for task in [self._cleanup_task, self._watch_task, *self._bg_tasks]:
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

    async def _import_notes(self) -> None:
        """把 plugin_data/notes/*.md 里的约定文档导入知识库，换机器也能带走。

        用文件内容哈希做指纹，内容没变就不再重复写入。
        """
        try:
            import hashlib
            import os as _os

            folder = _os.path.join(data_dir(), "notes")
            if not _os.path.isdir(folder):
                return
            for name in sorted(_os.listdir(folder)):
                if not name.endswith(".md"):
                    continue
                path = _os.path.join(folder, name)
                try:
                    body = open(path, encoding="utf-8").read()
                except Exception:  # noqa: BLE001
                    continue
                digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
                key = f"notes_{name}"
                if self.state.get(key) == digest:
                    continue
                saved = await kb_save(self.context, self.cfg, body.strip())
                if saved:
                    self.state.set(key, digest)
                    logger.info("[xiaozhi_bridge] 约定文档已入知识库：%s", name)
                else:
                    logger.warning("[xiaozhi_bridge] 约定文档未能入知识库（可能没有知识库）：%s", name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[xiaozhi_bridge] 导入约定文档失败：%s", exc)

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

        # 「记住…」这类话先写知识库，再照常回答
        remembered = ""
        if self.cfg.bool("kb_enable"):
            content = kb_match(text, self.cfg.list("kb_remember_words"))
            if content:
                remembered = await kb_save(self.context, self.cfg, content)

        answer = await self._answer(text, device_id, record=True)
        if remembered:
            return f"记住了"
        return answer

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
            await self._push_pair(question, answer)
            return answer

        task = asyncio.create_task(self._ask(question, device_id))
        try:
            answer = await asyncio.wait_for(asyncio.shield(task), timeout=threshold)
            # 阈值内答完：这是最常见的路径，必须写记录并推送，否则微信收不到
            if record and answer:
                await self.sync.record(question, answer)
            await self._push_pair(question, answer)
            return answer
        except asyncio.TimeoutError:
            bg = asyncio.create_task(self._finish_later(task, question, record))
            self._bg_tasks.add(bg)
            bg.add_done_callback(self._bg_tasks.discard)
            return "稍等我一下"

        # 快速回答也要补写记录 + 推送，否则设备对话不会进微信
        if record and answer:
            try:
                await self.sync.record(question, answer)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[xiaozhi_bridge] 补写记录失败：%s", exc)
        await self._push_pair(question, answer)
        return answer

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
                self.cfg.int("reply_max_chars", 40, 2000),
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
        """推送一轮问答。

        唯一的开关是视图里那个「推送对话到微信」：
        开着就每次都推，关着就一次都不推。静音时段是额外的过滤器。
        """
        if self.state.get("push_enabled", True) is False:
            self.pusher.last_result = "推送开关已关闭"
            logger.info("[xiaozhi_bridge] 推送开关关着，跳过")
            return
        if not self.cfg.push_umo:
            self.pusher.last_result = "没有推送目标"
            return
        if self.cfg.in_quiet_hours():
            self.pusher.last_result = "处于静音时段"
            logger.info("[xiaozhi_bridge] 静音时段，跳过推送")
            return

        try:
            if self.cfg.bool("push_with_question"):
                head = self.cfg.str("push_bot_tag", "【设备】") + question
                tail = self.cfg.str("push_user_tag", "【我】") + answer
                text = f"{head}\n{tail}"
            else:
                text = answer
            result = await self.pusher.send(text)
            logger.info("[xiaozhi_bridge] 推送结果：%s", result)
        except Exception as exc:  # noqa: BLE001
            self.pusher.last_result = f"异常：{exc}"[:120]
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
        # 配置可能比插件启动晚一步（面板先加载、后填 Token），这里补一次自动启动
        if self.mcp is None and self.cfg.token:
            self._start_mcp()
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
            "push_last": {
                "result": getattr(self.pusher, "last_result", ""),
                "at": getattr(self.pusher, "last_at", 0.0),
                "count": getattr(self.pusher, "count", 0),
            },
            "rpc_stats": (self.mcp.rpc_stats if self.mcp else {}),
            "last_method": (self.mcp.last_method if self.mcp else ""),
            "token_masked": self._mask(self.cfg.token),
            "endpoint": self.mcp.url if self.mcp else "",
            "connected_at": int(self.mcp.connected_at) if self.mcp else 0,
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

        async def _testpush():
            """发一条测试消息，用来确认推送链路通不通。"""
            try:
                # 临时放开节流，测试要能立刻看到
                self.pusher._last_text = ""
                self.pusher._last_at = 0.0
                result = await self.pusher.send("小智桥接：这是一条测试推送")
                ok = result == "已发送"
                return json_response({"ok": ok, "result": result})
            except Exception as exc:  # noqa: BLE001
                logger.warning("[xiaozhi_bridge] 测试推送失败：%s", exc)
                return error_response(f"测试推送失败：{exc}")

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

        async def _settings():
            """返回可编辑的设置项，密钥只给打码后的样子。"""
            conf = dict(self.astrbot_config or {})
            hidden = {"xiaozhi_token"}
            safe = {k: v for k, v in conf.items() if not k.endswith("_key") and k not in hidden}
            token = str(conf.get("xiaozhi_token") or "")
            safe["xiaozhi_token_masked"] = f"…{token[-6:]}" if token else ""
            return json_response({
                "settings": safe,
                "editable": [
                    "reply_max_chars", "long_task_threshold_sec", "long_task_remind_sec",
                    "quiet_hours", "push_to_wechat", "push_with_question", "mirror_enable",
                    "context_seed_enable", "context_seed_messages", "context_max_messages",
                    "require_trigger", "trigger_words", "kb_enable", "music_enable",
                ],
            })

        async def _settings_save():
            try:
                data = await web_request.json(default={})
            except Exception:  # noqa: BLE001
                return error_response("请求体必须是 JSON")
            if not isinstance(data, dict):
                return error_response("请求体必须是对象")
            allowed = {
                "reply_max_chars", "long_task_threshold_sec", "long_task_remind_sec",
                "quiet_hours", "push_to_wechat", "push_with_question", "mirror_enable",
                "context_seed_enable", "context_seed_messages", "context_max_messages",
                "require_trigger", "trigger_words", "kb_enable", "music_enable",
            }
            patch = {k: v for k, v in data.items() if k in allowed}
            if not patch:
                return error_response("没有可保存的字段")
            try:
                self.astrbot_config.save_config(patch)
                self.cfg = Settings(self.astrbot_config)
                return json_response({"ok": True, "changed": sorted(patch)})
            except Exception as exc:  # noqa: BLE001
                logger.warning("[xiaozhi_bridge] 保存设置失败：%s", exc)
                return error_response(f"保存失败：{exc}")

        async def _export():
            """把整套配置导成 JSON，换机器时用它恢复。

            密钥不外泄：token / api_key 一律导出成空串，导入时留空就沿用本机的，
            这样导出的文件可以随手存云盘，不会带着凭据到处跑。
            """
            try:
                secret_keys = {"xiaozhi_token", "astrbot_api_key"}
                conf = {
                    k: ("" if k in secret_keys else v)
                    for k, v in dict(self.astrbot_config or {}).items()
                }
                payload = {"plugin": PLUGIN_NAME, "version": "2.1.0", "config": conf}
                payload["notice"] = "密钥未导出，导入后请重新填 Token 与 API Key"
                return json_response(payload)
            except Exception as exc:  # noqa: BLE001
                return error_response(f"导出失败：{exc}")

        async def _import():
            """导入设置：密钥字段为空则保留本机的，避免把已有配置清空。"""
            try:
                data = await web_request.json(default={})
            except Exception:  # noqa: BLE001
                return error_response("请求体必须是 JSON")
            conf = (data or {}).get("config") if isinstance(data, dict) else None
            if not isinstance(conf, dict):
                return error_response("缺少 config 字段")

            patch = {}
            for key, value in conf.items():
                if key in ("xiaozhi_token", "astrbot_api_key") and not str(value or "").strip():
                    continue          # 留空表示沿用本机已填的
                patch[key] = value

            try:
                self.astrbot_config.save_config(patch)
                self.cfg = Settings(self.astrbot_config)
                return json_response({"ok": True, "keys": len(patch)})
            except Exception as exc:  # noqa: BLE001
                logger.warning("[xiaozhi_bridge] 导入设置失败：%s", exc)
                return error_response(f"导入失败：{exc}")

        routes = [
            ("status", _status, ["GET"], "桥接状态"),
            ("settings", _settings, ["GET"], "读设置"),
            ("settings/save", _settings_save, ["POST"], "存设置"),
            ("export", _export, ["GET"], "导出设置"),
            ("import", _import, ["POST"], "导入设置"),
            ("toggle", _toggle, ["POST"], "切换推送与召唤"),
            ("quiet", _quiet, ["POST"], "保存静音时段"),
            ("reconnect", _reconnect, ["POST"], "重新连接"),
            ("testpush", _testpush, ["POST"], "测试推送"),
            ("cleanup", _cleanup, ["POST"], "清理点歌缓存"),
        ]
        for path, handler, methods, desc in routes:
            try:
                self.context.register_web_api(DATA_PREFIX + path, handler, methods, desc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[xiaozhi_bridge] 注册接口 %s 失败：%s", path, exc)

    async def _watchdog_loop(self) -> None:
        """每 60 秒看一眼：配置里有 Token 但 MCP 没跑，就自动拉起。

        解决「插件启动早于用户填 Token」的问题，也兜住意外退出。
        """
        while True:
            try:
                await asyncio.sleep(60)
                if self.mcp is None and self.cfg.token:
                    logger.info("[xiaozhi_bridge] 看门狗：MCP 未运行，自动拉起")
                    self._start_mcp()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[xiaozhi_bridge] 看门狗异常：%s", exc)

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
