"""OpenClaw 网关客户端（可选组件）

定位说清楚：OpenClaw 只负责"NAS 上有什么 / 怎么样了"这类需要 agent 知识与
工具编排的问题。**播放链路不经过它**，所以它连不上时，歌照放、音量照调。

走 OpenAI 兼容端点 POST /v1/chat/completions。
需在 OpenClaw 配置里开启 gateway.http.endpoints.chatCompletions.enabled = true
鉴权：Authorization: Bearer <gateway token>
会话：x-openclaw-session-key 固定值即可复用同一会话
"""
import logging
import asyncio
import os
import re
import json
import time

import aiohttp

log = logging.getLogger("agent")

# 容器里整目录挂进来的 OpenClaw 配置（cmd/main: .openclaw -> /oc）
OC_CONFIG = os.environ.get("OC_CONFIG", "/oc/openclaw.json")

# 口播压缩的指令。要点逐条写死，别指望模型自己领会：
# 保留问句（否则用户没法回答）、不加新内容、只输出这一句。
SHRINK_PROMPT = (
    "把用户发来的这段回答改写成一句能直接念出来的口播，"
    "字数上限 %d 个汉字，这是硬性要求。"
    "写完后自己数一遍，超了就删掉次要细节，只保留结论和需要用户回答的问题。"
    "其他要求：先给结论；口语化；不要 markdown、列表、代码、链接、表情符号；"
    "原文中需要用户决定的问题必须保留；不要添加原文没有的信息；"
    "只输出改写后的这一句，不要解释、不要前缀、不要引号。"
)


def in_container():
    return os.path.exists("/.dockerenv")


def local_gateway_token():
    """读本机 OpenClaw 网关的 token（gateway.auth.token）。

    桥接和网关跑在同一台 NAS 上，配置文件就挂在这里，token 完全可读；
    用户重装后不必再去 OpenClaw 里翻一串十六进制粘贴过来。
    """
    try:
        with open(OC_CONFIG, encoding="utf-8") as f:
            g = (json.load(f).get("gateway") or {})
        return str(((g.get("auth") or {}).get("token")) or "").strip(), g.get("port")
    except Exception:
        return "", None


def _is_local_url(url):
    """地址是不是指向本机（容器内外两种写法都算）"""
    m = re.match(r"^https?://([^/:]+)", url or "")
    if not m:
        return False
    return m.group(1) in ("127.0.0.1", "localhost", "::1", "0.0.0.0",
                          "host.docker.internal", host_ip())


def host_ip():
    """宿主机的局域网 IP：启动脚本按 XIAOAI_PUBLIC_BASE 注入"""
    ip = os.environ.get("XIAOAI_HOST_IP") or ""
    if ip:
        return ip
    m = re.match(r"https?://([^:/]+)", os.environ.get("XIAOAI_PUBLIC_BASE") or "")
    return m.group(1) if m else "127.0.0.1"


def canonical(url):
    """把「宿主机视角」的地址换成容器里真正能连上的地址

    容器跑在独立网络里，127.0.0.1 指向它自己，宿主机局域网 IP 又被防火墙挡住，
    唯一能到宿主回环的是 host.docker.internal。所以配置里写 127.0.0.1 或本机
    局域网 IP 都行，发送前在这里统一改写，界面上就不用露出 host.docker.internal。
    """
    if not url or not in_container():
        return url
    m = re.match(r"^(https?://)([^/:]+)(:\d+)?(/.*)?$", url)
    if not m:
        return url
    scheme, host, port, path = m.groups()
    if host in ("127.0.0.1", "localhost", "::1", host_ip()):
        return f"{scheme}host.docker.internal{port or ''}{path or ''}"
    return url


async def _collect_sse(resp):
    """把 stream=True 的 SSE 分片拼成 OpenAI 非流式那样的 data 结构。"""
    import json as _json
    parts = []
    async for raw in resp.content:
        line = raw.decode("utf-8", "ignore").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = _json.loads(payload)
        except Exception:
            continue
        for ch in obj.get("choices") or []:
            piece = (ch.get("delta") or {}).get("content")
            if piece:
                parts.append(piece)
    return {"choices": [{"message": {"content": "".join(parts)}}]}


class Agent:
    def __init__(self, auth, cfg):
        self.cfg = cfg
        self.session = auth.session
        self.online = None            # None=未知 True=通 False=不通
        self.last_error = ""
        self.last_ms = None
        self.last_at = 0.0
        self._last_probe = 0.0
        self.apply(cfg)

    # ---------- 配置 ----------
    def apply(self, cfg):
        c = cfg.openclaw or {}
        # base_raw = 配置里写的样子（给界面看），base = 容器内实际能连的地址
        self.base_raw = (c.get("base_url") or "").rstrip("/")
        self.base = canonical(self.base_raw)
        self.token = c.get("token") or ""
        # 本机网关：token 直接从挂进来的 openclaw.json 里取，免手工填
        self.token_auto = False
        if not self.token and self.base_raw and _is_local_url(self.base_raw):
            _tk = local_gateway_token()[0]
            if _tk:
                self.token = _tk
                self.token_auto = True
        self.agent_id = c.get("agent_id") or "main"
        self.session_key = c.get("session_key") or "xiaoai:main"
        self.model = c.get("model") or "openclaw"
        # timeout: 0 / 不填 = 不限制（让 agent 想多久都行，它自己会把结果交给音箱）
        _t = c.get("timeout", 0)
        self.timeout = None if _t in (0, "0", None) else float(_t)
        self.system_prefix = c.get("system_prefix") or ""
        self.enabled = bool(self.base) and c.get("enabled", True) is not False
        return self

    @property
    def configured(self):
        return bool(self.base)

    def status(self):
        return {
            "configured": self.configured,
            "enabled": getattr(self, "enabled", False),
            "base_url": self.base_raw,
            "agent_id": self.agent_id,
            "session_key": self.session_key,
            "model": self.model,
            "timeout": self.timeout,
            "has_token": bool(self.token),
            "token_auto": getattr(self, "token_auto", False),
            "online": self.online,
            "last_error": self.last_error,
            "last_ms": self.last_ms,
            "last_at": int(self.last_at) if self.last_at else 0,
        }

    # ---------- 探测 ----------
    async def probe(self, force=False, ttl=30.0):
        """网关是否可达。结果缓存 30 秒，免得 WebUI 轮询把网关敲出火星。"""
        now = time.time()
        if not force and self.online is not None and now - self._last_probe < ttl:
            return self.online
        self._last_probe = now
        if not self.configured:
            self.online = False
            self.last_error = "未配置 base_url"
            return False
        t0 = time.perf_counter()
        try:
            async with self.session.get(f"{self.base}/health",
                                        timeout=6) as r:
                self.online = r.status < 500
                self.last_error = "" if self.online else f"HTTP {r.status}"
        except Exception as e:
            self.online = False
            self.last_error = f"{type(e).__name__}: {e}"
        self.last_ms = int((time.perf_counter() - t0) * 1000)
        return self.online

    # ---------- 对话 ----------
    async def chat(self, text, timeout=None):
        """把一句话交给 agent，返回纯文本。失败返回可播报的兜底话术。"""
        if not self.configured:
            log.warning("未配置 openclaw.base_url，返回回声（用于自测链路）")
            self.online = False
            self.last_error = "未配置 base_url"
            return f"收到：{text}"
        # 网关 /v1/models 只暴露 agent 别名；真实模型用 x-openclaw-model 头传。
        # 必须流式：非流式在该接口会挂住不返回（实测 120s 超时）。
        _m = (self.model or "").strip()
        if _m and not _m.startswith("openclaw"):
            route_model = f"openclaw/{self.agent_id or 'main'}"
            backend_model = _m
        else:
            route_model = _m or f"openclaw/{self.agent_id or 'main'}"
            backend_model = ""
        body = {
            "model": route_model,
            "stream": True,
            "messages": [
                {"role": "system", "content": self.system_prefix},
                {"role": "user", "content": text},
            ],
            # 让每台音箱各自一个稳定会话
            "user": self.session_key,
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "x-openclaw-agent-id": self.agent_id,
            "x-openclaw-session-key": self.session_key,
        }
        if backend_model:
            headers["x-openclaw-model"] = backend_model
        t0 = time.perf_counter()
        try:
            limit = timeout if timeout is not None else self.timeout
            # 没有限制时显式给 aiohttp 一个"永不超时"的 ClientTimeout，
            # 否则会落到 session 默认的 total 上限，长任务照样被杀。
            to = aiohttp.ClientTimeout(total=None) if limit is None else limit
            async with self.session.post(
                f"{self.base}/v1/chat/completions",
                json=body,
                headers=headers,
                timeout=to,
            ) as r:
                self.last_ms = int((time.perf_counter() - t0) * 1000)
                self.last_at = time.time()
                if r.status != 200:
                    body_txt = await r.text()
                    log.warning("openclaw 返回 %s: %s", r.status, body_txt[:300])
                    self.online = r.status < 500
                    self.last_error = f"HTTP {r.status}: {body_txt[:120]}"
                    return "后台处理出错了，稍后再试"
                data = await _collect_sse(r)
                self.online = True
                self.last_error = ""
        except Exception as e:
            self.last_ms = int((time.perf_counter() - t0) * 1000)
            self.last_at = time.time()
            self.online = False
            self.last_error = f"{type(e).__name__}: {e}"
            log.warning("openclaw 调用异常: %s", e)
            return "后台连不上，稍后再试"

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            log.warning("openclaw 响应格式异常: %s", str(data)[:300])
            return "后台返回的内容我没看懂"
        return (content or "").strip() or "处理完了，但没话可说"

    # ---------- 口播压缩 ----------
    def _candidates(self):
        """按优先级列出能直连的模型入口。

        OpenClaw 网关每次请求都要拼 4.7 万字符的 agent 提示，压一句 60 字的话
        也要好几秒；这里优先走模型网关的原始入口，实测 2~4 秒出结果。

        配置项都可选：openclaw.direct_base / direct_key / direct_model。
        没配就翻挂载进来的 openclaw.json，挑指向本机的 provider。
        """
        c = self.cfg.openclaw or {}
        base = (c.get("direct_base") or "").strip().rstrip("/")
        if base:
            return [(canonical(base),
                     (c.get("direct_key") or "").strip(),
                     (c.get("direct_model") or "").strip())]
        try:
            with open(OC_CONFIG, encoding="utf-8") as f:
                providers = ((json.load(f).get("models") or {}).get("providers") or {})
        except Exception:
            return None
        # 先筛出能当聊天接口用的 provider：
        #   ollama 的调用路径不是 /chat/completions，直接排除；
        #   只挂 embedding / 重排 / 语音模型的也排除（实测第一个就是 bge-m3）。
        skip_words = ("embed", "bge", "rerank", "whisper", "tts", "dall", "sd-")
        cands = []
        for name, v in providers.items():
            api = str(v.get("api") or "").lower()
            if api and "openai" not in api:
                continue
            base = (v.get("baseUrl") or "").strip().rstrip("/")
            if not base or not _is_local_url(base):
                continue
            ids = [m.get("id") for m in (v.get("models") or []) if m.get("id")]
            chat = [m for m in ids
                    if not any(w in m.lower() for w in skip_words)]
            if not chat:
                continue
            # 排序：pro 优先（实测 2.3 秒且只输出结论），flash 垫底——
            # 2026-11-11 实测 flash 配额打满会直接 429（要等到 17:38 才重置）
            def _rank(m):
                m = m.lower()
                if "flash" in m:
                    return 2
                if "pro" in m:
                    return 0
                return 1
            for mid in chat:
                cands.append((_rank(mid), 0 if base.endswith("/v1") else 1,
                              len(cands), canonical(base),
                              (v.get("apiKey") or ""), mid))
        if not cands:
            return None
        cands.sort()
        # 容器里 127.0.0.1 是自己，必须换成宿主机；沿用和对话同一条改写规则
        return [(b, k, m) for _, _, _, b, k, m in cands]

    def _direct(self):
        """首个候选入口（兼容旧调用点）"""
        cands = self._candidates()
        return cands[0] if cands else None

    async def shrink(self, text, max_chars=60, timeout=8.0):
        """把一段回答压成能念的一句。失败返回空串，调用方按原文播。

        依次试候选模型：首选 429 / 超时就换下一个，总时长不超过 timeout。
        """
        text = (text or "").strip()
        if not text:
            return ""
        cands = (self._candidates() or [])[:2]
        if not cands:
            log.debug("没有可直连的模型入口，跳过口播压缩")
            return ""
        per = max(3.0, timeout / 2)
        # 容差 1.6 倍：实测模型对「60字」能压到 82 字左右，再压一次只少 40 字
        # 却要多花 2.5 秒，不划算——播报省下的时间远比这多
        limit = int(max_chars * 1.6)
        for base, key, model in cands:
            out = await self._shrink_once(base, key, model, text,
                                          max_chars, per)
            if not out:
                continue
            if len(out) > limit:
                # 第一版还是太长：拿它自身再压一次，比原回答短就先用着
                log.info("首版 %d 字超过 %d，再压一次", len(out), limit)
                again = await self._shrink_once(base, key, model, out,
                                                max_chars, per)
                if again and len(again) < len(out):
                    out = again
            return self._with_question(text, out)
        return ""

    @staticmethod
    def _with_question(src, out):
        """压缩版丢了问句就补回来。

        模型经常把「要我现在切模型吗」当成次要细节删掉，用户就再也听不到
        需要他拍板的那一句了（2026-11-11 两次实测都这样）。问句在原文里
        有、压缩版里没有时，从原文末尾取最后一句问句接上去。
        """
        if not re.search(r"[？?]", src):
            return out
        if re.search(r"[？?]", out):
            return out
        parts = [p.strip() for p in re.split(r"(?<=[。！!？?])", src) if p.strip()]
        asks = [p for p in parts if re.search(r"[？?]", p)]
        if not asks:
            return out
        q = asks[-1]
        if len(q) > 50:                    # 问句太长就截到第一个逗号
            q = re.split(r"[，,]", q)[0] + "？"
        log.info("压缩丢了问句，补回：%s", q[:30])
        return out.rstrip("。") + "。" + q

    async def _shrink_once(self, base, key, model, text, max_chars, timeout):
        body = {
            "model": model or "gpt-4o-mini",
            "stream": False,
            "messages": [
                {"role": "system", "content": SHRINK_PROMPT % int(max_chars)},
                {"role": "user", "content": text},
            ],
        }
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        t0 = time.perf_counter()
        try:
            async with self.session.post(
                base + "/chat/completions", json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as r:
                if r.status != 200:
                    log.warning("口播压缩 %s 返回 %s，换下一个入口", model, r.status)
                    return ""
                data = await r.json()
            out = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        except asyncio.TimeoutError:
            log.warning("口播压缩 %s 超时 %.1fs", model, timeout)
            return ""
        except Exception as e:
            log.warning("口播压缩 %s 异常：%s", model, e)
            return ""
        out = re.sub(r"\s+", " ", out).strip().strip('"“”')
        if not out:
            return ""
        log.info("口播压缩 %.1fs（%s）：%s", time.perf_counter() - t0, model, out[:40])
        return out
