"""监听小爱对话记录

轮询 userprofile.mina.mi.com 的 conversation 接口，拿到最新一条对话。
注意：记录里已带 answers[0].tts.text，说明它是在小爱把回答生成完之后才写入的，
所以拿到 query 时小爱已经在开口了 —— 这是残句的根因，无法通过优化本模块消除。
"""
import asyncio
import json
import logging
import time

log = logging.getLogger("listen")

LATEST_ASK_API = (
    "https://userprofile.mina.mi.com/device_profile/v2/conversation"
    "?source=dialogu&hardware={hardware}&timestamp={ts}&limit=2"
)


class AuthError(Exception):
    """小米登录态失效（400/401/403）。要退避，不能按普通异常处理。"""


class ConversationListener:
    def __init__(self, auth, cfg):
        self.auth = auth
        self.cfg = cfg
        self.device_id = cfg.device.get("device_id", "")
        self.hardware = cfg.device.get("hardware", "")
        self.poll_sec = float(cfg.listen.get("poll_sec", 0.5))
        self.last_ts = int(time.time() * 1000)
        self._running = False
        self.auth_failed = False       # 给 WebUI 显示"需要重新扫码"
        self._fail = 0                 # 连续失败次数

    def _url(self):
        return LATEST_ASK_API.format(
            hardware=self.hardware, ts=int(time.time() * 1000)
        )

    async def _fetch_once(self):
        """返回最新记录 dict 或 None；鉴权失效抛 AuthError 让主循环退避"""
        await self.auth.ensure_login()
        cookies = self.auth.conversation_cookies(self.device_id)
        async with self.auth.session.get(
            self._url(), cookies=cookies, timeout=10
        ) as r:
            if r.status in (401, 403):
                log.warning("401/403，尝试重新登录")
                await self.auth.ensure_login()
                raise AuthError(f"HTTP {r.status}")
            if r.status != 200:
                if r.status in (400, 401, 403):
                    # 400 是 token 失效时小米的真实返回，跟 401 一样处理
                    raise AuthError(f"HTTP {r.status}")
                log.warning("对话接口返回 %s", r.status)
                return None
            payload = await r.json(content_type=None)

        raw = payload.get("data")
        if not raw:
            return None
        try:
            records = json.loads(raw).get("records") or []
        except (TypeError, ValueError):
            log.warning("记录解析失败: %r", str(raw)[:200])
            return None
        if not records:
            return None

        rec = records[0]
        answer = ""
        answers = rec.get("answers") or []
        if answers:
            answer = ((answers[0] or {}).get("tts") or {}).get("text", "").strip()
        return {
            "time": rec.get("time") or 0,
            "query": (rec.get("query") or "").strip(),
            "answer": answer,
        }

    async def run(self, on_command):
        """主循环：发现新记录就回调 on_command(record)"""
        self._running = True
        log.info(
            "开始监听 device=%s hardware=%s 间隔=%.2fs",
            self.device_id,
            self.hardware,
            self.poll_sec,
        )
        while self._running:
            delay = self.poll_sec
            try:
                rec = await self._fetch_once()
                if rec and rec["time"] > self.last_ts:
                    self.last_ts = rec["time"]
                    log.info("新指令 [%s] %s", rec["time"], rec["query"])
                    # 不 await：让回调并发跑，避免阻塞下一次轮询
                    asyncio.create_task(self._safe_handle(on_command, rec))
                if self._fail:
                    log.info("轮询恢复（此前连续失败 %d 次）", self._fail)
                    self._fail = 0
                    self.auth_failed = False
            except asyncio.CancelledError:
                self._running = False
                raise
            except AuthError as e:
                # 登录态失效：指数退避，别每 0.6 秒撞一次小米
                self._fail += 1
                self.auth_failed = True
                delay = min(60.0, max(2.0, self.poll_sec * (2 ** min(self._fail, 6))))
                if self._fail == 1:
                    log.warning("对话接口鉴权失效（%s），开始退避：轮询间隔 %.0fs。"
                                "需要在 WebUI 重新扫码登录。", e, delay)
                elif self._fail in (5, 20):
                    log.warning("鉴权仍未恢复（连续 %d 次），当前间隔 %.0fs", self._fail, delay)
            except Exception as e:
                self._fail += 1
                if self._fail <= 3:
                    log.warning("轮询异常: %s", e)
                delay = min(30.0, max(self.poll_sec, self.poll_sec * (2 ** min(self._fail, 5))))
            await asyncio.sleep(delay)

    @staticmethod
    async def _safe_handle(cb, rec):
        try:
            await cb(rec)
        except Exception:
            log.exception("处理指令失败")

    def stop(self):
        self._running = False
