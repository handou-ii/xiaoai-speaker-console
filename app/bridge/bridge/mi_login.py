"""小米账号登录（passport 二维码 / 账号密码）

为什么要有这个：bridge 以前只会复用 XiaoMusic 留下的 token，一旦过期只能去
NAS 上跑脚本。现在把登录搬进 WebUI，目的只有一个——**换设备/换账号后，
不登 NAS 也能自己把播放链路救回来**。

二维码走的是 passport 的 longPolling 流程，sid=micoapi（与小爱音箱同一套），
拿到的 serviceToken 正是 miservice 需要的那个。token 落盘格式与 miservice 一致：

    {"deviceId": ..., "userId": ..., "passToken": ..., "micoapi": [ssecurity, serviceToken]}
"""
import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import random
import re
import time
from urllib.parse import parse_qs, quote, urlencode, urlparse

import aiohttp

log = logging.getLogger("milogin")

PASSPORT = "https://account.xiaomi.com"
SID = "micoapi"
_TIMEOUT = 60


def _ua():
    """每次登录换一个机型串，减少被 passport 判定为同一客户端的概率"""
    h = lambda n: "".join(random.choices("0123456789ABCDEF", k=n))
    part = lambda n: "".join(random.choices(
        "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-", k=n))
    return (
        "Android-15-11.0.701-Xiaomi-23046RP50C-OS2.0.212.0.VMYCNXM-"
        f"{h(40)}-CN-{h(32)}-{h(32)}-SmartHome-MI_APP_STORE-"
        f"{h(40)}|{h(40)}|{part(16)}-64"
    )


def _strip(resp_text):
    """passport 的 JSON 前面挂了个 &&&START&&&"""
    return resp_text.replace("&&&START&&&", "").strip()


def qr_svg(url, box_size=8):
    """把登录链接渲染成内联 SVG（不依赖 Pillow，也不用前端引库）"""
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage,
                      box_size=box_size, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode("utf-8")
    # 去掉固定宽高，交给 CSS 控制
    svg = svg.replace("width=", "data-w=").replace("height=", "data-h=")
    return svg.replace("<svg ", '<svg class="qr-svg" ', 1)


class MiLogin:
    """一次登录会话。WebUI 先 start() 拿二维码，再 poll() 等扫码结果。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.state = "idle"           # idle | waiting | ok | error
        self.message = ""
        self.login_url = ""
        self.qr_url = ""
        self.started_at = 0.0
        self.deadline = 0.0
        self.user_id = ""
        self._task = None
        self._session = None

    # ---------- 状态 ----------
    def status(self):
        now = time.time()
        return {
            "state": self.state,
            "message": self.message,
            "login_url": self.login_url if self.state == "waiting" else "",
            "expires_in": max(0, int(self.deadline - now)) if self.deadline else 0,
            "user_id": self.user_id,
        }

    # ---------- 启动 ----------
    async def start(self):
        if self.state == "waiting" and time.time() < self.deadline:
            # 已有未过期的二维码，直接复用，别让用户反复刷新
            return {"ok": True, **self.status(), "qr_svg": qr_svg(self.login_url)}

        await self._cancel()
        self.state = "waiting"
        self.message = "等待扫码"
        self.user_id = ""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT))
        try:
            data = await self._bootstrap()
        except Exception as e:
            await self._cancel()
            self.state = "error"
            self.message = f"获取二维码失败：{e}"
            return {"ok": False, "error": self.message, **self.status()}

        self.login_url = data["loginUrl"]
        self.qr_url = data.get("qr", "")
        self.started_at = time.time()
        # passport 一般给 300 秒
        self.deadline = self.started_at + float(data.get("timeout") or 240)
        self._task = asyncio.create_task(self._wait(data["lp"]))
        log.info("已生成小米登录二维码，有效期 %.0fs", self.deadline - self.started_at)
        return {"ok": True, **self.status(),
                "qr_svg": qr_svg(self.login_url),
                "qr_url": self.qr_url,
                "tip": data.get("qrTips", "用米家 App 或小米手机扫码")}

    async def _bootstrap(self):
        """serviceLogin → 取 location → longPolling/loginUrl → 二维码链接"""
        headers = {"User-Agent": _ua()}
        async with self._session.get(
                f"{PASSPORT}/pass/serviceLogin?sid={SID}&_json=true",
                headers=headers) as r:
            data = json.loads(_strip(await r.text()))
        loc = data.get("location")
        if not loc:
            raise RuntimeError(data.get("description") or "未返回登录地址")

        params = {k: v[0] for k, v in parse_qs(urlparse(loc).query).items()}
        params.update({
            "theme": "", "bizDeviceType": "",
            "_hasLogo": "false", "_qrsize": "240",
            "_dc": str(int(time.time() * 1000)),
        })
        url = f"{PASSPORT}/longPolling/loginUrl?" + urlencode(params)
        async with self._session.get(url, headers=headers) as r:
            return json.loads(_strip(await r.text()))

    async def _wait(self, lp_url):
        """长轮询等扫码；成功就把 token 落盘"""
        headers = {"User-Agent": _ua()}
        try:
            timeout = aiohttp.ClientTimeout(total=max(60, self.deadline - time.time() + 30))
            async with self._session.get(lp_url, headers=headers,
                                         timeout=timeout) as r:
                data = json.loads(_strip(await r.text()))
            if data.get("code") != 0:
                raise RuntimeError(data.get("description") or "扫码未完成")
            log.info("扫码通过，长轮询返回字段：%s", ",".join(sorted(data.keys())))

            # 正确姿势是「给 location 带上 clientSign 再取」：小米只认签名请求，
            # 裸 GET 只会拿到一个页面、不种 serviceToken —— 这正是
            # 2026-11-03 03:11 那次"换 token 失败：成功"的根因。
            loc = data.get("location") or ""
            nonce, ssecurity = data.get("nonce"), data.get("ssecurity")
            service_token = ""
            if loc and nonce and ssecurity:
                ssecurity, service_token = await self._token_from_location(
                    loc, nonce, ssecurity, headers)
            if not service_token and loc:
                async with self._session.get(loc, headers=headers) as r:
                    await r.read()
            cookies = {c.key: c.value for c in self._session.cookie_jar}
            service_token = (service_token or cookies.get("serviceToken")
                             or data.get("serviceToken") or "")
            if not service_token:
                # 兜底：拿刚扫出来的 passToken 让 miservice 再换一次
                log.warning("签名回调没换到 serviceToken，改用 passToken 换取（cookie: %s）",
                            ",".join(cookies.keys()) or "无")
                ssecurity, service_token = await self._mint_service_token(cookies)
            token = self._build_token(data, cookies, ssecurity, service_token)
            self._save_token(token)
            self.user_id = token.get("userId", "")
            self.state = "ok"
            self.message = "登录成功"
            log.info("小米账号登录成功 userId=%s", self.user_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self.state == "waiting":
                self.state = "error"
                self.message = f"登录失败：{e}"
            log.warning("扫码登录结束：%s", e)
        finally:
            await self._close_session()

    async def _token_from_location(self, loc, nonce, ssecurity, headers):
        """给回调地址带上 clientSign 再取，serviceToken 才会下发

        小米 passport 的规矩：location 必须附 clientSign（sha1(nonce&ssecurity)
        的 base64），否则只返回一个页面、不种 cookie。
        """
        nsec = f"nonce={nonce}&{ssecurity}"
        sign = base64.b64encode(hashlib.sha1(nsec.encode()).digest()).decode()
        url = loc + ("&" if "?" in loc else "?") + "clientSign=" + quote(sign)
        async with self._session.get(url, headers=headers) as r:
            st = r.cookies.get("serviceToken")
            st = st.value if hasattr(st, "value") else (st or "")
            if not st:
                text = await r.text()
                m = re.search(r'"serviceToken"\s*:\s*"([^"]+)"', text or "")
                st = m.group(1) if m else ""
            else:
                await r.read()
        if st:
            log.info("已用 clientSign 换到 serviceToken")
        return ssecurity, st

    async def _mint_service_token(self, cookies):
        """用扫码拿到的 passToken 换 serviceToken

        以前这里是自己拼 serviceLogin + clientSign，结果小米返回
        {"code":0,"description":"成功"} 但没带 location/nonce/ssecurity，
        报成「换取 serviceToken 失败：成功」（2026-11-03 03:11 那次）。
        现在直接借 miservice 的 MiAccount.login()——nonce/ssecurity/clientSign
        这套签名以官方库为准，它和后面调 MiNAService 用的是同一套代码。
        """
        pass_token = (cookies.get("passToken") or "").strip()
        if not pass_token:
            raise RuntimeError("回调里没有 passToken，无法换取 serviceToken")
        try:
            from miservice import MiAccount
        except ImportError:                       # 极端情况退回老流程
            log.warning("没装 miservice，退回手工 serviceLogin 流程")
            return await self._mint_by_hand({})

        device_id = ((cookies.get("deviceId") or "").strip().upper()
                     or "".join(random.choices("0123456789ABCDEF", k=16)))
        acc = MiAccount(self._session, "", "", None)
        acc.token = {
            "deviceId": device_id,
            "userId": cookies.get("userId") or "",
            "passToken": pass_token,
        }
        ok = await acc.login(SID)
        if not ok:
            raise RuntimeError("passToken 换 serviceToken 失败：小米拒绝（日志里有原始返回）")
        pair = acc.token.get(SID) or ("", "")
        ssecurity = pair[0] if isinstance(pair, (list, tuple)) and pair else ""
        service_token = pair[1] if isinstance(pair, (list, tuple)) and len(pair) > 1 else ""
        if not service_token:
            raise RuntimeError("换到的 serviceToken 为空")
        log.info("passToken 换取 serviceToken 成功（miservice）")
        return ssecurity, service_token

    async def _mint_by_hand(self, headers):
        """老的手工换取流程，仅在 miservice 不可用时兜底"""
        async with self._session.get(
                f"{PASSPORT}/pass/serviceLogin?sid={SID}&_json=true",
                headers=headers) as r:
            raw = _strip(await r.text())
        try:
            data = json.loads(raw)
        except ValueError:
            raise RuntimeError(f"serviceLogin 返回异常：{raw[:120]}")
        loc = data.get("location")
        nonce, ss = data.get("nonce"), data.get("ssecurity")
        if not (loc and nonce and ss):
            log.warning("serviceLogin 未给齐字段：%s", raw[:300])
            raise RuntimeError(
                f"换取 serviceToken 失败：{data.get('description') or data.get('code')}")
        nsec = f"nonce={nonce}&{ss}"
        sign = base64.b64encode(hashlib.sha1(nsec.encode()).digest()).decode()
        async with self._session.get(loc + "&clientSign=" + quote(sign),
                                     headers=headers) as r:
            st = (await r.text()).strip()
        if not st:
            raise RuntimeError("serviceToken 为空")
        return ss, st

    def _build_token(self, data, cookies, ssecurity="", service_token=""):
        service_token = service_token or cookies.get("serviceToken") \
            or data.get("serviceToken") or ""
        if not service_token:
            raise RuntimeError("回调里没有 serviceToken")
        token = {
            "deviceId": cookies.get("deviceId")
            or "".join(random.choices("0123456789ABCDEF", k=16)),
            "userId": str(data.get("userId") or cookies.get("userId") or ""),
            "passToken": data.get("passToken") or cookies.get("passToken") or "",
            # miservice 约定：token[sid] = (ssecurity, serviceToken)
            SID: [ssecurity or data.get("ssecurity", ""), service_token],
            "cUserId": cookies.get("cUserId", ""),
            "saveTime": int(time.time() * 1000),
        }
        return token

    def _save_token(self, token):
        path = self.cfg.abspath(self.cfg.mi.get("token_store") or "./conf/.mi.token")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(token, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        # 同步一份根凭据（auth.json）：miservice 只会动 .mi.token，
        # 这份文件它碰不到，续期时拿它兜底
        root_path = os.path.join(os.path.dirname(path), "auth.json")
        root = {k: token.get(k) for k in
                ("passToken", "userId", "deviceId", "cUserId", "saveTime")
                if token.get(k)}
        if root.get("passToken"):
            try:
                tmp = root_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(root, f, ensure_ascii=False, indent=2)
                os.replace(tmp, root_path)
                try:
                    os.chmod(root_path, 0o600)
                except OSError:
                    pass
            except OSError as e:
                log.warning("写 auth.json 失败：%s", e)
        log.info("token 已写入 %s", path)

    async def _close_session(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _cancel(self):
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        await self._close_session()
