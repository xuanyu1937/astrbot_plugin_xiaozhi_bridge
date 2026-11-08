"""配置视图：集中默认值与派生字段，读取时一律钳到安全区间。"""
from __future__ import annotations

from datetime import datetime

DEFAULTS = {
    "xiaozhi_token": "",
    "astrbot_api_key": "",
    "astrbot_base_url": "http://127.0.0.1:6185",
    "request_timeout": 120,
    "unified_user_id": [],
    "push_target_umo": "",
    "push_platform_id": "",
    "push_to_wechat": True,
    "push_with_question": True,
    "push_bot_tag": "【设备】",
    "push_user_tag": "【我】",
    "mirror_enable": True,
    "require_trigger": True,
    "trigger_words": ["小助手"],
    "exit_words": ["退出", "不聊了", "结束", "换回小智"],
    "trigger_idle_minutes": 30,
    "user_alias": "哥哥",
    "quiet_hours": "",
    "astrbot_session_id": "xiaozhi-wx",
    "multi_device_enable": False,
}


def as_list(value, default=None):
    """列表配置兼容旧版的逗号分隔字符串。"""
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        raw = value.replace("，", ",")
        return [x.strip() for x in raw.split(",") if x.strip()]
    return list(default or [])


def clamp_int(value, default, low, high):
    """数值配置一律钳区间，缺项或写错都不至于崩。"""
    try:
        num = int(value)
    except (TypeError, ValueError):
        num = int(default)
    return max(low, min(high, num))


class Settings:
    """只读配置视图。"""

    def __init__(self, config):
        self._cfg = config or {}

    def raw(self, key):
        value = self._cfg.get(key)
        if value is None or value == "":
            return DEFAULTS.get(key, "")
        return value

    def str(self, key, default=""):
        value = self.raw(key)
        return str(value) if value is not None else default

    def int(self, key, low=0, high=10**6):
        return clamp_int(self.raw(key), DEFAULTS.get(key, 0), low, high)

    def bool(self, key):
        value = self.raw(key)
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("1", "true", "yes", "on", "是")

    def list(self, key):
        return as_list(self.raw(key), DEFAULTS.get(key, []))

    # ---------- 派生字段 ----------
    @property
    def token(self):
        return self.str("xiaozhi_token").strip()

    @property
    def api_key(self):
        return self.str("astrbot_api_key").strip()

    @property
    def base_url(self):
        return self.str("astrbot_base_url", "http://127.0.0.1:6185").rstrip("/")

    @property
    def sender_id(self):
        ids = self.list("unified_user_id")
        return ids[0] if ids else ""

    @property
    def device_session_id(self):
        return self.str("astrbot_session_id", "xiaozhi-wx").strip() or "xiaozhi-wx"

    def device_umo(self, device_id=""):
        """设备会话：webchat 下按发送人 + 会话标识拼，多设备时追加设备后缀。"""
        uid = self.sender_id or "local"
        sid = self.device_session_id
        if self.bool("multi_device_enable") and device_id:
            sid = f"{sid}-{device_id}"
        return f"webchat:FriendMessage:webchat!{uid}!{sid}"

    @property
    def push_umo(self):
        target = self.str("push_target_umo").strip()
        if target:
            return target
        uid = self.sender_id
        if not uid:
            return ""
        platform = self.str("push_platform_id").strip() or "weixin_personal_ezgt"
        return f"{platform}:FriendMessage:{uid}"

    @property
    def alias(self):
        return self.str("user_alias", "哥哥").strip() or "哥哥"

    @property
    def persona(self):
        return self.str("persona_label").strip()

    @property
    def trigger_words(self):
        return self.list("trigger_words") or ["小助手"]

    @property
    def exit_words(self):
        return self.list("exit_words")

    def in_quiet_hours(self, now=None):
        """静音时段判断，支持 23:30-07:30 这种跨零点写法。"""
        span = self.str("quiet_hours").strip()
        if "-" not in span:
            return False
        start_s, end_s = span.split("-", 1)
        try:
            start = datetime.strptime(start_s.strip(), "%H:%M").time()
            end = datetime.strptime(end_s.strip(), "%H:%M").time()
        except ValueError:
            return False
        cur = (now or datetime.now()).time()
        if start <= end:
            return start <= cur <= end
        return cur >= start or cur <= end
