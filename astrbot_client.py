# -*- coding: utf-8 -*-
"""插件内部与 AstrBot 自身对话的客户端。

复用 AstrBot 的 Open API（本机 127.0.0.1:6185/api/v1/chat），
这样设备侧拿到的回复与 WebChat 会话完全一致（同一人格、同一套工具）。
"""

from __future__ import annotations

import json
import re

import httpx

_MARKER_RE = re.compile(r"&&[A-Za-z0-9_]+&&")
_URL_RE = re.compile(r"https?://\S+")


_TOOLJSON_RE = re.compile(r'\{[^{}]*"id"\s*:\s*"call_[^{}]*\}')
_TOOLJSON_RE2 = re.compile(r'\{"id"\s*:\s*"call_[\s\S]*?\}\s*$', re.M)


def sanitize(text: str) -> str:
    """去掉表情包标记、markdown 符号、以及工具调用 JSON 残渣，避免 TTS 念出来。"""
    text = text or ""
    # 工具调用痕迹：{"id": "call_xxx", "ts": ..., "result": ...}
    text = _TOOLJSON_RE.sub("", text)
    text = _TOOLJSON_RE2.sub("", text)
    text = re.sub(r'"id"\s*:\s*"call_[^"]*"', "", text)
    text = _URL_RE.sub("", text)          # 链接会被 TTS 逐字念出来，直接去掉
    text = _MARKER_RE.sub("", text)
    text = text.translate(str.maketrans("", "", "*`~"))
    text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)
    lines = []
    for line in text.splitlines():
        s = line.strip()
        # 整行只是 JSON / 工具回显的，丢掉
        if s.startswith("{") and s.endswith("}"):
            continue
        if '"result"' in s and '"call_' in s:
            continue
        if s.startswith("[") and s.endswith("]") and "call_" in s:
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def ask_self_stream(cfg: dict, question: str, session_id: str | None = None):
    """流式版：逐段吐出清洗后的增量文本（给 OpenAI 兼容接口用）。"""
    base = (cfg.get("astrbot_base_url") or "http://127.0.0.1:6185").rstrip("/")
    key = cfg.get("astrbot_api_key") or ""
    if not key:
        yield "（插件未配置 astrbot_api_key）"
        return

    body = {
        "message": question,
        "session_id": session_id or cfg.get("astrbot_session_id") or "xiaozhi-wx",
        "username": cfg.get("astrbot_username") or "哥哥",
        "enable_streaming": True,
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    accumulated = ""
    emitted = ""
    snapshot = ""
    timeout = httpx.Timeout(float(cfg.get("timeout") or 60), connect=10.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST", f"{base}/api/v1/chat", json=body, headers=headers
        ) as resp:
            if resp.status_code != 200:
                raw = (await resp.aread())[:300].decode("utf-8", "replace")
                yield f"（AstrBot 返回 {resp.status_code}：{raw}）"
                return

            async for line in resp.aiter_lines():
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    evt = json.loads(chunk)
                except json.JSONDecodeError:
                    continue

                etype = evt.get("type")
                if etype == "run_snapshot" and not snapshot:
                    try:
                        parts = ((evt.get("data") or {}).get("content") or {}).get("message") or []
                        snapshot = "".join(
                            p.get("text") or "" for p in parts if p.get("type") in ("plain", "text")
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    continue

                if etype == "complete":
                    final = evt.get("data")
                    if isinstance(final, dict):
                        final = final.get("text") or ""
                    if isinstance(final, str) and final.strip():
                        accumulated = final
                        current = sanitize(accumulated)
                        if current.startswith(emitted):
                            delta = current[len(emitted):]
                        else:
                            delta = current
                        emitted = current
                        if delta:
                            yield delta
                    continue

                if etype != "plain":
                    continue
                if evt.get("chain_type") in ("reasoning", "tool", "tool_call", "function"):
                    continue
                data = evt.get("data")
                if isinstance(data, dict):
                    data = data.get("text") or ""
                if not isinstance(data, str) or not data:
                    continue

                accumulated += data
                current = sanitize(accumulated)
                if current.startswith(emitted):
                    delta = current[len(emitted):]
                else:
                    delta = current
                emitted = current
                if delta:
                    yield delta

    if not emitted and snapshot:
        text = sanitize(snapshot)
        if text:
            yield text


async def ask_self(cfg: dict, question: str, session_id: str | None = None) -> str:
    """把一句话发给 AstrBot 本体，取回文本回复。"""
    base = (cfg.get("astrbot_base_url") or "http://127.0.0.1:6185").rstrip("/")
    key = cfg.get("astrbot_api_key") or ""
    if not key:
        return "嗯，我这边还没配好密钥。"

    body = {
        "message": question,
        "session_id": session_id or cfg.get("astrbot_session_id") or "xiaozhi-wx",
        "username": cfg.get("astrbot_username") or "哥哥",
        "enable_streaming": True,
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
    }

    buf: list[str] = []
    snapshot = ""
    started = None
    timeout = httpx.Timeout(float(cfg.get("timeout") or 60), connect=10.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST", f"{base}/api/v1/chat", json=body, headers=headers
        ) as resp:
            if resp.status_code != 200:
                raw = (await resp.aread())[:300].decode("utf-8", "replace")
                return f"（AstrBot 返回 {resp.status_code}：{raw}）"

            async for line in resp.aiter_lines():
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    evt = json.loads(chunk)
                except json.JSONDecodeError:
                    continue

                etype = evt.get("type")
                if etype == "run_snapshot" and not snapshot:
                    try:
                        parts = ((evt.get("data") or {}).get("content") or {}).get("message") or []
                        snapshot = "".join(
                            p.get("text") or "" for p in parts if p.get("type") in ("plain", "text")
                        )
                    except Exception:  # noqa: BLE001
                        pass
                elif etype == "plain":
                    if evt.get("chain_type") in ("reasoning", "tool", "tool_call", "function"):
                        continue
                    data = evt.get("data")
                    if isinstance(data, dict):
                        data = data.get("text") or ""
                    if isinstance(data, str) and data:
                        buf.append(data)

    text = sanitize("".join(buf)) or sanitize(snapshot)
    if text:
        return text
    # 空回复：给设备一句能直接读出口的短句，避免它判定超时/卡顿
    return "嗯，我在。"
