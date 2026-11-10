"""直连 OpenAI 兼容端点的大模型通道

和 agent.py 的分工：
  * agent.py 走 OpenClaw 网关，那一侧带着几万 token 的 agent 上下文，适合「让它自己想办法」
  * 这里是**最小请求**：只发 system + 一句用户话（后续再带 tools），目标是把一句话
    变成一次工具调用，响应快到几秒级

配置全在 config.json 的 llm 节点，界面可改，不用重启容器。
"""
import json
import logging
import time

import aiohttp

from .agent import canonical, local_gateway_token, _is_local_url

log = logging.getLogger("llm")

DEFAULT_SYSTEM = (
    "你是小爱音箱的播放助手。只做两件事：判断用户想听什么、调用工具执行。"
    "不要解释过程，不要输出多余文本。"
)


def normalize_base(url):
    """把 base_url 统一成带 /v1 的形式：用户只填到域名也能用"""
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if u.endswith("/v1"):
        return u
    return u + "/v1"


class Llm:
    def __init__(self, cfg):
        self.cfg = cfg
        self.last_ms = 0
        self.last_at = 0.0
        self.last_error = ""
        self.online = False
        self.apply()

    def apply(self):
        """从配置里重读一遍（存完配置就调它，不必重启）"""
        c = self.cfg.get("llm") or {}
        self.enabled = bool(c.get("enabled", False))
        self.base_raw = normalize_base(c.get("base_url") or "")
        self.model = (c.get("model") or "").strip()
        self.key = (c.get("api_key") or "").strip()
        self.key_auto = False
        # 地址指向本机网关时不填密钥也能用：token 直接从挂进来的 openclaw.json 读，
        # 和 agent.py 是同一套做法，省得用户到处翻一串十六进制
        self.local = bool(self.base_raw) and _is_local_url(self.base_raw)
        if not self.key and self.local:
            _tk = local_gateway_token()[0]
            if _tk:
                self.key = _tk
                self.key_auto = True
        self.timeout = float(c.get("timeout") or 20)
        self.system = (c.get("system") or DEFAULT_SYSTEM).strip()

    @property
    def configured(self):
        return bool(self.base_raw and self.model)

    def status(self):
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "base_url": self.base_raw,
            "model": self.model,
            "has_key": bool(self.key),
            "key_auto": self.key_auto,
            "online": self.online,
            "last_ms": self.last_ms,
            "last_at": self.last_at,
            "last_error": self.last_error,
        }

    async def chat(self, messages, timeout=None):
        """一次对话，返回 {ok, reply, ms, error}。流式与非流式都兼容。"""
        if not self.configured:
            return {"ok": False, "reply": "", "ms": 0,
                    "error": "还没填 base_url 或模型名"}
        body = {"model": self.model, "stream": True, "messages": messages}
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        if self.local:
            # 本机 OpenClaw 网关要这两个头才认得出 agent 与后端模型
            headers["x-openclaw-agent-id"] = str(
                (self.cfg.get("openclaw") or {}).get("agent_id") or "main")
        t0 = time.perf_counter()
        limit = float(timeout or self.timeout)
        try:
            to = aiohttp.ClientTimeout(total=limit)
            async with aiohttp.ClientSession(timeout=to) as s:
                async with s.post(canonical(self.base_raw) + "/chat/completions",
                                  json=body, headers=headers) as r:
                    text = await r.text()
                    self.last_ms = int((time.perf_counter() - t0) * 1000)
                    self.last_at = time.time()
                    if r.status != 200:
                        self.online = False
                        self.last_error = "HTTP %s: %s" % (r.status, text[:160])
                        return {"ok": False, "reply": "", "ms": self.last_ms,
                                "error": self.last_error}
                    self.online = True
                    self.last_error = ""
                    reply = _parse_reply(text)
                    return {"ok": True, "reply": reply, "ms": self.last_ms,
                            "error": ""}
        except Exception as e:
            self.last_ms = int((time.perf_counter() - t0) * 1000)
            self.last_at = time.time()
            self.online = False
            self.last_error = str(e)[:200]
            log.warning("llm 请求失败：%s", e)
            return {"ok": False, "reply": "", "ms": self.last_ms,
                    "error": self.last_error}

    async def chat_tools(self, messages, tools, timeout=None):
        """带工具的一次调用（非流式）：返回 {ok, message, ms, error}

        非流式是为了拿到完整的 tool_calls；流式下工具参数是分片下发的，
        拼装容易出错，而这里每次都是短请求，非流式省不了多少时间。
        """
        if not self.configured:
            return {"ok": False, "ms": 0, "error": "还没填 base_url 或模型名"}
        # 必须流式：这个端点非流式会挂住不返回（和 agent.py 遇到的情况一样），
        # 流式下工具参数是分片下发的，交给 _collect_tool_sse 按 index 拼装
        body = {"model": self.model, "messages": messages, "stream": True}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        if self.local:
            headers["x-openclaw-agent-id"] = str(
                (self.cfg.get("openclaw") or {}).get("agent_id") or "main")
        t0 = time.perf_counter()
        limit = float(timeout or self.timeout)
        try:
            to = aiohttp.ClientTimeout(total=limit)
            async with aiohttp.ClientSession(timeout=to) as s:
                async with s.post(canonical(self.base_raw) + "/chat/completions",
                                  json=body, headers=headers) as r:
                    text = await r.text()
                    self.last_ms = int((time.perf_counter() - t0) * 1000)
                    self.last_at = time.time()
                    if r.status != 200:
                        self.online = False
                        self.last_error = "HTTP %s: %s" % (r.status, text[:160])
                        return {"ok": False, "ms": self.last_ms, "error": self.last_error}
                    self.online = True
                    self.last_error = ""
                    msg = _collect_tool_sse(text)
                    if msg is None:
                        return {"ok": False, "ms": self.last_ms,
                                "error": "返回解析失败：%s" % text[:120]}
                    return {"ok": True, "message": msg, "ms": self.last_ms, "error": ""}
        except Exception as e:
            self.last_ms = int((time.perf_counter() - t0) * 1000)
            self.last_at = time.time()
            self.online = False
            self.last_error = str(e)[:200]
            log.warning("llm 工具调用失败：%s", e)
            return {"ok": False, "ms": self.last_ms, "error": self.last_error}

    async def test(self, text="只回两个字：通了"):
        """界面上「测试连接」用：发一句话看能不能回"""
        msgs = [{"role": "system", "content": self.system},
                {"role": "user", "content": text}]
        return await self.chat(msgs)


def _parse_reply(text):
    """兼容三种返回：SSE（data: {...}）、带心跳注释的 SSE、普通 JSON

    有些网关（本项目实测过）会先发一行 SSE 注释心跳 ``: heartbeat``，
    报文因此不以 ``data:`` 开头。旧实现只看首行，会把整段原始报文
    当成回复返回——上层拿到一坨 ``data: {"choices":...`` 当人话用。
    """
    text = (text or "").strip()
    if not text:
        return ""
    # 去掉 SSE 注释行（以冒号开头）与空行，再看是不是流式
    lines = [ln for ln in text.splitlines()
             if ln.strip() and not ln.strip().startswith(":")]
    body = "\n".join(lines).strip()
    if body.startswith("data:"):
        out = []
        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            for ch in (obj.get("choices") or []):
                d = ch.get("delta") or {}
                piece = d.get("content")
                if piece:
                    out.append(piece)
        return "".join(out).strip()
    try:
        obj = json.loads(body)
    except Exception:
        return body[:200]
    for ch in (obj.get("choices") or []):
        msg = ch.get("message") or {}
        if msg.get("content"):
            return str(msg["content"]).strip()
        if ch.get("text"):
            return str(ch["text"]).strip()
    return ""


def _collect_tool_sse(text):
    """把流式分片拼成一条 message：正文 + tool_calls（按 index 合并参数）"""
    text = (text or "").strip()
    if not text:
        return None
    content = []
    calls = {}
    lines = text.splitlines()
    # 有些不走标准 SSE：可能先来一行 ": heartbeat" 保活，也可能直接回整段 JSON
    if not any(l.strip().startswith("data:") for l in lines):
        try:
            obj = json.loads(text)
        except Exception:
            return None
        ch = (obj.get("choices") or [{}])[0]
        return ch.get("message") or {}
    for line in lines:
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        for ch in (obj.get("choices") or []):
            d = ch.get("delta") or {}
            if d.get("content"):
                content.append(d["content"])
            for tc in (d.get("tool_calls") or []):
                i = tc.get("index", 0)
                cur = calls.setdefault(i, {"id": "", "type": "function",
                                           "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    cur["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    cur["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    cur["function"]["arguments"] += fn["arguments"]
    msg = {"role": "assistant", "content": "".join(content)}
    if calls:
        msg["tool_calls"] = [calls[k] for k in sorted(calls)]
    return msg
