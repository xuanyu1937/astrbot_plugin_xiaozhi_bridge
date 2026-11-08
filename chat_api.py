"""调用 AstrBot 官方 Open API，并把回复清洗成能直接念出来的文本。"""
from __future__ import annotations

import json
import re

import httpx


class ChatError(RuntimeError):
    """调用失败时抛出，带可读中文原因。"""


_JUNK = [
    # 工具调用记录的整段 JSON：从 {"id": "call_ 开始，一直吞到配对的 } 为止
    re.compile(r"\{*\s*\"id\"\s*:\s*\"call_[\s\S]*?\"(?:result|ts)\"[\s\S]*?\}\s*\}*"),
    re.compile(r"\"id\"\s*:\s*\"call_[\s\S]*?\"?\}"),
    re.compile(r"```[\s\S]*?```"),          # 代码块整段丢掉
    re.compile(r"`{1,3}[^`\n]{1,200}`{1,3}"),  # 行内代码
    re.compile(r"&&[^&]{1,32}&&"),
    re.compile(r"https?://\S+"),
]
_SKIP_CHAIN = ("reasoning", "tool", "tool_call", "function")


def sanitize(text: str, limit: int = 300) -> str:
    """去掉表情包标记、URL、工具调用残渣和 Markdown 记号。"""
    out = str(text or "")
    for pattern in _JUNK:
        out = pattern.sub("", out)
    out = re.sub(r"[*`~]+", "", out)
    out = re.sub(r"^#{1,6}\s*", "", out, flags=re.M)
    out = out.strip(" }]{[\"',")
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = out.strip()
    # 推送和朗读都不适合太长：超长按句末截断，避免把整段日志念出来
    if limit and len(out) > limit:
        cut = out[:limit]
        for mark in ("。", "！", "？", "\n"):
            pos = cut.rfind(mark)
            if pos > limit * 0.5:
                return cut[: pos + 1].strip()
        return cut.strip() + "…"
    return out


def _snapshot_text(event: dict) -> str:
    """run_snapshot 里是完整消息链，只挑纯文本。"""
    try:
        parts = ((event.get("data") or {}).get("content") or {}).get("message") or []
        return "".join(
            str(p.get("text") or "")
            for p in parts
            if isinstance(p, dict) and p.get("type") in ("plain", "text")
        )
    except Exception:  # noqa: BLE001
        return ""


async def ask_astrbot(
    base_url: str,
    api_key: str,
    message: str,
    session_id: str,
    username: str,
    timeout: int = 120,
    max_chars: int = 300,
) -> str:
    """POST /api/v1/chat，按 SSE 取增量文本；拿不到就回退 run_snapshot。"""
    if not api_key:
        raise ChatError("还没配置 AstrBot API Key，去插件配置里填一下")

    url = f"{base_url.rstrip('/')}/api/v1/chat"
    body = {
        "message": message,
        "session_id": session_id,
        "username": username,
        "enable_streaming": True,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
    }

    chunks: list[str] = []
    snapshot = ""
    try:
        client_timeout = httpx.Timeout(timeout, connect=10.0)
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            stream = client.stream("POST", url, json=body, headers=headers)
            async with stream as resp:
                if resp.status_code != 200:
                    raw = (await resp.aread())[:200].decode("utf-8", "replace")
                    raise ChatError(f"AstrBot 返回 {resp.status_code}：{raw}")
                async for line in resp.aiter_lines():
                    if not line or line.startswith(":") or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type")
                    if etype == "run_snapshot" and not snapshot:
                        snapshot = _snapshot_text(event)
                    elif etype == "plain":
                        if event.get("chain_type") in _SKIP_CHAIN:
                            continue
                        data = event.get("data")
                        if isinstance(data, dict):
                            data = data.get("text") or ""
                        if isinstance(data, str) and data:
                            chunks.append(data)
    except ChatError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ChatError(f"连不上 AstrBot：{exc}") from exc

    return sanitize("".join(chunks), max_chars) or sanitize(snapshot, max_chars)
