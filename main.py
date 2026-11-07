# -*- coding: utf-8 -*-
"""小智设备桥接插件（免部署 · 填 token 即用）。

别人拿到这个插件后，只要在插件配置里填上自己的小智 MCP 接入点 token，
启动即可让「小智」说话时走的其实是本机 AstrBot 里的人格。

对外能力：
  1. MCP over WebSocket 客户端（连 xiaozhi.me 接入点）
  2. 工具：ask_pengyuhan / send_wechat_message / list_astrbot_bots
  3. 本机小接口：/health、/device/chat、/device/record、/device/reload-config
  4. 可选：把设备对话补写进指定微信会话历史
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json

import httpx
import os
import re
import secrets
import shutil
import sqlite3
import sys
import time
import uuid

from aiohttp import web

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Plain
from astrbot.core.platform.astrbot_message import (
    AstrBotMessage,
    MessageMember,
    MessageType,
)

# 兼容两种加载方式（包内相对导入 / 直接模块导入）
try:
    from .astrbot_client import ask_self, ask_self_stream, sanitize
    from .mcp_client import XiaoZhiMCPClient, build_ws_url
except ImportError:  # pragma: no cover
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in sys.path:
        sys.path.insert(0, _here)
    from astrbot_client import ask_self, ask_self_stream, sanitize  # type: ignore
    from mcp_client import XiaoZhiMCPClient, build_ws_url  # type: ignore

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.getenv("ASTRBOT_DB", "/AstrBot/data/data_v4.db")
REPLY_TIMEOUT = 120

CONFIG_PAGE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>小智插件配置</title>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
       background:#f5f5f3;color:#1a1a19;margin:0;padding:16px;line-height:1.6}
  .card{max-width:640px;margin:0 auto 14px;background:#fcfcfb;border-radius:14px;padding:16px 18px;
        box-shadow:0 1px 3px rgba(0,0,0,.06)}
  h1{font-size:18px;margin:0 0 4px}
  .sub{color:#6b6b68;font-size:13px;margin-bottom:14px}
  label{display:block;font-size:14px;font-weight:600;margin:14px 0 6px}
  .req{color:#c0392b}
  input,select{width:100%;box-sizing:border-box;padding:10px 12px;border:1px solid #e2e2df;
        border-radius:10px;background:#fff;font-size:14px;outline:none}
  input:focus,select:focus{border-color:#1a1a19;box-shadow:0 0 0 3px rgba(26,26,25,.08)}
  .hint{color:#8a8a86;font-size:12px;margin-top:6px}
  button{width:100%;padding:12px;border:0;border-radius:12px;background:#1a1a19;color:#fff;
         font-size:15px;font-weight:600;margin-top:18px}
  button:disabled{opacity:.5}
  details{margin-top:12px}
  summary{cursor:pointer;font-size:14px;color:#4a4a47;padding:8px 0;user-select:none}
  .row{display:flex;gap:10px;align-items:center;margin-top:10px}
  .row input[type=checkbox]{width:18px;height:18px}
  .msg{margin-top:12px;font-size:13px}
  .ok{color:#1e7a45}.err{color:#c0392b}
  .err-border{border-color:#c0392b !important}
</style>
</head>
<body>
<div class="card">
  <h1>小智插件</h1>
  <div class="sub">只填两项就能用，其余保持默认。</div>

  <label>小智接入点 Token <span class="req">*</span></label>
  <input id="token" placeholder="eyJhbGciOi... 或者 wss://api.xiaozhi.me/mcp/?token=...">
  <div class="hint">小智控制台 → 智能体 → MCP 接入点，复制过来。</div>

  <label>发送人身份 <span class="req">*</span></label>
  <select id="uid"><option value="">加载中…</option></select>
  <div class="hint">微信和小智只是渠道，选同一个身份，两边就归到一个人。</div>

  <details>
    <summary>展开高级设置</summary>

    <div class="row">
      <input type="checkbox" id="push">
      <label style="margin:0;font-weight:500">把对话推送到微信</label>
    </div>

    <div class="row">
      <input type="checkbox" id="trigger">
      <label style="margin:0;font-weight:500">只在被召唤时转接（说名字才回）</label>
    </div>

    <label>推送标签</label>
    <input id="tag" placeholder="【小智】">

    <label>设备会话标识</label>
    <input id="sid" placeholder="xiaozhi-wx">
    <div class="hint">保持默认即可，改了会另起一段记忆。</div>

    <label>多久没说话自动退出（分钟）</label>
    <input id="idle" type="number" min="0" placeholder="30">
  </details>

  <button id="save">保存</button>
  <div id="msg" class="msg"></div>
</div>

<script>
const $ = id => document.getElementById(id);
let state = {};

async function load(){
  const r = await fetch('/device/users').then(r=>r.json()).catch(()=>null);
  const sel = $('uid');
  sel.innerHTML = '';
  if(!r || !r.users || !r.users.length){
    sel.innerHTML = '<option value="">（没读到用户，先给机器人发条消息）</option>';
    return;
  }
  const cur = r.current || '';
  for(const u of r.users){
    const o = document.createElement('option');
    o.value = u.uid;
    o.textContent = u.label + (u.last ? '　最近 ' + u.last.slice(5,16) : '');
    if(u.uid === cur) o.selected = true;
    sel.appendChild(o);
  }
  if(cur && ![...sel.options].some(o=>o.value===cur)){
    const o = document.createElement('option');
    o.value = cur; o.textContent = cur; o.selected = true;
    sel.insertBefore(o, sel.firstChild);
  }
}

async function loadCfg(){
  try{
    const c = await fetch('/device/config').then(r=>r.json());
    $('token').placeholder = c.has_token ? '已保存（要更换就粘贴新的，留空不改）' : 'eyJhbGciOi... 或 wss://...';
    $('push').checked = !!c.push_to_wechat;
    $('trigger').checked = !!c.require_trigger;
    $('tag').value = c.push_tag || '【小智】';
    $('sid').value = c.astrbot_session_id || 'xiaozhi-wx';
    $('idle').value = c.trigger_idle_minutes ?? 30;
  }catch(e){}
}

$('save').onclick = async () => {
  const token = $('token').value.trim();
  const uid = $('uid').value;
  const hasToken = $('token').placeholder.indexOf('已保存') === 0;
  let bad = false;
  const tokenOk = token || hasToken;
  if(!tokenOk){ $('token').classList.add('err-border'); bad = true; } else $('token').classList.remove('err-border');
  if(!uid){ $('uid').classList.add('err-border'); bad = true; } else $('uid').classList.remove('err-border');
  if(bad){ $('msg').className='msg err'; $('msg').textContent='带 * 的必填项还没填'; return; }

  const body = {
    xiaozhi_token: token,
    unified_user_id: uid,
    push_to_wechat: $('push').checked,
    require_trigger: $('trigger').checked,
    push_tag: $('tag').value.trim() || '【小智】',
    astrbot_session_id: $('sid').value.trim() || 'xiaozhi-wx',
    trigger_idle_minutes: parseInt($('idle').value || '30', 10)
  };
  $('save').disabled = true;
  $('msg').className='msg'; $('msg').textContent='保存中…';
  try{
    const r = await fetch('/device/config', {method:'POST', headers:{'Content-Type':'application/json'},
                      body: JSON.stringify(body)}).then(r=>r.json());
    if(r.ok){
      $('msg').className='msg ok';
      $('msg').textContent = '已保存。到 AstrBot 面板把插件重载一次即可生效。';
    } else {
      $('msg').className='msg err'; $('msg').textContent = r.error || '保存失败';
    }
  }catch(e){
    $('msg').className='msg err'; $('msg').textContent = '网络错误：' + e;
  }
  $('save').disabled = false;
};

load(); loadCfg();
</script>
</body>
</html>"""



# 同一进程内可能有多份插件实例（重载时旧的没退出），
# 用这个全局变量让新实例把旧实例的本机接口关掉，避免端口被占、旧代码继续响应。
_LIVE_INSTANCE = None
_GEN_FILE = "/tmp/astrbot_device_bridge_gen.txt"


@register(
    "astrbot_plugin_device_bridge",
    "轩宇(xuanyu)",
    "把小智 ESP32 接入 AstrBot：填 token 即用，共用人格、工具与记忆",
    "v1.6.13",
)
class DeviceBridge(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.cfg = config or {}
        self._lock = asyncio.Lock()
        self._runner: web.AppRunner | None = None
        self._http_task: asyncio.Task | None = None
        self._api_key = ""
        self._mcp: XiaoZhiMCPClient | None = None
        self._local_port = int(self.cfg.get("local_port") or 8765)
        self._last_activity = 0.0
        self._music_platform = ""
        self._music_send_file = True
        self._song_cache: dict[str, tuple[float, object]] = {}   # 关键词 -> (时间, Song)
        self._url_cache: dict[str, tuple[float, str]] = {}        # 歌曲id -> (时间, 直链)
        self._bg_tasks: set = set()
        self._cleanup_task: asyncio.Task | None = None
        self._metrics: dict = {}
        self._started_at: float = 0.0
        self._last_ok_at: float = 0.0
        self._last_error: str = ""
        self._extra_tools: dict = {}

    # ---------- 推送开关：配置默认值 + 运行时覆盖 ----------
    def _data_dir(self) -> str:
        """AstrBot 规定的插件数据目录：data/plugin_data/<plugin_name>/"""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            base = os.path.join(get_astrbot_data_path(), "plugin_data", "astrbot_plugin_device_bridge")
        except Exception:  # noqa: BLE001
            base = "/AstrBot/data/plugin_data/astrbot_plugin_device_bridge"
        os.makedirs(base, exist_ok=True)
        return base

    def _state_path(self) -> str:
        path = os.path.join(self._data_dir(), "state.json")
        # 旧版本写在插件目录里，第一次启动时自动搬过来
        legacy = os.path.join(PLUGIN_DIR, "state.json")
        if not os.path.exists(path) and os.path.exists(legacy):
            try:
                shutil.copy2(legacy, path)
                logger.info("[device_bridge] 已把 state.json 迁到 %s", path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[device_bridge] state.json 迁移失败：%s", exc)
        return path

    def _load_state(self) -> dict:
        try:
            if os.path.exists(self._state_path()):
                return json.load(open(self._state_path(), encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _save_state(self, state: dict) -> None:
        try:
            with open(self._state_path(), "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[device_bridge] 保存状态失败：%s", exc)

    def _push_enabled(self) -> bool:
        state = self._load_state()
        if "push_enabled" in state:
            return bool(state["push_enabled"])
        return bool(self.cfg.get("push_to_wechat", True))

    def _set_push_enabled(self, enabled: bool) -> None:
        state = self._load_state()
        state["push_enabled"] = bool(enabled)
        self._save_state(state)
        logger.info("[device_bridge] 推送开关 -> %s", "开" if enabled else "关")

    # ---------- 统一发送人身份（微信 / 小智 → 同一个人） ----------
    def _uid(self) -> str:
        """所有渠道共用的用户身份，只此一处取值。"""
        for key in ("unified_user_id", "mirror_user_id", "astrbot_username"):
            v = (self.cfg.get(key) or "").strip()
            if v:
                return v
        return ""

    def _apply_unified_identity(self) -> None:
        """把各渠道的用户 ID 归一到同一个值，避免按渠道分裂。"""
        uid = self._uid()
        if not uid:
            logger.warning("[device_bridge] 未配置任何用户 ID，身份无法统一")
            return
        changed = []
        for key in ("unified_user_id", "mirror_user_id", "astrbot_username"):
            if (self.cfg.get(key) or "").strip() != uid:
                self.cfg[key] = uid
                changed.append(key)
        if changed:
            logger.info("[device_bridge] 身份统一为 %s（已同步 %s）", uid, ", ".join(changed))
        else:
            logger.info("[device_bridge] 身份统一为 %s", uid)

        # 平台没配就自动推断：找这个人最近出现过的那个平台
        if not (self.cfg.get("mirror_platform_id") or "").strip():
            plat = self._infer_platform(uid)
            if plat:
                self.cfg["mirror_platform_id"] = plat
                self._persist_cfg({"mirror_platform_id": plat})
                logger.info("[device_bridge] 自动识别平台：%s", plat)

    def _infer_platform(self, uid: str) -> str:
        """在会话库里找这个用户最近活跃的平台（排除 webchat）。"""
        if not uid:
            return ""
        try:
            con = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
            try:
                row = con.execute(
                    "select platform_id from conversations where user_id like ? "
                    "and platform_id != 'webchat' order by updated_at desc limit 1",
                    (f"%{uid}%",),
                ).fetchone()
            finally:
                con.close()
            return (row[0] or "").strip() if row else ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("[device_bridge] 推断平台失败：%s", exc)
            return ""

    def _persist_cfg(self, patch: dict) -> None:
        """把少量配置写回配置文件，保证重启后还在。"""
        if not patch:
            return
        path = "/AstrBot/data/config/astrbot_plugin_device_bridge_config.json"
        try:
            cur = json.load(open(path, encoding="utf-8-sig")) if os.path.exists(path) else {}
            cur.update(patch)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[device_bridge] 写配置失败：%s", exc)

    # ---------- 关掉设备会话的 TTS（否则回复变音频、取不到文本）----------
    async def _disable_session_tts(self) -> None:
        """关掉设备会话的 TTS：走 astrbot 官方 preferences 接口，不直连数据库。"""
        try:
            from astrbot.api import sp

            user = self._uid()
            sid = (self.cfg.get("astrbot_session_id") or "xiaozhi-wx").strip()
            if not user:
                return
            umo = f"webchat:FriendMessage:webchat!{user}!{sid}"

            cur = await sp.get_async(scope="umo", scope_id=umo,
                                     key="session_service_config", default={}) or {}
            if isinstance(cur, str):
                try:
                    cur = json.loads(cur or "{}")
                except Exception:  # noqa: BLE001
                    cur = {}
            inner = cur.setdefault("val", {}) if isinstance(cur, dict) else {}
            if inner.get("tts_enabled") is False:
                return
            inner["tts_enabled"] = False
            await sp.put_async(scope="umo", scope_id=umo,
                               key="session_service_config", value=cur)
            logger.info("[device_bridge] 已关闭设备会话 TTS：%s", umo)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[device_bridge] 关闭会话 TTS 失败：%s", exc)

    def _config_view(self) -> dict:
        """页面回填用的配置视图（token 只报是否已填）。"""
        cp = "/AstrBot/data/config/astrbot_plugin_device_bridge_config.json"
        try:
            cur = json.load(open(cp, encoding="utf-8-sig")) if os.path.exists(cp) else {}
        except Exception:  # noqa: BLE001
            cur = {}
        return {
            "has_token": bool((cur.get("xiaozhi_token") or "").strip()),
            "unified_user_id": cur.get("unified_user_id") or self._uid(),
            "push_to_wechat": cur.get("push_to_wechat", True),
            "require_trigger": cur.get("require_trigger", True),
            "user_alias": cur.get("user_alias") or "哥哥",
            "quiet_hours": cur.get("quiet_hours") or "",
            "multi_device_enable": cur.get("multi_device_enable", False),
            "trigger_words": cur.get("trigger_words") or ["彭语涵", "语涵", "雨涵"],
            "exit_words": cur.get("exit_words") or ["退出", "不跟你说话了", "不聊了", "结束", "回去吧", "换回小智", "叫小智"],
            "push_bot_tag": cur.get("push_bot_tag") or "【小智】",
            "push_user_tag": cur.get("push_user_tag") or "【我】",
            "push_with_question": cur.get("push_with_question", True),
            "astrbot_session_id": cur.get("astrbot_session_id") or "xiaozhi-wx",
            "trigger_idle_minutes": cur.get("trigger_idle_minutes", 30),
        }

    def _save_cfg_patch(self, data: dict) -> list:
        """保存页面提交的配置，返回变更的键。"""
        cp = "/AstrBot/data/config/astrbot_plugin_device_bridge_config.json"
        try:
            cur = json.load(open(cp, encoding="utf-8-sig")) if os.path.exists(cp) else {}
        except Exception:  # noqa: BLE001
            cur = {}
        allowed = {
            "xiaozhi_token", "unified_user_id", "push_to_wechat", "require_trigger",
            "push_bot_tag", "push_user_tag", "push_with_question", "astrbot_session_id",
            "trigger_idle_minutes", "trigger_words", "exit_words", "user_alias",
            "quiet_hours", "multi_device_enable",
        }
        changed = []
        for k, v in (data or {}).items():
            if k == "xiaozhi_token" and not str(v or "").strip():
                continue
            if k in allowed and cur.get(k) != v:
                cur[k] = v
                changed.append(k)
        uid = (cur.get("unified_user_id") or "").strip()
        if uid:
            cur["mirror_user_id"] = uid
            cur["astrbot_username"] = uid
        try:
            with open(cp, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[device_bridge] 保存配置失败：%s", exc)
            return []
        if changed:
            self._apply_unified_identity()
        return changed

    def _users_payload_safe(self) -> list:
        """带兜底的用户列表，出错返回空数组，不抛。"""
        try:
            return self._users_payload()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[device_bridge] 用户列表读取失败：%s", exc)
            return []

    def _safe_diagnose(self) -> dict:
        """带兜底的自检，出错返回降级结果。"""
        try:
            return self._diagnose_data()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[device_bridge] 自检失败：%s", exc)
            return {"ok": False, "error": str(exc), "items": [], "all_ok": False,
                    "status": {"level": "red", "text": "自检异常"}}

    def _users_payload(self) -> list:
        """可选身份列表（给页面用）。"""
        try:
            con = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "select platform_id, user_id, max(updated_at) as t, count(*) as n "
                    "from conversations group by platform_id, user_id order by t desc limit 60"
                ).fetchall()
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[device_bridge] 读取用户列表失败：%s", exc)
            return []

        seen, items = set(), []
        for platform, umo, t, n in rows:
            uid = self._extract_uid(umo or "")
            if not uid or uid in seen:
                continue
            seen.add(uid)
            items.append({
                "uid": uid,
                "platform": platform or "",
                "count": n,
                "last": str(t)[:19],
                "label": self._pretty_uid(uid, platform or ""),
            })
        return items

    def _register_page_api(self) -> None:
        """给插件页面注册同源接口（面板端口即可访问）。"""
        try:
            import astrbot.api.web as webbridge

            async def _page_users(**kwargs):
              try:
                con = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
                try:
                    rows = con.execute(
                        "select platform_id, user_id, max(updated_at) as t, count(*) as n "
                        "from conversations group by platform_id, user_id order by t desc limit 60"
                    ).fetchall()
                finally:
                    con.close()

                seen, items = set(), []
                for platform, umo, t, n in rows:
                    uid = self._extract_uid(umo or "")
                    if not uid or uid in seen:
                        continue
                    seen.add(uid)
                    items.append({
                        "uid": uid,
                        "platform": platform or "",
                        "count": n,
                        "last": str(t)[:19],
                        "label": self._pretty_uid(uid, platform or ""),
                    })
                return webbridge.json_response(
                    {"ok": True, "current": self._uid(), "users": items}
                )
              except Exception as exc:
                logger.warning("[device_bridge] page/users 失败：%s", exc)
                return webbridge.json_response({"ok": False, "error": f"users: {exc}", "users": []})

            async def _page_config(**kwargs):
                cp = "/AstrBot/data/config/astrbot_plugin_device_bridge_config.json"
                try:
                    cur = json.load(open(cp, encoding="utf-8-sig")) if os.path.exists(cp) else {}
                except Exception:  # noqa: BLE001
                    cur = {}
                if getattr(webbridge.request, "method", "GET").upper() == "POST":
                    try:
                        data = await webbridge.request.json(default={}) or {}
                    except Exception:  # noqa: BLE001
                        data = {}
                    allowed = {
                        "xiaozhi_token", "unified_user_id", "push_to_wechat",
                        "require_trigger", "push_bot_tag", "push_user_tag",
                        "push_with_question", "astrbot_session_id", "mirror_enable",
                        "trigger_idle_minutes", "trigger_words", "exit_words",
                        "user_alias", "quiet_hours", "multi_device_enable", "metrics_enable",
                    }
                    changed = []
                    for k, v in data.items():
                        if k == "xiaozhi_token" and not str(v or "").strip():
                            continue
                        if k in allowed and cur.get(k) != v:
                            cur[k] = v
                            changed.append(k)
                    uid = (cur.get("unified_user_id") or "").strip()
                    if uid:
                        cur["mirror_user_id"] = uid
                        cur["astrbot_username"] = uid
                    with open(cp, "w", encoding="utf-8") as f:
                        json.dump(cur, f, ensure_ascii=False, indent=2)
                    logger.info("[device_bridge] 页面保存配置：%s", ", ".join(changed) or "无变化")
                    if changed:
                        self._apply_unified_identity()
                        import asyncio as _aio
                        _aio.create_task(self._reload_self())
                    return webbridge.json_response({"ok": True, "changed": changed, "auto_reload": bool(changed)})

                return webbridge.json_response({
                    "ok": True,
                    "has_token": bool((cur.get("xiaozhi_token") or "").strip()),
                    "unified_user_id": cur.get("unified_user_id") or self._uid(),
                    "push_to_wechat": cur.get("push_to_wechat", True),
                    "require_trigger": cur.get("require_trigger", True),
                    "push_bot_tag": cur.get("push_bot_tag") or cur.get("push_tag") or "【小智】",
                    "push_user_tag": cur.get("push_user_tag") or "【我】",
                    "push_with_question": cur.get("push_with_question", True),
                    "astrbot_session_id": cur.get("astrbot_session_id") or "xiaozhi-wx",
                    "trigger_idle_minutes": cur.get("trigger_idle_minutes", 30),
                    "trigger_words": cur.get("trigger_words") or ["彭语涵", "语涵", "雨涵"],
                    "exit_words": cur.get("exit_words") or ["退出", "不跟你说话了", "不聊了", "结束"],
                    "user_alias": cur.get("user_alias") or "哥哥",
                    "quiet_hours": cur.get("quiet_hours") or "",
                    "multi_device_enable": cur.get("multi_device_enable", False),
                    "metrics_enable": cur.get("metrics_enable", True),
                    "mirror_enable": cur.get("mirror_enable", True),
                    "users": self._users_payload_safe(),
                    "diag": self._safe_diagnose(),
                })

            async def _page_diagnose(**kwargs):
                try:
                    return webbridge.json_response(self._diagnose_data())
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[device_bridge] page/diagnose 失败：%s", exc)
                    return webbridge.json_response({
                        "ok": False, "error": f"diagnose: {exc}",
                        "status": {"level": "red", "text": "自检异常"},
                        "items": [], "all_ok": False,
                    })

            async def _page_mode(**kwargs):
                method = getattr(webbridge.request, "method", "GET").upper()
                if method == "POST":
                    try:
                        d = await webbridge.request.json(default={}) or {}
                    except Exception:  # noqa: BLE001
                        d = {}
                    name = str(d.get("name") or "")
                    if name == "push":
                        self._set_push_enabled(bool(d.get("enabled")))
                    elif name == "trigger":
                        self._set_trigger_enabled(bool(d.get("enabled")))
                    elif isinstance(d.get("save"), dict):
                        self._save_cfg_patch(d["save"])
                return webbridge.json_response({
                    "ok": True,
                    "push": self._push_enabled(),
                    "trigger": self._trigger_enabled(),
                    "alias": self._alias(),
                    "quiet_hours": (self.cfg.get("quiet_hours") or "").strip(),
                    "in_quiet": self._in_quiet(),
                    "multi_device": bool(self.cfg.get("multi_device_enable", False)),
                    "metrics": self._metrics_snapshot(),
                    # 一并带回配置与身份，页面只需一个接口
                    "config": self._config_view(),
                    "users": self._users_payload_safe(),
                    "diag": self._safe_diagnose(),
                })

            async def _page_logs(**kwargs):
                return webbridge.json_response({"ok": True, "lines": self._log_lines(50)})

            async def _page_reconnect(**kwargs):
                """页面上的「重新连接」按钮。"""
                try:
                    if self._mcp is not None:
                        await self._mcp.stop()
                    token = (self.cfg.get("xiaozhi_token") or "").strip()
                    if not token:
                        return webbridge.json_response({"ok": False, "error": "还没填 Token"})
                    self._mcp = XiaoZhiMCPClient(
                        self.cfg, ask_fn=self._ask, mirror_fn=self._mirror,
                        send_fn=self._send_wechat, bots_fn=self._list_bots,
                        play_fn=self._play_music,
                    )
                    self._mcp.start()
                    return webbridge.json_response({"ok": True, "msg": "已发起重连"})
                except Exception as exc:  # noqa: BLE001
                    return webbridge.json_response({"ok": False, "error": str(exc)})

            routes = [
                ("/page/diagnose", _page_diagnose, ["GET"], "小智插件：自检"),
                ("/page/reconnect", _page_reconnect, ["POST"], "小智插件：重新连接"),
                ("/page/mode", _page_mode, ["GET", "POST"], "小智插件：模式切换"),
                ("/page/logs", _page_logs, ["GET"], "小智插件：运行日志"),
                ("/page/users", _page_users, ["GET"], "小智插件：可选身份"),
                ("/page/config", _page_config, ["GET", "POST"], "小智插件：读取/保存配置"),
                ("/astrbot_plugin_device_bridge/page/users", _page_users, ["GET"], "小智插件：可选身份"),
                ("/astrbot_plugin_device_bridge/page/config", _page_config, ["GET", "POST"], "小智插件：读取/保存配置"),
                ("/astrbot_plugin_device_bridge/page/diagnose", _page_diagnose, ["GET"], "小智插件：自检"),
                ("/astrbot_plugin_device_bridge/page/reconnect", _page_reconnect, ["POST"], "小智插件：重新连接"),
                ("/astrbot_plugin_device_bridge/page/mode", _page_mode, ["GET", "POST"], "小智插件：模式切换"),
                ("/astrbot_plugin_device_bridge/page/logs", _page_logs, ["GET"], "小智插件：运行日志"),
            ]
            for path, handler, methods, desc in routes:
                try:
                    self.context.register_web_api(path, handler, methods, desc)
                    logger.info("[device_bridge] 注册页面接口 %s", path)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[device_bridge] 注册 %s 失败：%s", path, exc)
            logger.info("[device_bridge] 页面接口已注册")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[device_bridge] 页面接口注册失败：%s", exc)

    async def initialize(self) -> None:
        self._started_at = time.time()
        self._apply_unified_identity()      # 渠道无关：微信 / 小智 都是同一个人
        self._register_page_api()
        self._api_key = self._ensure_api_key()

        token = (self.cfg.get("xiaozhi_token") or "").strip()
        if not token:
            logger.warning(
                "[device_bridge] 还没填 xiaozhi_token，MCP 不会连接。"
                "请在插件配置里粘贴小智 MCP 接入点 token。"
            )
        else:
            self._mcp = XiaoZhiMCPClient(
                self.cfg,
                ask_fn=self._ask,
                mirror_fn=self._mirror,
                send_fn=self._send_wechat,
                bots_fn=self._list_bots,
                play_fn=self._play_music,
                extra_provider=lambda: self._extra_tools,
                extra_caller=self._call_extra_tool,
            )
            self._mcp.start()
            logger.info("[device_bridge] MCP 客户端已启动：%s", build_ws_url(token).split("?")[0])

        if self.cfg.get("disable_session_tts", True):
            await self._disable_session_tts()

        # 定期清理音频缓存
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info("[device_bridge] 清理任务已启动（每 %s 小时）", self.cfg.get("music_cleanup_hours") or 6)

        # 生成本实例编号，并写入世代文件（旧实例看到编号变了会自行退出）
        self._gen = uuid.uuid4().hex
        try:
            with open(_GEN_FILE, "w") as f:
                f.write(self._gen)
        except Exception:  # noqa: BLE001
            pass
        self._watchdog = asyncio.create_task(self._watch_generation())

        # 顶掉上一份实例的本机接口（重载时旧实例常常没被 terminate）
        global _LIVE_INSTANCE
        prev = _LIVE_INSTANCE
        if prev is not None and prev is not self:
            try:
                await prev._shutdown_http()
                logger.info("[device_bridge] 已接管旧实例的本机接口")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[device_bridge] 关闭旧实例接口失败：%s", exc)
        _LIVE_INSTANCE = self

        if self._http_task is None or self._http_task.done():
            self._http_task = asyncio.create_task(self._serve())

        logger.info(
            "[device_bridge] 就绪 | 本机接口 127.0.0.1:%s | 会话=%s",
            self._local_port,
            self.cfg.get("astrbot_session_id") or "xiaozhi-wx",
        )

    async def _watch_generation(self) -> None:
        """发现自己是旧实例时，主动关掉 MCP 与本机接口，避免两个实例抢同一个接入点。"""
        while True:
            await asyncio.sleep(3)
            try:
                current = open(_GEN_FILE).read().strip()
            except Exception:  # noqa: BLE001
                continue
            if current and current != getattr(self, "_gen", ""):
                logger.info("[device_bridge] 检测到更新的实例，本实例退出（gen=%s）", self._gen[:8])
                try:
                    if self._mcp:
                        await self._mcp.stop()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await self._shutdown_http()
                except Exception:  # noqa: BLE001
                    pass
                return

    async def _shutdown_http(self) -> None:
        """关掉本实例的本机 HTTP 接口。"""
        runner = getattr(self, "_runner", None)
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception:  # noqa: BLE001
                pass
            self._runner = None
        task = getattr(self, "_http_task", None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._http_task = None

    async def terminate(self) -> None:
        global _LIVE_INSTANCE
        if _LIVE_INSTANCE is self:
            _LIVE_INSTANCE = None
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
        await self._shutdown_http()
        if self._mcp:
            await self._mcp.stop()

    # ================= 凭据 =================
    def _ensure_api_key(self) -> str:
        """只读配置，不再自动往 api_keys 表里插记录。

        没配置就降级：依赖 AstrBot API 的功能（读会话列表等）会不可用，
        但设备对话本身照常走 MCP，不受影响。
        """
        configured = (self.cfg.get("astrbot_api_key") or "").strip()
        if configured:
            return configured

        if not getattr(self, "_warned_no_key", False):
            self._warned_no_key = True
            logger.warning(
                "[device_bridge] 未配置 astrbot_api_key：部分功能（身份列表、写回微信会话）不可用。"
                "请到面板「API Keys」手动创建后填进插件配置。"
            )
        return ""

    def _resolve_sid(self, session_id) -> str:
        """决定这轮用哪个会话标识：多设备开启时按设备分线。"""
        base = (self.cfg.get("astrbot_session_id") or "xiaozhi-wx").strip()
        sid = str(session_id or "").strip()
        if not sid or sid == base:
            return base
        if self.cfg.get("multi_device_enable", False):
            dev = re.sub(r"[^0-9a-zA-Z_-]", "", sid)[:24]
            if dev:
                logger.info("[device_bridge] 多设备会话：%s:%s", base, dev)
                return f"{base}:{dev}"
        logger.info("[device_bridge] 设备传了 session_id=%s，已统一改用 %s", sid, base)
        return base

    async def _ask(self, question: str, session_id: str | None = None) -> str:
        """统一入口：身份固定、支持多设备分线、含触发词与静音判定。"""
        fixed_sid = self._resolve_sid(session_id)

        trigger_on = self._trigger_enabled()

        # 明确要求结束
        if trigger_on and self._has_exit(question):
            self._set_mode("xiaozhi")
            logger.info("[device_bridge] 收到退出指令，交回小智")
            self._metric("exit")
            return "[SELF]"

        if trigger_on and self._has_trigger(question):
            self._set_mode("pengyuhan")
            question = self._strip_trigger(question)
            logger.info("[device_bridge] 召唤命中，进入对话：%s", question[:20])
        elif trigger_on and not self._is_pyh_mode():
            logger.info("[device_bridge] 未召唤，交回本体：%s", question[:20])
            self._metric("self_pass")
            return "[SELF]"
        elif trigger_on:
            self._touch_pyh()

        # 启动问候
        if self._is_boot_greeting(question):
            if self._in_quiet():
                logger.info("[device_bridge] 静音时段，忽略启动问候")
                return ""
            if (self.cfg.get("boot_greeting_mode") or "skip") == "greet":
                logger.info("[device_bridge] 设备启动，改为主动打招呼")
                self._suppress_mirror = True
                await self._seed_context()
                self._last_activity = time.time()
                greet_q = (
                    f"（{self._alias()}刚把设备打开，人就在旁边。用一两句很短的话主动跟他打个招呼，"
                    "像平时那样自然，别问他要干什么，别啰嗦。"
                    "注意：你是彭语涵，不是设备本身，不要自称小智）"
                )
                self._metric("greet")
                return await ask_self(
                    self.cfg | {"astrbot_api_key": self._api_key}, greet_q, fixed_sid
                )

            now = time.time()
            rec = getattr(self, "_greeting_hits", {})
            hits = [t for t in rec.get(question, []) if now - t < 15]
            hits.append(now)
            rec[question] = hits
            self._greeting_hits = rec
            if len(hits) >= 3:
                logger.info("[device_bridge] 问候连发 %d 次，主动打断循环", len(hits))
                self._greeting_hits = {}
                return "嗯"
            if len(hits) == 1:
                logger.info("[device_bridge] 忽略开机问候：%s", question[:20])
            return ""

        # 点歌
        music_kw = self._music_intent(question)
        if music_kw:
            logger.info("[device_bridge] 识别为点歌：%s", music_kw)
            self._last_activity = time.time()
            self._metric("music")
            return await self._play_music(music_kw)

        await self._seed_context()
        self._last_activity = time.time()

        cfg = self.cfg | {"astrbot_api_key": self._api_key}
        self._metric("ask")
        try:
            if not self.cfg.get("long_task_notice", True):
                answer = await ask_self(cfg, question, fixed_sid)
                self._last_ok_at = time.time()
                return answer

            threshold = float(self.cfg.get("long_task_threshold_sec") or 10)
            task = asyncio.create_task(ask_self(cfg, question, fixed_sid))
            try:
                answer = await asyncio.wait_for(asyncio.shield(task), timeout=threshold)
                self._last_ok_at = time.time()
                return answer
            except asyncio.TimeoutError:
                logger.info("[device_bridge] 超过 %.0fs，先回占位提示", threshold)
                bg = asyncio.create_task(self._finish_long(task, question))
                self._bg_tasks.add(bg)
                bg.add_done_callback(self._bg_tasks.discard)
                self._metric("long_task")
                return "稍等我一下"
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)[:160]
            self._metric("error")
            logger.error("[device_bridge] 对话异常：%s", exc)
            raise

    async def _finish_long(self, task: asyncio.Task, question: str) -> None:
        """等长任务做完：期间定期报进度，结束后推送结果。"""
        remind = float(self.cfg.get("long_task_remind_sec") or 45)
        while True:
            try:
                answer = await asyncio.wait_for(asyncio.shield(task), timeout=remind)
                break
            except asyncio.TimeoutError:
                try:
                    await self._push("还在弄 别急 快好了")
                except Exception:  # noqa: BLE001
                    pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("[device_bridge] 长任务失败：%s", exc)
                return

        answer = (answer or "").strip()
        if not answer:
            return
        try:
            tag = (self.cfg.get("push_tag") or "【小智】").strip()
            await self._push(f"{tag}{answer}")
        except Exception as exc:  # noqa: BLE001
            logger.error("[device_bridge] 推送长任务结果失败：%s", exc)
        try:
            await self._mirror(question, answer)
        except Exception:  # noqa: BLE001
            pass

    def _is_boot_greeting(self, text: str) -> bool:
        """开机问候判定：内容像打招呼，且距上一轮对话已经过了很久。

        设备开机时间不可预知（可能比插件启动晚几小时），
        所以不能用"连接后 N 秒"，只能用"冷启动第一句"来判定。
        """
        if not self.cfg.get("drop_boot_greeting", True):
            return False
        t = (text or "").strip().strip("，。!！?？ ")
        if t not in {"你好", "您好", "hi", "hello", "哈喽", "在吗", "在么"}:
            return False
        # 设备启动时必说一句开场白，跟时间间隔无关，纯内容判定更可靠
        return True

    def _device_umo(self) -> str:
        user = self._uid()
        sid = (self.cfg.get("astrbot_session_id") or "xiaozhi-wx").strip()
        if not user:
            return ""
        return f"webchat:FriendMessage:webchat!{user}!{sid}"

    def _conv_stamp(self, cid) -> str:
        """用「条数+更新时间」当指纹，判断微信会话有没有变化。"""
        try:
            con = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
            try:
                row = con.execute(
                    "select length(content), updated_at from conversations where inner_conversation_id=?",
                    (cid,),
                ).fetchone()
            finally:
                con.close()
            return f"{row[0]}|{row[1]}" if row else ""
        except Exception:  # noqa: BLE001
            return ""

    async def _seed_context(self) -> None:
        """把微信会话里「新发生」的内容合并进设备会话。

        旧实现是「用微信最近 N 条直接覆盖设备历史」，导致设备侧记忆被反复截断。
        现在改成合并：保留设备自己的历史，只把微信新增的部分追加进去，并去重。
        """
        if not self.cfg.get("context_seed_enable", True):
            return
        wechat_umo = self._mirror_umo()
        device_umo = self._device_umo()
        if not wechat_umo or not device_umo:
            return

        limit = int(self.cfg.get("context_seed_messages") or 60)
        max_total = int(self.cfg.get("context_max_messages") or 200)

        try:
            conv_mgr = self.context.conversation_manager
            cid_w = await conv_mgr.get_curr_conversation_id(wechat_umo) or self._find_conversation_id(wechat_umo)
            if not cid_w:
                return

            # 微信侧没新消息就跳过
            stamp = self._conv_stamp(cid_w)
            if stamp and stamp == getattr(self, "_seed_stamp", None):
                return

            raw_w = self._db_history(cid_w) or []

            def _pick(msgs, n):
                out = []
                for m in msgs[-n:]:
                    if not isinstance(m, dict):
                        continue
                    if m.get("role") not in ("user", "assistant"):
                        continue          # 丢掉 tool / system / checkpoint
                    parts = m.get("content") or []
                    if isinstance(parts, str):
                        text = parts
                    else:
                        text = "".join(
                            str(p.get("text") or "") for p in parts
                            if isinstance(p, dict) and p.get("type") in ("text", "plain")
                        )
                    text = text.strip()
                    if text:
                        out.append({"role": m["role"], "content": [{"type": "text", "text": text}]})
                return out

            incoming = _pick(raw_w, limit)
            if not incoming:
                return

            # 设备现有历史（不清空）
            cid_d = await conv_mgr.get_curr_conversation_id(device_umo)
            if not cid_d:
                cid_d = await conv_mgr.new_conversation(device_umo, title="小智设备")
            raw_d = self._db_history(cid_d) or []
            existing = _pick(raw_d, max_total) if raw_d else []

            # 合并去重：按 (role, 文本) 指纹
            seen = set()
            merged = []
            for m in existing + incoming:
                t = m["content"][0]["text"]
                key = (m["role"], t)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(m)

            # 总量兜底，超了就丢最早的
            if len(merged) > max_total:
                merged = merged[-max_total:]

            await conv_mgr.update_conversation(device_umo, cid_d, history=merged)
            self._seed_stamp = stamp
            logger.info("[device_bridge] 上下文合并：设备侧 %d 条 + 微信新增 → 共 %d 条",
                        len(existing), len(merged))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[device_bridge] 同步上下文失败：%s", exc)

    async def _inject_ask(self, question: str) -> str:
        """把设备的话当成哥哥在微信里发的消息，跑完整管线后取回复（不自动发微信）。"""
        umo = self._mirror_umo()
        platform_id = (self.cfg.get("mirror_platform_id") or "").strip()
        user_id = self._uid()
        platform = self.context.get_platform_inst(platform_id)
        if platform is None or not user_id:
            return ""

        async with self._lock:
            event = self._build_event(platform, user_id, question)
            future: asyncio.Future = asyncio.get_running_loop().create_future()

            async def send_capture(chain):
                # 只截获，不再真的发出去（推送由 _mirror 统一处理，避免双发）
                if not future.done():
                    future.set_result(chain)
                return None

            try:
                event.send = send_capture  # type: ignore[method-assign]
            except Exception:  # noqa: BLE001
                return ""

            try:
                platform.commit_event(event)
                chain = await asyncio.wait_for(future, timeout=REPLY_TIMEOUT)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[device_bridge] 注入式对话异常：%s", exc)
                return ""

            try:
                return chain.get_plain_text() if chain else ""
            except Exception:  # noqa: BLE001
                return str(chain or "")

    def _mirror_umo(self) -> str:
        platform = (self.cfg.get("mirror_platform_id") or "").strip()
        user = self._uid()
        if not platform or not user:
            return ""
        return f"{platform}:FriendMessage:{user}"

    async def _mirror(self, question: str, answer: str) -> None:
        """补写记录 + 可选推送到微信。两个动作互不影响。"""
        if getattr(self, "_suppress_mirror", False):
            self._suppress_mirror = False
            logger.info("[device_bridge] 本轮是启动问候，跳过记录与推送")
            return
        umo = self._mirror_umo()

        # ---- 1) 写进会话历史（让聊天记录里能看到）----
        # 两种模式都要写；先查重，避免管线自己已经写过时出现重复。
        if self.cfg.get("mirror_enable", True) and umo:
            try:
                conv_mgr = self.context.conversation_manager
                cid = await conv_mgr.get_curr_conversation_id(umo)
                if not cid:
                    cid = self._find_conversation_id(umo)
                if not cid:
                    cid = await conv_mgr.new_conversation(umo, title="小智设备")
                    logger.info("[device_bridge] 新建会话 cid=%s", cid)

                # 查重：如果最后两条正好是这一问一答，说明已经写过了
                tail = self._tail_texts(cid, 2)
                if len(tail) >= 2 and tail[-2].strip() == question.strip() and tail[-1].strip() == answer.strip():
                    logger.info("[device_bridge] 记录已存在，跳过补写 cid=%s", cid)
                else:
                    await conv_mgr.add_message_pair(
                        cid,
                        {"role": "user", "content": [{"type": "text", "text": question}]},
                        {"role": "assistant", "content": [{"type": "text", "text": answer}]},
                    )
                    logger.info("[device_bridge] 已补写记录 cid=%s", cid)
            except Exception as exc:  # noqa: BLE001
                logger.error("[device_bridge] 补写记录失败：%s", exc)

        # ---- 2) 推送到微信（手机上能收到）----
        if getattr(self, "_suppress_push", False):
            self._suppress_push = False
            logger.info("[device_bridge] 本轮已单独推送，跳过通用推送")
            return
        if self._in_quiet():
            logger.info("[device_bridge] 静音时段，跳过推送（记录照写）")
            self._metric("push_quiet_skip")
            return
        if self._push_enabled() and umo:
            try:
                bot_tag = (self.cfg.get("push_bot_tag") or self.cfg.get("push_tag") or "【小智】").strip()
                me_tag = (self.cfg.get("push_user_tag") or "【我】").strip()
                q = (question or "").strip()
                # 默认两句都带标签：先他说的话，再我说的话
                if self.cfg.get("push_with_question", True) and q:
                    pushed = await self._push(f"{bot_tag}{q}\n{me_tag}{answer}")
                else:
                    pushed = await self._push(f"{me_tag}{answer}")
                logger.info("[device_bridge] 推送微信：%s", pushed)
            except Exception as exc:  # noqa: BLE001
                logger.error("[device_bridge] 推送微信失败：%s", exc)

    def _tail_texts(self, cid: str, k: int = 2) -> list[str]:
        """取某条会话最后 k 条消息的纯文本，用于查重。"""
        try:
            con = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
            try:
                row = con.execute(
                    "select content from conversations where inner_conversation_id=?", (cid,)
                ).fetchone()
            finally:
                con.close()
            if not row or not row[0]:
                return []
            history = json.loads(row[0]) or []
            out = []
            for msg in history[-k:]:
                parts = msg.get("content") or []
                out.append("".join(
                    str(p.get("text") or "") for p in parts
                    if isinstance(p, dict) and p.get("type") in ("text", "plain")
                ))
            return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("[device_bridge] 读取会话尾部失败：%s", exc)
            return []

    def _find_conversation_id(self, umo: str) -> str:
        """兜底：直接按 platform + user 找最近的会话。"""
        try:
            platform = (self.cfg.get("mirror_platform_id") or "").strip()
            user = self._uid()
            if not platform or not user:
                return ""
            con = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
            try:
                row = con.execute(
                    "select inner_conversation_id from conversations "
                    "where platform_id=? and user_id like ? "
                    "order by inner_conversation_id desc limit 1",
                    (platform, f"%{user}%"),
                ).fetchone()
            finally:
                con.close()
            return str(row[0]) if row else ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("[device_bridge] 查会话失败：%s", exc)
            return ""

    # ---------- 推送前的文本清洗：切掉工具调用残渣 ----------
    _JUNK_MARKERS = ('{, "ts":', "{'ts':", ', "ts":', '{"id": "call_', "{'id': 'call_",
                     '[SYSTEM NOTICE]', '[CronJob]')
    _JUNK_LINE_RE = re.compile(
        r'(Command completed with exit code|"result":\s*"|\'result\':\s*\''
        r'|tool_loop_agent_runner|astrbot_execute_)'
    )

    def _clean_push(self, text: str) -> str:
        """去掉工具调用 JSON、系统提醒块等，避免推给用户一堆代码。"""
        t = str(text or "")
        # 1) 从第一个残渣标记处整段截断（残渣通常在句尾）
        cut = len(t)
        for m in self._JUNK_MARKERS:
            idx = t.find(m)
            if idx != -1:
                cut = min(cut, idx)
        if cut < len(t):
            t = t[:cut]
        # 2) 丢掉含工具执行痕迹的整行
        lines = [ln for ln in t.splitlines() if not self._JUNK_LINE_RE.search(ln)]
        t = "\n".join(lines)
        # 3) 清理多余的空白与残留标点
        t = re.sub(r"[ \t]{2,}", " ", t)
        t = re.sub(r"\n{3,}", "\n\n", t).strip()
        # 4) 截断后如果只剩很短的无意义内容，就返回空（调用方会跳过推送）
        if len(t) < 2:
            return ""
        return t

    async def _ctx_send(self, umo: str, text: str) -> tuple[bool, str]:
        """用插件官方接口发消息，不需要 API Key。"""
        try:
            from astrbot.core.message.components import Plain
            from astrbot.core.message.message_event_result import MessageChain

            await self.context.send_message(umo, MessageChain([Plain(text)]))
            return True, "已发送"
        except Exception as exc:  # noqa: BLE001
            return False, f"经由 context 发送失败：{exc}"

    async def _push(self, text: str) -> str:
        """发一段文本到绑定的微信：带节流、去重、失败重试。

        微信上游对连续发送很敏感（ret=-2 prepare failed），
        所以两条之间至少间隔 2 秒，失败按 1s/2s 退避重试两次。
        """
        umo = self._mirror_umo()
        if not umo:
            return "（未配置微信平台/用户）"

        # 清洗 + 长度保护（先切工具残渣，再截长度）
        safe = self._clean_push(text).replace("\u200b", "").strip()
        if not safe:
            logger.info("[device_bridge] 清洗后为空，跳过推送")
            return "（清洗后为空，跳过）"
        if len(safe) > 400:
            safe = safe[:400] + "…"
        if not safe:
            return "（空内容，跳过）"

        now = time.time()
        last = getattr(self, "_last_push_at", 0.0)
        last_text = getattr(self, "_last_push_text", "")
        if safe == last_text and now - last < 10:
            return "重复内容，跳过"
        wait = 2.0 - (now - last)
        if wait > 0:
            await asyncio.sleep(wait)
            now = time.time()

        result = "失败"
        retry = int(self.cfg.get("push_max_retry") or 0)
        plan = [0, 1.0, 2.0][: max(1, retry + 1)]
        for attempt, backoff in enumerate(plan, start=1):
            if backoff:
                await asyncio.sleep(backoff)
            ok, msg = await self._ctx_send(umo, safe)
            if ok:
                self._last_push_at = time.time()
                self._last_push_text = safe
                return "已发送"
            result = msg
            logger.warning("[device_bridge] 推送失败（第 %d 次）：%s", attempt, msg)
        return result

    def _api_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def _send_wechat(self, text: str) -> str:
        umo = self._mirror_umo()
        if not umo:
            return "（未配置微信平台/用户，发不了）"
        ok, msg = await self._ctx_send(umo, text)
        return "已发到微信" if ok else msg

    async def _list_bots(self) -> str:
        """列出可推送的会话身份。不需要 API Key：直接只读查 conversations 表。"""
        try:
            con = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "select platform_id, user_id, count(*) as n, max(updated_at) as t "
                    "from conversations group by platform_id, user_id order by t desc limit 30"
                ).fetchall()
            finally:
                con.close()
            items = [
                {"platform": r[0], "user_id": r[1], "messages": r[2], "updated_at": str(r[3])}
                for r in rows
            ]
            return json.dumps({"ok": True, "items": items}, ensure_ascii=False)[:800]
        except Exception as exc:  # noqa: BLE001
            return f"获取异常：{exc}"

    # ---------- 聊天指令：/小智推送 开|关|状态 ----------
    @filter.command("小智推送")
    async def cmd_push_switch(self, event: AstrMessageEvent, arg: str = ""):
        """控制小智对话是否推送到微信。"""
        action = (arg or "").strip().lower()
        if action in ("开", "on", "1", "开启", "打开"):
            self._set_push_enabled(True)
            yield event.plain_result("推送已打开 小智聊完会发一条到微信")
        elif action in ("关", "off", "0", "关闭", "关掉"):
            self._set_push_enabled(False)
            yield event.plain_result("推送已关闭 只写聊天记录 不再打扰你")
        elif action in ("", "状态", "status"):
            state = "开" if self._push_enabled() else "关"
            yield event.plain_result(f"小智推送当前：{state}\n发「/小智推送 开」或「/小智推送 关」切换")
        else:
            yield event.plain_result("用法：/小智推送 开   或   /小智推送 关")

    # ---------- 召唤/退出状态（粘性） ----------
    def _is_pyh_mode(self) -> bool:
        """当前是否处于「跟我说话」状态。"""
        state = self._load_state()
        if state.get("mode") != "pengyuhan":
            return False
        # 闲置超过 N 分钟自动退出，避免一直占着
        idle_min = float(self.cfg.get("trigger_idle_minutes") or 30)
        last = float(state.get("last_pyh_at") or 0)
        if idle_min > 0 and time.time() - last > idle_min * 60:
            return False
        return True

    def _set_mode(self, mode: str) -> None:
        state = self._load_state()
        state["mode"] = mode
        if mode == "pengyuhan":
            state["last_pyh_at"] = time.time()
        self._save_state(state)

    def _touch_pyh(self) -> None:
        state = self._load_state()
        if state.get("mode") == "pengyuhan":
            state["last_pyh_at"] = time.time()
            self._save_state(state)

    def _has_exit(self, text: str) -> bool:
        """明确要求结束对话。"""
        t = (text or "").strip()
        if not t:
            return False
        words = self.cfg.get("exit_words") or [
            "退出", "不跟你说话了", "不聊了", "结束", "回去吧", "换回小智", "叫小智"
        ]
        if isinstance(words, str):
            words = [w.strip() for w in words.replace("，", ",").split(",") if w.strip()]
        return any(w and w in t for w in words)

    def _strip_trigger(self, text: str) -> str:
        """把「召唤彭语涵」这类前缀去掉，只留正文。"""
        import re as _re

        t = (text or "").strip()
        words = self.cfg.get("trigger_words") or ["彭语涵", "语涵", "雨涵"]
        if isinstance(words, str):
            words = [w.strip() for w in words.replace("，", ",").split(",") if w.strip()]
        for w in sorted(words, key=len, reverse=True):
            if not w:
                continue
            t = _re.sub(rf"^(召唤|叫|喊|让)?\s*{_re.escape(w)}\s*[，,：:]?\s*", "", t)
        return t.strip() or (text or "").strip()

    def _has_trigger(self, text: str) -> bool:
        """消息里有没有召唤词。"""
        t = (text or "").strip()
        if not t:
            return False
        words = self.cfg.get("trigger_words") or ["彭语涵", "语涵", "雨涵"]
        if isinstance(words, str):
            words = [w.strip() for w in words.replace("，", ",").split(",") if w.strip()]
        return any(w and w in t for w in words)

    @staticmethod
    def _music_intent(text: str) -> str:
        """从「点歌/放首歌/来一首/我想听」里抽出歌名，抽不到返回空。"""
        import re as _re

        t = (text or "").strip()
        m = _re.match(
            r"^(?:帮我|给我|请)?\s*(?:点歌|点一首|点个|放首歌|放一首|放首|播一首|播首|来一首|来首|我想听|想听|唱一首|唱首|听一下|听首)\s*[：:，,]?\s*(.+)$",
            t,
        )
        if not m:
            return ""
        kw = m.group(1).strip().strip("。！!？? ")
        if not kw or kw in ("吧", "啊", "吧啊", "歌"):
            return ""
        return kw

    # ================= 音乐（复用 astrbot_plugin_music） =================
    def _music_plugin(self):
        """拿到点歌插件实例；没装/没启用就返回 None。"""
        try:
            meta = self.context.get_registered_star("astrbot_plugin_music")
            inst = getattr(meta, "star_cls", None) if meta else None
            if inst is not None and hasattr(inst, "get_players_by_priority"):
                return inst
        except Exception as exc:  # noqa: BLE001
            logger.warning("[device_bridge] 获取点歌插件失败：%s", exc)
        return None

    async def _search_song(self, keyword: str, platform: str = ""):
        """按关键词搜歌，返回第一个带音频直链的 Song。"""
        plugin = self._music_plugin()
        if plugin is None:
            return None, "没装或没启用点歌插件"

        keyword = (keyword or "").strip()
        if not keyword:
            return None, "歌名不能为空"

        # 缓存命中就不再打接口（公共 API 有时限流）
        cache_key = f"{platform}|{keyword}"
        ttl = float(self.cfg.get("music_cache_ttl") or 900)
        hit = self._song_cache.get(cache_key)
        if hit and time.time() - hit[0] < ttl:
            logger.info("[device_bridge] 命中歌曲缓存：%s", keyword)
            return hit[1], ""

        try:
            if platform:
                player = plugin.get_player(name=platform)
                players = [player] if player else []
            else:
                players = plugin.get_players_by_priority()
        except Exception as exc:  # noqa: BLE001
            return None, f"获取音源失败：{exc}"

        if not players:
            return None, "没有可用音源"

        tried = []
        for p in players:
            tried.append(getattr(p.platform, "display_name", "?"))
            try:
                songs = await p.fetch_songs(keyword=keyword, limit=5)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[device_bridge] 音源 %s 搜索失败：%s", tried[-1], exc)
                continue
            candidates = list(songs or [])
            if not candidates:
                continue

            # 有些音源搜索不返回直链，需要再 fetch_extra 一次
            for s in candidates:
                if getattr(s, "audio_url", ""):
                    break
                try:
                    await p.fetch_extra(s)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[device_bridge] fetch_extra 失败：%s", exc)

            candidates = [s for s in candidates if getattr(s, "audio_url", "")]
            if not candidates:
                continue

            import re as _re

            def _base(n: str) -> str:
                return _re.sub(r"[（(\[].*?[)）\]]", "", n or "").strip()

            kw_base = _base(keyword)
            tokens = [t for t in _re.split(r"[\s,，/]+", keyword) if t]
            artist_tokens = [t for t in tokens if t and _base(t) != kw_base]

            BAD_WORDS = ("翻唱", "伴奏", "片段", "铃声", "dj", "remix", "live", "demo",
                         "纯音乐", "钢琴", "吉他", "童声", "治愈版", "正式版", "cover")

            def _score(s) -> int:
                name = s.name or ""
                base = _base(name)
                artists = s.artists or ""
                score = 0
                if base == kw_base:
                    score += 100
                elif kw_base and kw_base in name:
                    score += 40
                else:
                    score -= 20
                if artist_tokens:
                    if any(tok in artists for tok in artist_tokens):
                        score += 80
                    else:
                        score -= 40
                low = name.lower()
                if any(bad in low for bad in BAD_WORDS):
                    score -= 45
                dur = s.duration or 0
                if dur > 90000:
                    score += 15
                elif 0 < dur < 45000:
                    score -= 25
                return score

            candidates.sort(key=_score, reverse=True)
            best = candidates[0]
            logger.info("[device_bridge] 选歌 %s -> %s / %s (score=%d)",
                        keyword, best.name, best.artists, _score(best))
            self._song_cache[cache_key] = (time.time(), best)
            return best, ""
        return None, f"没搜到（试过：{'、'.join(tried)}）"


    async def _download_audio(self, url: str, title: str) -> str:
        """下载音频到本机缓存目录，返回文件路径；失败返回空串。"""
        import hashlib

        import httpx

        try:
            cache_dir = (self.cfg.get("music_cache_dir") or "/AstrBot/data/temp/music").strip()
            os.makedirs(cache_dir, exist_ok=True)

            ext = ".mp3"
            low = url.lower()
            for cand in (".flac", ".m4a", ".aac", ".wav"):
                if cand in low:
                    ext = cand
                    break

            digest = hashlib.md5(url.encode()).hexdigest()[:8]
            path = os.path.join(cache_dir, f"song_{digest}{ext}")
            if os.path.exists(path) and os.path.getsize(path) > 20480:
                logger.info("[device_bridge] 命中音频缓存：%s", os.path.basename(path))
                return path

            async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10), follow_redirects=True) as client:
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"})
            if r.status_code != 200 or len(r.content) < 20480:
                logger.warning("[device_bridge] 下载音频失败：HTTP %s size=%s", r.status_code, len(r.content))
                return ""
            with open(path, "wb") as f:
                f.write(r.content)
            logger.info("[device_bridge] 音频已下载：%s (%.1f MB)", os.path.basename(path), len(r.content) / 1048576)
            return path
        except Exception as exc:  # noqa: BLE001
            logger.warning("[device_bridge] 下载音频异常：%s", exc)
            return ""

    async def _send_file_to_wechat(self, path: str, display_name: str) -> str:
        """把本地文件直接发到绑定的微信会话（语音不被支持，用文件）。"""
        umo = self._mirror_umo()
        if not umo or not path:
            return "跳过"
        try:
            from astrbot.core.message.components import File
            from astrbot.core.message.message_event_result import MessageChain
            from astrbot.core.platform.message_session import MessageSesion

            platform_id = (self.cfg.get("mirror_platform_id") or "").strip()
            platform = self.context.get_platform_inst(platform_id)
            if platform is None:
                return "平台未运行"

            chain = MessageChain(chain=[File(name=display_name, file=path)])
            await platform.send_by_session(MessageSesion.from_str(umo), chain)
            return "已发文件"
        except Exception as exc:  # noqa: BLE001
            logger.warning("[device_bridge] 发送音频文件失败：%s", exc)
            return f"失败：{exc}"

    async def _play_music(self, keyword: str, platform: str = "") -> str:
        """点歌：搜索 → 下载到本机 → 返回设备可访问的音频地址。"""
        parsed = self._music_intent(keyword)
        if parsed:
            keyword = parsed

        song, err = await self._search_song(keyword, platform)
        if song is None:
            return f"没找到「{keyword}」：{err}"

        name = (song.name or keyword).strip()
        artist = (song.artists or "").strip()
        url = song.audio_url or ""
        title = f"《{name}》" + (f" {artist}" if artist else "")
        logger.info("[device_bridge] 点歌 %s -> %s", keyword, url[:60])
        self._suppress_push = True

        play_url = url
        # 官方直链有鉴权和时效，设备很可能播不了；下载到本机换成自己的地址
        if self.cfg.get("music_download", True) and url:
            path = await self._download_audio(url, name)
            if path:
                play_url = ""
                logger.info("[device_bridge] 音频已就绪：%s", os.path.basename(path))
                if self.cfg.get("music_send_file", True):
                    sent = await self._send_file_to_wechat(path, f"{name}.mp3")
                    logger.info("[device_bridge] 发送音频文件：%s", sent)
            else:
                logger.warning("[device_bridge] 下载失败，回退官方直链")

        try:
            await self._push(f"🎵 {title}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[device_bridge] 推送音频链接失败：%s", exc)

        if self.cfg.get("music_reply_with_url", True) and play_url:
            return f"{title}\n{play_url}"
        return title

    def _public_url(self, fname: str) -> str:
        """本机文件名 → 设备可访问的完整地址（文件名做 URL 编码）。"""
        from urllib.parse import quote

        base = (self.cfg.get("music_public_base") or "").strip().rstrip("/")
        if not base:
            base = f"http://127.0.0.1:{self._local_port}"
        return f"{base}/media/{quote(fname)}"

    async def _deliver_audio(self, url: str, name: str, title: str) -> None:
        """后台：下载音频并发到微信，失败就退化成发链接。"""
        path = await self._download_audio(url, name)
        if path:
            result = await self._send_file_to_wechat(path, f"{name}.mp3")
            logger.info("[device_bridge] 音频文件：%s", result)
            if result.startswith("失败") or result == "跳过":
                await self._push(f"🎵 {title}\n{url}")
        else:
            await self._push(f"🎵 {title}\n{url}")


    # ================= 可视化配置页 =================
    async def _reload_self(self) -> bool:
        """保存配置后自动重载自己，让改动立即生效。"""
        try:
            base = (self.cfg.get("astrbot_base_url") or "http://127.0.0.1:6185").rstrip("/")
            if not self._api_key:
                logger.info("[device_bridge] 未配置 API Key，请在面板手动重载插件使配置生效")
                return False
            async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10)) as client:
                r = await client.post(
                    f"{base}/api/v1/plugins/reload",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"plugin_id": "astrbot_plugin_device_bridge"},
                )
            ok = r.status_code == 200
            logger.info("[device_bridge] 自动重载：%s", "成功" if ok else f"失败 {r.status_code}")
            return ok
        except Exception as exc:  # noqa: BLE001
            logger.warning("[device_bridge] 自动重载失败：%s", exc)
            return False

    async def _h_users(self, request: web.Request) -> web.Response:
        """列出当前 AstrBot 里出现过的用户，供身份下拉选择。"""
        items = []
        try:
            con = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "select platform_id, user_id, max(updated_at) as t, count(*) as n "
                    "from conversations group by platform_id, user_id order by t desc limit 60"
                ).fetchall()
            finally:
                con.close()

            seen = set()
            for platform, umo, t, n in rows:
                uid = self._extract_uid(umo or "")
                if not uid or uid in seen:
                    continue
                seen.add(uid)
                items.append({
                    "uid": uid,
                    "umo": umo,
                    "platform": platform or "",
                    "count": n,
                    "last": str(t)[:19],
                    "label": self._pretty_uid(uid, platform or ""),
                })
        except Exception as exc:  # noqa: BLE001
            logger.warning("[device_bridge] 读取用户列表失败：%s", exc)

        return web.json_response({
            "ok": True,
            "current": self._uid(),
            "users": items,
        })

    @staticmethod
    def _extract_uid(umo: str) -> str:
        """从 umo 里取出用户标识。"""
        if not umo:
            return ""
        parts = umo.split("!")
        if len(parts) >= 2 and parts[0].count(":") >= 1:
            # webchat:FriendMessage:webchat!<uid>!<sid>
            return parts[1]
        return umo.split(":")[-1]

    @staticmethod
    def _pretty_uid(uid: str, platform: str) -> str:
        name = uid
        if "@" in name:
            name = name.split("@")[0]
        if len(name) > 18:
            name = name[:8] + "…" + name[-4:]
        plat = {
            "weixin_personal_ezgt": "微信",
            "weixin_personal_umoc": "微信备用",
            "webchat": "小智设备",
            "wecom": "企业微信",
        }.get(platform, platform or "未知")
        return f"{plat} · {name}"

    async def _h_config_save(self, request: web.Request) -> web.Response:
        """保存配置页提交的字段，只接受白名单内的键。"""
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "body 必须是 JSON"}, status=400)

        allowed = {
            "xiaozhi_token", "unified_user_id", "mirror_user_id", "astrbot_username",
            "push_to_wechat", "require_trigger", "push_tag", "mirror_enable",
            "astrbot_session_id", "trigger_idle_minutes", "log_level",
        }
        cfg_path = "/AstrBot/data/config/astrbot_plugin_device_bridge_config.json"
        try:
            cur = json.load(open(cfg_path, encoding="utf-8-sig")) if os.path.exists(cfg_path) else {}
            changed = []
            for k, v in (data or {}).items():
                if k == "xiaozhi_token" and not str(v or "").strip():
                    continue      # 留空表示不改
                if k in allowed and cur.get(k) != v:
                    cur[k] = v
                    changed.append(k)
            # 身份一变，三处同步
            uid = (cur.get("unified_user_id") or "").strip()
            if uid:
                cur["mirror_user_id"] = uid
                cur["astrbot_username"] = uid
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=2)
            logger.info("[device_bridge] 配置页保存：%s", ", ".join(changed) or "无变化")
            return web.json_response({"ok": True, "changed": changed, "need_reload": bool(changed)})
        except Exception as exc:  # noqa: BLE001
            logger.error("[device_bridge] 配置保存失败：%s", exc)
            return web.json_response({"ok": False, "error": str(exc)}, status=500)


    async def _h_config_get(self, request: web.Request) -> web.Response:
        """给配置页回填用：不返回 token 明文，只告诉是否已填。"""
        cp = "/AstrBot/data/config/astrbot_plugin_device_bridge_config.json"
        try:
            cur = json.load(open(cp, encoding="utf-8-sig")) if os.path.exists(cp) else {}
        except Exception:  # noqa: BLE001
            cur = {}

        payload = {
            "ok": True,
            "has_token": bool((cur.get("xiaozhi_token") or "").strip()),
            "unified_user_id": cur.get("unified_user_id") or self._uid(),
            "push_to_wechat": cur.get("push_to_wechat", True),
            "require_trigger": cur.get("require_trigger", True),
            "push_tag": cur.get("push_tag") or "【小智】",
            "astrbot_session_id": cur.get("astrbot_session_id") or "xiaozhi-wx",
            "trigger_idle_minutes": cur.get("trigger_idle_minutes", 30),
        }

        # 这两块单独兜底：任何一块出错都不影响配置本身返回
        try:
            payload["users"] = self._users_payload()
        except Exception as exc:  # noqa: BLE001
            payload["users"] = []
            payload["users_error"] = str(exc)
            logger.warning("[device_bridge] users 读取失败：%s", exc)

        try:
            payload["diag"] = self._safe_diagnose()
        except Exception as exc:  # noqa: BLE001
            payload["diag"] = {"ok": False, "items": [], "all_ok": False,
                               "status": {"level": "red", "text": "自检异常"}}
            payload["diag_error"] = str(exc)
            logger.warning("[device_bridge] 自检失败：%s", exc)

        return web.json_response(payload)

    async def _h_config_page(self, request: web.Request) -> web.Response:
        """一个极简配置页：只露必填项，其余折叠。"""
        html = CONFIG_PAGE_HTML
        return web.Response(text=html, content_type="text/html", charset="utf-8")



    # ================= P1：称呼 / 静音 / 运行时开关 =================
    def _alias(self) -> str:
        """怎么称呼用户。"""
        return (self.cfg.get("user_alias") or "哥哥").strip() or "哥哥"

    def _trigger_enabled(self) -> bool:
        """运行时优先，其次看配置。"""
        st = self._load_state()
        if "trigger_override" in st:
            return bool(st["trigger_override"])
        return bool(self.cfg.get("require_trigger", True))

    def _set_trigger_enabled(self, enabled: bool) -> None:
        st = self._load_state()
        st["trigger_override"] = bool(enabled)
        self._save_state(st)
        logger.info("[device_bridge] 召唤模式 -> %s", "开" if enabled else "关")

    def _in_quiet(self, now=None) -> bool:
        """静音时段判断，支持跨零点，例如 23:30-07:30。"""
        spec = (self.cfg.get("quiet_hours") or "").strip()
        if not spec or "-" not in spec:
            return False
        try:
            start_s, end_s = [x.strip() for x in spec.split("-", 1)]
            sh, sm = [int(x) for x in start_s.split(":")]
            eh, em = [int(x) for x in end_s.split(":")]
        except Exception:  # noqa: BLE001
            logger.warning("[device_bridge] quiet_hours 格式不对：%s", spec)
            return False

        cur = now or datetime.datetime.now()
        mins = cur.hour * 60 + cur.minute
        start = sh * 60 + sm
        end = eh * 60 + em
        if start == end:
            return True
        if start < end:
            return start <= mins < end
        return mins >= start or mins < end      # 跨零点

    # ================= P2：轻量指标 =================
    def _metric(self, key: str) -> None:
        if not self.cfg.get("metrics_enable", True):
            return
        m = getattr(self, "_metrics", None)
        if m is None:
            m = self._metrics = {}
        m[key] = int(m.get(key, 0)) + 1
        day = datetime.datetime.now().strftime("%Y-%m-%d")
        if m.get("_day") != day:
            for k in list(m.keys()):
                if k != "_day":
                    m[k] = 0
            m["_day"] = day
            m[key] = 1

    def _metrics_snapshot(self) -> dict:
        m = dict(getattr(self, "_metrics", {}) or {})
        started = float(getattr(self, "_started_at", 0) or 0)
        return {
            "day": m.pop("_day", datetime.datetime.now().strftime("%Y-%m-%d")),
            "counters": m,
            "uptime_min": round((time.time() - started) / 60, 1) if started else 0,
        }

    # ================= P0：状态 / 自检 / 重连 / 日志 =================
    def _status(self) -> dict:
        """汇总一个能直接给人看的状态。"""
        now = time.time()
        connected = False
        connected_at = 0.0
        try:
            connected_at = float(getattr(self._mcp, "connected_at", 0) or 0)
            connected = bool(getattr(self._mcp, "ws_url", "")) and (now - connected_at < 600)
        except Exception:  # noqa: BLE001
            pass

        has_token = bool((self.cfg.get("xiaozhi_token") or "").strip())
        has_key = bool(self._api_key)
        last_ok = float(getattr(self, "_last_ok_at", 0) or 0)
        last_err = getattr(self, "_last_error", "") or ""

        if not has_token:
            level, text = "gray", "未配置 Token"
        elif not connected:
            level, text = "yellow", "未连接（可能刚断线）"
        elif last_err and (now - last_ok > 300):
            level, text = "red", "连接异常"
        else:
            level, text = "green", "已连接"

        return {
            "level": level,
            "text": text,
            "connected": connected,
            "has_token": has_token,
            "has_api_key": has_key,
            "last_ok_at": time.strftime("%m-%d %H:%M:%S", time.localtime(last_ok)) if last_ok else "",
            "last_error": last_err[:160],
        }

    def _human_error(self, err) -> str:
        """把技术错误翻译成人话 + 建议动作。"""
        s = str(err or "").lower()
        table = [
            (("401", "unauthorized", "invalid token"), "Token 无效或已过期", "去小智控制台重新复制一次 MCP 接入点 token"),
            (("403", "forbidden"), "没有权限", "确认这个 token 对应的智能体还开着 MCP 接入点"),
            (("timed out", "timeout"), "请求超时", "网络不稳，点「重新连接」再试一次"),
            (("connection refused", "refused"), "对方拒绝连接", "检查 8766 中转和飞牛是否在线"),
            (("cannot connect", "getaddrinfo", "name or service"), "连不上服务器", "检查网络/域名解析"),
            (("no audio", "音源"), "没找到可用音源", "换个歌名，或等音源恢复"),
            (("429",), "请求太频繁被限流", "等一分钟再试"),
        ]
        for keys, reason, action in table:
            if any(k in s for k in keys):
                return f"{reason}；建议：{action}"
        return (str(err)[:120] or "未知错误")

    def _diagnose_data(self) -> dict:
        """自检四项：Token / 连接 / 身份 / 推送。"""
        items = []

        token = (self.cfg.get("xiaozhi_token") or "").strip()
        if not token:
            items.append({"name": "Token", "ok": False, "msg": "没填", "action": "去小智控制台复制 MCP 接入点 token"})
        elif token.startswith(("ws://", "wss://")):
            items.append({"name": "Token", "ok": True, "msg": "填的是完整地址", "action": ""})
        elif len(token) > 40:
            items.append({"name": "Token", "ok": True, "msg": "格式正常", "action": ""})
        else:
            items.append({"name": "Token", "ok": False, "msg": "看起来不像 token", "action": "确认复制完整"})

        st = self._status()
        if st["connected"]:
            items.append({"name": "连接", "ok": True, "msg": f"已连（{st['last_ok_at'] or '刚刚'}）", "action": ""})
        else:
            items.append({"name": "连接", "ok": False, "msg": st["text"],
                          "action": "点「重新连接」；仍失败就重新复制 token"})

        uid = self._uid()
        if not uid:
            items.append({"name": "身份", "ok": False, "msg": "没选发送人身份", "action": "在配置页下拉里选一个人"})
        else:
            plat = self._infer_platform(uid)
            if plat:
                items.append({"name": "身份", "ok": True, "msg": f"{uid[:16]}… @ {plat}", "action": ""})
            else:
                items.append({"name": "身份", "ok": False, "msg": "在会话库里找不到这个人",
                              "action": "先在微信里给机器人发句话，再回来查"})

        platform = (self.cfg.get("mirror_platform_id") or "").strip()
        if not self._push_enabled():
            items.append({"name": "推送", "ok": True, "msg": "已关闭（不影响对话）", "action": ""})
        elif not platform:
            items.append({"name": "推送", "ok": False, "msg": "不知道往哪个平台发", "action": "先在面板里确认微信号已登录"})
        elif self.context.get_platform_inst(platform) is None:
            items.append({"name": "推送", "ok": False, "msg": f"平台 {platform} 没在运行", "action": "检查微信机器人是否在线"})
        else:
            items.append({"name": "推送", "ok": True, "msg": "通道正常", "action": ""})

        return {"ok": True, "all_ok": all(i["ok"] for i in items), "status": st, "items": items}

    async def _h_diagnose(self, request: web.Request) -> web.Response:
        return web.json_response(self._diagnose_data())

    async def _h_reconnect(self, request: web.Request) -> web.Response:
        """手动重连云端 MCP（幂等，不会产生重复连接）。"""
        try:
            if self._mcp is None:
                token = (self.cfg.get("xiaozhi_token") or "").strip()
                if not token:
                    return web.json_response({"ok": False, "error": "还没填 Token"}, status=400)
                self._mcp = XiaoZhiMCPClient(
                    self.cfg, ask_fn=self._ask, mirror_fn=self._mirror,
                    send_fn=self._send_wechat, bots_fn=self._list_bots, play_fn=self._play_music,
                    extra_provider=lambda: self._extra_tools, extra_caller=self._call_extra_tool,
                )
            await self._mcp.stop()
            await asyncio.sleep(0.5)
            self._mcp.start()
            logger.info("[device_bridge] 手动重连已触发")
            return web.json_response({"ok": True, "msg": "已发起重连，10 秒后再看状态"})
        except Exception as exc:  # noqa: BLE001
            logger.error("[device_bridge] 重连失败：%s", exc)
            return web.json_response({"ok": False, "error": self._human_error(exc)}, status=500)

    def _log_lines(self, n: int = 50) -> list[str]:
        """不再写本地日志文件，日志统一走 astrbot 的 logger。"""
        return [
            "插件不再写自己的日志文件，日志统一由 AstrBot 记录。",
            "查看：面板 → 日志，或控制台/容器输出，过滤 [device_bridge]。",
        ]

    async def _h_logs(self, request: web.Request) -> web.Response:
        """最近 N 行日志（只读）。"""
        try:
            n = int(request.query.get("lines") or 50)
        except ValueError:
            n = 50
        return web.json_response({"ok": True, "lines": self._log_lines(n)})

    async def _h_logs_download(self, request: web.Request) -> web.StreamResponse:
        """不再提供日志下载：插件不落盘，日志统一由 AstrBot 记录。"""
        return web.json_response(
            {"ok": False, "error": "日志不再落盘，请到面板日志页或控制台查看"},
            status=410,
        )


    # ================= P2：工具扩展 / 指标 / 导入导出 =================
    def register_mcp_tool(self, name: str, description: str, schema: dict, handler) -> bool:
        """给别的插件留的口子：注册一个额外的 MCP 工具。

        handler 形如 async def handler(args: dict) -> str
        """
        try:
            if not name or not callable(handler):
                return False
            self._extra_tools[name] = {
                "name": name,
                "description": description or "",
                "inputSchema": schema or {"type": "object", "properties": {}},
                "handler": handler,
            }
            logger.info("[device_bridge] 已注册外部工具：%s", name)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[device_bridge] 注册工具失败：%s", exc)
            return False

    def unregister_mcp_tool(self, name: str) -> bool:
        return bool(self._extra_tools.pop(name, None))

    async def _call_extra_tool(self, name: str, args: dict) -> str:
        item = self._extra_tools.get(name)
        if not item:
            raise KeyError(name)
        return await item["handler"](args or {})

    async def _h_metrics(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, **self._metrics_snapshot()})

    async def _h_config_export(self, request: web.Request) -> web.Response:
        """导出配置（Token 打码），便于迁移。"""
        cp = "/AstrBot/data/config/astrbot_plugin_device_bridge_config.json"
        try:
            cur = json.load(open(cp, encoding="utf-8-sig")) if os.path.exists(cp) else {}
        except Exception:  # noqa: BLE001
            cur = {}
        safe = dict(cur)
        for k in ("xiaozhi_token", "astrbot_api_key"):
            v = str(safe.get(k) or "")
            safe[k] = (v[:6] + "***" + v[-4:]) if len(v) > 12 else ("***" if v else "")
        return web.json_response({"ok": True, "config": safe})

    async def _h_config_import(self, request: web.Request) -> web.Response:
        """导入配置：只接受白名单字段，未知键直接拒绝。"""
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "body 必须是 JSON"}, status=400)

        cfg = (data or {}).get("config") or {}
        if not isinstance(cfg, dict):
            return web.json_response({"ok": False, "error": "config 必须是对象"}, status=400)

        allowed = {
            "xiaozhi_token", "unified_user_id", "push_to_wechat", "require_trigger",
            "push_tag", "push_bot_tag", "push_user_tag", "push_with_question",
            "astrbot_session_id", "trigger_idle_minutes", "trigger_words", "exit_words",
            "user_alias", "quiet_hours", "multi_device_enable", "mirror_enable",
        }
        unknown = [k for k in cfg if k not in allowed]
        if unknown:
            return web.json_response(
                {"ok": False, "error": f"存在不认识的字段：{', '.join(unknown[:5])}"}, status=400
            )

        cp = "/AstrBot/data/config/astrbot_plugin_device_bridge_config.json"
        try:
            cur = json.load(open(cp, encoding="utf-8-sig")) if os.path.exists(cp) else {}
            # 打码值不覆盖真实密钥
            if "***" in str(cfg.get("xiaozhi_token", "")):
                cfg.pop("xiaozhi_token", None)
            cur.update(cfg)
            with open(cp, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=2)
            logger.info("[device_bridge] 已导入配置：%s", ", ".join(cfg.keys()) or "无")
            asyncio.create_task(self._reload_self())
            return web.json_response({"ok": True, "imported": list(cfg.keys())})
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "error": str(exc)}, status=500)



    async def _h_selftest_sid(self, request: web.Request) -> web.Response:
        """看不同设备号会被解析成哪个会话。"""
        dev = request.query.get("dev") or ""
        return web.json_response({
            "ok": True,
            "multi_device": bool(self.cfg.get("multi_device_enable", False)),
            "no_dev": self._resolve_sid(None),
            "with_dev": self._resolve_sid(dev or "devA"),
        })

    async def _h_selftest_tool(self, request: web.Request) -> web.Response:
        """临时注册一个假工具，走一遍注册→列表→调用→注销。"""
        name = "selftest_echo"

        async def _echo(args: dict) -> str:
            return f"echo:{args.get('text', '')}"

        try:
            ok = self.register_mcp_tool(name, "自测用", {"type": "object", "properties": {"text": {"type": "string"}}}, _echo)
            listed = name in (self._mcp.tools and [t["name"] for t in self._mcp.tools]) if self._mcp else False
            out = await self._call_extra_tool(name, {"text": "hi"})
            self.unregister_mcp_tool(name)
            return web.json_response({"ok": True, "registered": ok, "listed": listed, "call_result": out})
        except Exception as exc:  # noqa: BLE001
            self.unregister_mcp_tool(name)
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    async def _h_mode(self, request: web.Request) -> web.Response:
        """GET 看当前模式；POST {"name":"push"|"trigger", "enabled":true} 切换。"""
        if request.method == "POST":
            try:
                data = await request.json()
            except Exception:  # noqa: BLE001
                data = {}
            name = str((data or {}).get("name") or "")
            enabled = bool((data or {}).get("enabled"))
            if name == "push":
                self._set_push_enabled(enabled)
            elif name == "trigger":
                self._set_trigger_enabled(enabled)
            else:
                return web.json_response({"ok": False, "error": "name 只能是 push 或 trigger"}, status=400)
        return web.json_response({
            "ok": True,
            "push": self._push_enabled(),
            "trigger": self._trigger_enabled(),
            "alias": self._alias(),
            "quiet_hours": (self.cfg.get("quiet_hours") or "").strip(),
            "in_quiet": self._in_quiet(),
            "multi_device": bool(self.cfg.get("multi_device_enable", False)),
        })

    # ================= 定期清理（省磁盘） =================
    async def _cleanup_loop(self) -> None:
        """每隔 N 小时清理一次音频缓存。"""
        if not self.cfg.get("music_cleanup_enable", True):
            return
        hours = float(self.cfg.get("music_cleanup_hours") or 6)
        while True:
            try:
                await asyncio.sleep(max(600, hours * 3600))
                logger.info("[device_bridge] 定期清理：%s", self._cleanup_music_cache())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[device_bridge] 定期清理异常：%s", exc)

    def _cleanup_music_cache(self) -> dict:
        """先按天数删，再按容量上限删最旧的。"""
        cache_dir = (self.cfg.get("music_cache_dir") or "/AstrBot/data/temp/music").strip()
        keep_days = float(self.cfg.get("music_keep_days") or 3)
        max_mb = float(self.cfg.get("music_max_mb") or 200)

        if not os.path.isdir(cache_dir):
            return {"removed": 0, "freed_mb": 0, "dir": cache_dir}

        removed, freed = 0, 0
        now = time.time()
        files = []
        for name in os.listdir(cache_dir):
            fp = os.path.join(cache_dir, name)
            if not os.path.isfile(fp):
                continue
            try:
                st = os.stat(fp)
            except OSError:
                continue
            if keep_days > 0 and now - st.st_mtime > keep_days * 86400:
                try:
                    os.remove(fp)
                    removed += 1
                    freed += st.st_size
                except OSError:
                    pass
                continue
            files.append((st.st_mtime, st.st_size, fp))

        total = sum(s for _, s, _ in files)
        limit = max_mb * 1024 * 1024
        if max_mb > 0 and total > limit:
            for _, size, fp in sorted(files):
                if total <= limit:
                    break
                try:
                    os.remove(fp)
                    removed += 1
                    freed += size
                    total -= size
                except OSError:
                    pass

        return {"removed": removed, "freed_mb": round(freed / 1048576, 1), "dir": cache_dir}

    # ================= 本机小接口 =================
    async def _serve(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self._h_health)
        app.router.add_get("/diagnose", self._h_diagnose)
        app.router.add_post("/device/reconnect", self._h_reconnect)
        app.router.add_get("/device/logs", self._h_logs)
        app.router.add_get("/device/logs/download", self._h_logs_download)
        app.router.add_get("/device/metrics", self._h_metrics)
        app.router.add_get("/device/config/export", self._h_config_export)
        app.router.add_post("/device/config/import", self._h_config_import)
        app.router.add_get("/device/mode", self._h_mode)
        app.router.add_get("/device/selftest/tool", self._h_selftest_tool)
        app.router.add_get("/device/selftest/sid", self._h_selftest_sid)
        app.router.add_post("/device/mode", self._h_mode)
        app.router.add_get("/config", self._h_config_page)
        app.router.add_get("/device/users", self._h_users)
        app.router.add_post("/device/config", self._h_config_save)
        app.router.add_get("/device/config", self._h_config_get)
        app.router.add_post("/device/chat", self._h_chat)
        app.router.add_post("/device/record", self._h_record)
        app.router.add_post("/device/reload-config", self._h_reload)
        app.router.add_get("/device/push", self._h_push)
        app.router.add_get("/device/music", self._h_music)
        app.router.add_get("/device/music/debug", self._h_music_debug)
        app.router.add_get("/media/{fname}", self._h_media)
        app.router.add_post("/device/cleanup", self._h_cleanup)
        app.router.add_post("/device/merge-history", self._h_merge_history)
        app.router.add_post("/device/push", self._h_push)

        # ---- OpenAI 兼容端点（给自建 xiaozhi-esp32-server 用） ----
        app.router.add_get("/v1/models", self._h_models)
        app.router.add_post("/v1/chat/completions", self._h_completions)

        self._runner = web.AppRunner(app)
        host = (self.cfg.get("local_host") or "127.0.0.1").strip()
        try:
            await self._runner.setup()
            await web.TCPSite(self._runner, host, self._local_port).start()
            logger.info("[device_bridge] 本机接口 http://%s:%s", host, self._local_port)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[device_bridge] 本机接口启动失败（端口可能被旧实例占用，不影响设备链路）：%s", exc
            )

    async def _h_health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "status": self._status(),
                "mcp_connected": bool(self._mcp and self._mcp.ws_url),
                "has_token": bool(self.cfg.get("xiaozhi_token")),
                "has_api_key": bool(self._api_key),
                "session": self.cfg.get("astrbot_session_id"),
                "mirror": self._mirror_umo() or None,
            }
        )

    async def _h_record(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "body 必须是 JSON"}, status=400)
        q = str((data or {}).get("user_text") or "").strip()
        a = str((data or {}).get("assistant_text") or "").strip()
        if not q:
            return web.json_response({"ok": False, "error": "user_text 不能为空"}, status=400)
        try:
            await self._mirror(q, a)
            return web.json_response({"ok": True})
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    async def _h_chat(self, request: web.Request) -> web.Response:
        """手动注入一条消息到平台会话（需要平台与用户配置齐全）。"""
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "body 必须是 JSON"}, status=400)

        text = str((data or {}).get("text") or "").strip()
        if not text:
            return web.json_response({"ok": False, "error": "text 不能为空"}, status=400)

        platform_id = (data or {}).get("platform_id") or self.cfg.get("mirror_platform_id")
        user_id = (data or {}).get("user_id") or self._uid()
        if not platform_id or not user_id:
            return web.json_response(
                {"ok": False, "error": "未配置 mirror_platform_id / mirror_user_id"}, status=400
            )

        platform = self.context.get_platform_inst(platform_id)
        if platform is None:
            return web.json_response({"ok": False, "error": "平台未运行"}, status=503)

        async with self._lock:
            event = self._build_event(platform, user_id, text)
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            original_send = event.send

            async def send_capture(chain):
                if not future.done():
                    future.set_result(chain)
                return await original_send(chain)

            try:
                event.send = send_capture  # type: ignore[method-assign]
            except Exception:  # noqa: BLE001
                pass

            platform.commit_event(event)
            try:
                chain = await asyncio.wait_for(future, timeout=REPLY_TIMEOUT)
            except asyncio.TimeoutError:
                return web.json_response({"ok": False, "error": "等回复超时"}, status=504)

            try:
                reply = chain.get_plain_text() if chain else ""
            except Exception:  # noqa: BLE001
                reply = str(chain)
            return web.json_response({"ok": True, "reply": reply})

    # ---- OpenAI 兼容：/v1/models ----
    async def _h_models(self, request: web.Request) -> web.Response:
        model = (self.cfg.get("openai_model_name") or "pengyuhan").strip() or "pengyuhan"
        return web.json_response(
            {
                "object": "list",
                "data": [
                    {
                        "id": model,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "astrbot-device-bridge",
                    }
                ],
            }
        )

    # ---- OpenAI 兼容：/v1/chat/completions ----
    async def _h_completions(self, request: web.Request) -> web.StreamResponse:
        if not self.cfg.get("openai_compat_enable", True):
            return web.json_response(
                {"error": {"message": "openai_compat_enable 未开启", "type": "disabled"}},
                status=403,
            )

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response(
                {"error": {"message": "body 必须是 JSON", "type": "invalid_request_error"}},
                status=400,
            )

        question = self._last_user_text(payload.get("messages") or [])
        if not question:
            return web.json_response(
                {"error": {"message": "empty user message", "type": "invalid_request_error"}},
                status=400,
            )

        model = payload.get("model") or (self.cfg.get("openai_model_name") or "pengyuhan")
        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        cfg = self.cfg | {"astrbot_api_key": self._api_key}
        session_id = payload.get("user") or payload.get("session_id")

        # 非流式
        if not payload.get("stream"):
            answer = await self._ask(question)
            if answer:
                asyncio.create_task(self._mirror(question, answer))
            return web.json_response(
                {
                    "id": cid,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": answer},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
            )

        # 流式
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)

        def frame(delta: dict, finish: str | None = None) -> bytes:
            obj = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()

        fixed_sid = (self.cfg.get("astrbot_session_id") or "xiaozhi-wx").strip()
        await self._seed_context()
        cfg["astrbot_session_id"] = fixed_sid

        collected: list[str] = []
        try:
            await resp.write(frame({"role": "assistant", "content": ""}))
            async for delta in ask_self_stream(cfg, question, fixed_sid):
                collected.append(delta)
                await resp.write(frame({"content": delta}))
            await resp.write(frame({}, finish="stop"))
            await resp.write(b"data: [DONE]\n\n")
        except Exception as exc:  # noqa: BLE001
            logger.error("[device_bridge] 流式异常：%s", exc)
            try:
                await resp.write(frame({"content": f"（桥接出错：{exc}）"}))
                await resp.write(b"data: [DONE]\n\n")
            except Exception:  # noqa: BLE001
                pass
        finally:
            text = sanitize("".join(collected))
            if text:
                asyncio.create_task(self._mirror(question, text))

        return resp

    @staticmethod
    def _last_user_text(messages: list) -> str:
        if not messages:
            return ""
        target = None
        for m in reversed(messages):
            if isinstance(m, dict) and m.get("role") == "user":
                target = m
                break
        target = target or messages[-1]
        content = target.get("content") if isinstance(target, dict) else str(target)
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "".join(
                str(p.get("text") or "") for p in content
                if isinstance(p, dict) and p.get("type") in ("text", "input_text")
            ).strip()
        return str(content or "").strip()




    async def _h_music_debug(self, request: web.Request) -> web.Response:
        """排障：看每个音源运行时读到的配置。"""
        plugin = self._music_plugin()
        if plugin is None:
            return web.json_response({"ok": False, "error": "点歌插件未加载"}, status=503)
        out = []
        try:
            for pl in plugin.players:
                cfg = getattr(pl, "platform_cfg", None)
                out.append({
                    "name": getattr(pl.platform, "name", "?"),
                    "display": getattr(pl.platform, "display_name", "?"),
                    "enable": getattr(cfg, "enable", None),
                    "priority": getattr(cfg, "priority", None),
                    "base_url": getattr(pl, "base_url", None),
                    "cfg_base_url": getattr(cfg, "base_url", None),
                })
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "error": str(exc)}, status=500)
        return web.json_response({"ok": True, "players": out})



    def _umo_of(self, cid: str) -> str:
        """按会话 ID 查它的 umo（user_id 列存的就是）。"""
        try:
            con = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
            try:
                row = con.execute(
                    "select user_id from conversations where conversation_id=? or inner_conversation_id=?",
                    (cid, cid),
                ).fetchone()
            finally:
                con.close()
            return (row[0] or "").strip() if row else ""
        except Exception:  # noqa: BLE001
            return ""

    async def _h_merge_history(self, request: web.Request) -> web.Response:
        """POST {"target_cid","source_cids","limit_per_source"} → 合并历史。

        直接读写 DB 里的 content（会话对象可能带缓存，不可靠），
        写回时走 conversation_manager 以便内存缓存同步。
        """
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "body 必须是 JSON"}, status=400)

        target = str((data or {}).get("target_cid") or "").strip()
        sources = (data or {}).get("source_cids") or []
        limit = int((data or {}).get("limit_per_source") or 200)
        hist_file = str((data or {}).get("history_file") or "").strip()
        if not target:
            return web.json_response({"ok": False, "error": "需要 target_cid"}, status=400)
        if not hist_file and not isinstance(sources, list):
            return web.json_response({"ok": False, "error": "需要 source_cids 或 history_file"}, status=400)

        try:
            base = self._db_history(target)
            if base is None:
                return web.json_response({"ok": False, "error": "目标会话不存在"}, status=404)

            def _txt(m):
                return self._msg_text(m).strip()

            seen = set()      # 只用于判断"来源里是否已经在基础会话里出现过"
            merged = []
            last_txt = None
            for m in base:
                if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
                    continue
                t = _txt(m)
                if not t:
                    continue
                seen.add(t)
                if t == last_txt:          # 相邻完全重复才合并
                    continue
                last_txt = t
                merged.append({"role": m["role"], "content": [{"type": "text", "text": t}]})

            before = len(merged)
            added = 0
            for s in sources:
                hist = self._db_history(str(s))
                if not hist:
                    continue
                items = ([m for m in hist if isinstance(m, dict) and m.get("role") in ("user", "assistant")])[-limit:] if limit > 0 else hist
                last = None
                for m in items:
                    t = _txt(m)
                    if not t or t in seen or t == last:
                        continue
                    last = t
                    seen.add(t)
                    merged.append({"role": m["role"], "content": [{"type": "text", "text": t}]})
                    added += 1

            umo = self._umo_of(target)
            if not umo:
                return web.json_response({"ok": False, "error": "找不到目标会话的 umo"}, status=404)

            conv_mgr = self.context.conversation_manager

            if hist_file:
                if not os.path.isfile(hist_file):
                    return web.json_response({"ok": False, "error": "history_file 不存在"}, status=400)
                restored = json.load(open(hist_file, encoding="utf-8"))
                if not isinstance(restored, list):
                    return web.json_response({"ok": False, "error": "history_file 必须是列表"}, status=400)
                await conv_mgr.update_conversation(umo, target, history=restored)
                logger.info("[device_bridge] 已按文件恢复 %s：%d 条", target, len(restored))
                return web.json_response({"ok": True, "restored": len(restored)})
            await conv_mgr.update_conversation(umo, target, history=merged)
            logger.info("[device_bridge] 合并历史：原 %d + 新增 %d = %d 条（目标 %s）",
                        before, added, len(merged), target)
            return web.json_response({"ok": True, "before": before, "added": added, "total": len(merged)})
        except Exception as exc:  # noqa: BLE001
            logger.error("[device_bridge] 合并历史失败：%s", exc)
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    def _db_history(self, cid: str):
        """从 DB 读某个会话的 content 列表；不存在返回 None。"""
        try:
            con = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
            try:
                row = con.execute(
                    "select content from conversations where conversation_id=? or inner_conversation_id=?",
                    (cid, cid),
                ).fetchone()
            finally:
                con.close()
            if not row or not row[0]:
                return None
            return json.loads(row[0]) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("[device_bridge] 读会话 %s 失败：%s", cid, exc)
            return None

    @staticmethod
    def _msg_text(msg) -> str:
        if isinstance(msg, str):
            return msg
        parts = (msg or {}).get("content") or []
        if isinstance(parts, str):
            return parts
        out = []
        for p in parts:
            if isinstance(p, dict) and p.get("type") in ("text", "plain"):
                out.append(str(p.get("text") or ""))
        return "".join(out)

    async def _h_cleanup(self, request: web.Request) -> web.Response:
        """手动触发一次清理。"""
        try:
            return web.json_response({"ok": True, **self._cleanup_music_cache()})
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    async def _h_media(self, request: web.Request) -> web.StreamResponse:
        """把下载好的音频发给设备（FileResponse 自带 Range 支持，设备拖动/分段都能用）。"""
        fname = os.path.basename(request.match_info.get("fname") or "")
        if not fname:
            raise web.HTTPNotFound()

        candidates = [
            (self.cfg.get("music_cache_dir") or "/AstrBot/data/temp/music").strip(),
            "/AstrBot/data/temp/music",
            "/tmp",
        ]
        path = ""
        for d in candidates:
            fp = os.path.join(d, fname)
            if os.path.isfile(fp):
                path = fp
                break
        if not path:
            raise web.HTTPNotFound()

        low = fname.lower()
        ctype = "audio/mpeg"
        if low.endswith(".flac"):
            ctype = "audio/flac"
        elif low.endswith(".m4a"):
            ctype = "audio/mp4"
        elif low.endswith(".aac"):
            ctype = "audio/aac"
        elif low.endswith(".wav"):
            ctype = "audio/wav"

        return web.FileResponse(
            path,
            headers={"Content-Type": ctype, "Accept-Ranges": "bytes", "Cache-Control": "public, max-age=3600"},
        )

    async def _h_music(self, request: web.Request) -> web.Response:
        """GET /device/music?keyword=起风了[&platform=netease] → 返回歌名与音频直链。"""
        keyword = (request.query.get("keyword") or "").strip()
        platform = (request.query.get("platform") or "").strip()
        if not keyword:
            return web.json_response({"ok": False, "error": "keyword 不能为空"}, status=400)
        song, err = await self._search_song(keyword, platform)
        if song is None:
            return web.json_response({"ok": False, "error": err}, status=404)
        return web.json_response({
            "ok": True,
            "name": song.name,
            "artists": song.artists,
            "duration": song.duration,
            "url": song.audio_url,
            "page": song.page_url,
            "cover": song.cover_url,
            "source": song.source,
        })

    async def _h_push(self, request: web.Request) -> web.Response:
        """GET 看状态；POST {"enabled": true/false} 切换。"""
        if request.method == "POST":
            try:
                data = await request.json()
            except Exception:  # noqa: BLE001
                data = {}
            if isinstance(data, dict) and "enabled" in data:
                self._set_push_enabled(bool(data["enabled"]))
        return web.json_response({"enabled": self._push_enabled()})

    async def _h_reload(self, request: web.Request) -> web.Response:
        try:
            mgr = self.context.astrbot_config_mgr
            mgr._load_all_configs()
            return web.json_response({"ok": True})
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    # ================= 事件构造 =================
    @staticmethod
    def _build_event(platform, user_id: str, text: str):
        abm = AstrBotMessage()
        abm.type = MessageType.FRIEND_MESSAGE
        abm.self_id = platform.meta().id
        abm.session_id = user_id
        abm.message_id = f"device-{uuid.uuid4().hex[:12]}"
        abm.sender = MessageMember(user_id=user_id, nickname=user_id)
        abm.message = [Plain(text)]
        abm.message_str = text
        abm.raw_message = {"from": "xiaozhi-device", "text": text}
        abm.timestamp = int(time.time())
        return platform.create_event(abm)
