"""小米账号认证与会话

token 来源有三个，优先级从高到低：
  1. conf/.mi.token（WebUI 扫码登录 / 之前登录留下）
  2. conf/auth.json（长期根凭据：passToken/userId/deviceId，只增不删）
  3. 账号密码登录（备用；小米风控严格时容易失败）

只要 token 里已有 micoapi 的 serviceToken 就直接用，**不要每次启动都 login()**。

—— 掉线根因（2026-11-05 定位，对照 hanxi/xiaomusic 的 auth.json 方案）——
小米在 `serviceLogin` 给出 location 之后，是靠那次 **带 clientSign 的 GET 回调**
下发凭据的，并且会在这次响应的 Set-Cookie 里**轮换 passToken**。
miservice 的 `_securityTokenService` 只从里面取了 `serviceToken`，把新的 passToken
丢了；下一次续期还用旧的 passToken，服务端一律回 70016「登录验证失败」。
更糟的是 miservice 的 `login()` 一旦在 `self.token["passToken"] = resp["passToken"]`
抛 KeyError，就会 `self.token = None; save_token()` 把文件删掉，我们再用 .bak 把
**已经作废的旧 passToken** 还原回来 —— 从此每次续期都 70016，只能重新扫码。

所以这里做三件事，缺一不可：
  1. 登录/续期时把回调响应的 Set-Cookie 全收下，轮换后的 passToken 立刻落盘；
  2. 另存一份 auth.json 根凭据（xiaomusic 同款），miservice 碰不到它；
  3. 失败后不再用 .bak 无条件覆盖，避免把刚轮换的新凭据冲掉。
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import shutil
import time
from urllib.parse import quote

from aiohttp import ClientSession
from miservice import MiAccount, MiNAService

log = logging.getLogger("auth")

SID = "micoapi"

# micoapi 的 serviceToken 在小米服务端只有 24 小时有效期。过期后 miservice 只会
# 一直 401，不会自己拿 passToken 去换 —— 从前每天凌晨都要重新扫码就是这个原因。
REFRESH_EVERY = 12 * 3600     # 每 12 小时刷一次（24 小时过期，留一半余量）
REFRESH_FIRST = 600           # 启动后先等 10 分钟，避开启动登录
REFRESH_MIN_GAP = 1800        # 401 触发的补救刷新，最短间隔 30 分钟
PROBE_EVERY = 1800            # 每 30 分钟探一次活，别等 token 过期才发现掉线


class Auth:
    def __init__(self, cfg):
        self.cfg = cfg
        self.session = None
        self.account = None
        self.mina = None
        self.token_path = cfg.abspath(cfg.mi.get("token_store") or "./conf/.mi.token")
        # 登录熔断：失败多次后冷却，避免每轮轮询都去撞小米风控
        self._login_fail_count = 0
        self._login_block_until = 0.0
        self.last_auth_error = ""
        self._refresh_task = None
        self._refresh_block_until = 0.0
        self.last_refresh_ok = 0

    async def start(self):
        self.session = ClientSession()
        os.makedirs(os.path.dirname(self.token_path), exist_ok=True)
        self._new_account()

        if self.has_service_token:
            log.info("复用已有 serviceToken（跳过登录），userId=%s", self.user_id)
        else:
            log.info("没有可用 serviceToken，尝试登录一次")
            if not await self._try_login():
                # 不再抛异常退出：退出会导致容器反复重启、WebUI 也跟着没了，
                # 用户连"扫码登录"的入口都打不开。降级运行，让 WebUI 可用。
                log.error("小米账号未登录，语音链路不可用。"
                          "处理办法：打开 WebUI → 账号 → 扫码登录。")
            else:
                bak = self.token_path + ".bak"
                if os.path.isfile(self.token_path):
                    shutil.copy2(self.token_path, bak)

        self.mina = MiNAService(self.account)
        self._start_refresher()
        return self

    # ---------- serviceToken 自动续期 ----------
    def _start_refresher(self):
        if self._refresh_task and not self._refresh_task.done():
            return
        try:
            self._refresh_task = asyncio.create_task(self._refresh_loop())
        except RuntimeError:                   # 没有事件循环（脚本模式）就跳过
            self._refresh_task = None

    async def _refresh_loop(self):
        await asyncio.sleep(REFRESH_FIRST)
        last_refresh = time.time()
        while True:
            try:
                if not self.has_service_token:
                    await self.ensure_login()
                elif time.time() - last_refresh >= REFRESH_EVERY:
                    if await self._refresh_once("定期续期"):
                        last_refresh = time.time()
                else:
                    # 探活：xiaomusic 的 need_login() 也是先真的调一次接口，
                    # 没坏就不动 token（每轮都换等于每天多给自己几次翻车机会）
                    await self._probe_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("认证后台巡检异常：%s", e)
            await asyncio.sleep(PROBE_EVERY)

    async def _probe_once(self):
        """调一次 device_list：401 就立刻补齐 token，正常就什么都不做"""
        try:
            await self.mina.device_list()
        except Exception as e:
            msg = str(e)
            if "401" in msg or "403" in msg or "Unauthorized" in msg:
                log.warning("巡检发现 token 已失效（401），立即续期")
                await self._refresh_once("巡检 401")
            # 其它异常（网络抖动）不管

    async def _refresh_once(self, why=""):
        """用 passToken 静默换一个新的 serviceToken（不重启、不掉线）"""
        root = self._root_token()
        tok = self.token if isinstance(self.token, dict) else {}
        if not (tok.get("passToken") or root.get("passToken")):
            log.info("跳过续期（%s）：没有 passToken，需要扫码登录", why)
            return False
        before = self.service_token
        ok = await self._try_login()
        # _try_login 失败时可能已经用备份重建了 account，mina 必须一起换掉，
        # 否则后续请求还挂在那个 token 被清空的老对象上。
        try:
            self.mina = MiNAService(self.account)
        except Exception:
            pass
        if ok:
            self.last_refresh_ok = int(time.time())
            changed = self.service_token != before
            log.info("serviceToken 续期成功（%s），token %s，下次 %d 小时后",
                     why or "手动", "已更换" if changed else "未变",
                     REFRESH_EVERY // 3600)
        else:
            log.warning("serviceToken 续期失败（%s）：%s", why, self.last_auth_error)
        return ok

    def kick_refresh(self, why="请求被拒"):
        """401 时的补救：异步补一次续期，不阻塞当前请求"""
        if time.time() < self._refresh_block_until:
            return
        self._refresh_block_until = time.time() + REFRESH_MIN_GAP
        try:
            asyncio.create_task(self._refresh_once(why))
        except RuntimeError:
            pass

    def _new_account(self):
        # 先修文件、再构造 MiAccount：MiAccount 构造时就会读 token，
        # 顺序反了的话第一次判断 has_service_token 仍然为假，会白跑一次登录。
        self._normalize_token_file()
        self.account = MiAccount(
            self.session,
            self.cfg.mi.get("account", ""),
            self.cfg.mi.get("password", ""),
            self.token_path,
        )
        self._normalize_token()
        self._adopt_root()          # auth.json 里有、.mi.token 里没有的字段补齐
        self._wrap_login()
        self._patch_mi_request()

    # ---------- 对 miservice 的两处压制 ----------
    # miservice.miaccount 里有两段危险逻辑：
    #   mi_request: `if (token and sid in token) or await self.login(sid)`  → 每个请求都可能同步登录
    #                `if status == 401: self.token = None; 递归重试`        → 一次 401 把内存 token 清空
    #   login:       失败时 `self.token = None` + `save_token()`            → 把磁盘 token 也写空
    # 三者叠加 = 一次 401 之后，每个请求都触发一次必定失败的登录，永不恢复
    # （2026-10-28 19:14 起 6.5 小时刷了 3 万条，就是这条链）。这里逐个按住。

    def _wrap_login(self):
        """给 account.login 套一层：有 token 就不登，冷却期内直接返回 False"""
        if getattr(self.account, "_xb_login_wrapped", False):
            return
        real = self.account.login
        auth = self

        async def patched(sid, *a, **kw):
            # 已经有可用 token 时，miservice 不该再登（否则等于每请求一次登录）
            if auth.has_service_token:
                return True
            if time.time() < auth._login_block_until:
                return False                      # 冷却中：不发请求、不写文件、不打日志
            return await auth._try_login(sid)

        self.account.login = patched
        self.account._xb_login_wrapped = True
        self._real_login = real

    def _patch_mi_request(self):
        """401 之后 miservice 会把 token 置 None，这里补回来，避免每请求都重新登录"""
        if getattr(self.account, "_xb_req_wrapped", False):
            return
        real = self.account.mi_request
        auth = self

        async def patched(sid, url, data, headers, relogin=True):
            try:
                return await real(sid, url, data, headers, relogin)
            except Exception:
                if not auth.has_service_token:
                    # miservice 已把 token 清成 None：先用根凭据补回内存，
                    # 让后续请求走"带旧 token 的 401"而不是"每请求一次登录"
                    auth._restore_from_root()
                # token 还在但服务端说不行，多半是 24 小时到期：拿 passToken 换新的
                auth.kick_refresh("请求被拒")
                raise

        self.account.mi_request = patched
        self.account._xb_req_wrapped = True

    # ---------- 根凭据 ----------
    def _root_candidates(self):
        return [self.token_path + ".bak",
                os.path.join(os.path.dirname(self.token_path), "auth.json")]

    def _restore_from_root(self):
        """从备份 / auth.json 恢复内存 token（含 micoapi 键），返回是否恢复成功"""
        for path in self._root_candidates():
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(d, dict):
                continue
            if not d.get(SID):
                st, ss = d.get("serviceToken"), d.get("ssecurity")
                if isinstance(st, str) and st and isinstance(ss, str) and ss:
                    d[SID] = [ss, st]
                else:
                    continue
            self.account.token = d
            log.info("已从 %s 恢复内存 token", os.path.basename(path))
            return True
        return False

    def _normalize_token_file(self):
        """磁盘上的「扁平」token 就地补成 miservice 结构（幂等）"""
        try:
            with open(self.token_path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict) or raw.get(SID):
            return
        st, ss = raw.get("serviceToken"), raw.get("ssecurity")
        if not (isinstance(st, str) and st and isinstance(ss, str) and ss):
            return
        raw[SID] = [ss, st]
        try:
            tmp = self.token_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.token_path)
            log.info("已把扁平 token 规范化为 miservice 格式（文件）")
        except OSError as e:
            log.warning("规范化 token 失败：%s", e)

    def _normalize_token(self):
        """把「扁平」token 补成 miservice 要的结构。

        有些来源（XiaoMusic 导出、早期脚本）把 serviceToken/ssecurity 直接放在顶层，
        没有 `micoapi: [ssecurity, serviceToken]` 这个键。这种情况下 has_service_token
        永远为假，于是每轮轮询都去登录一次 —— 2026-10-29 就是这样刷了 3 万条报错。
        """
        t = self.account.token
        if not isinstance(t, dict) or t.get(SID):
            return
        st, ss = t.get("serviceToken"), t.get("ssecurity")
        if isinstance(st, str) and st and isinstance(ss, str) and ss:
            t[SID] = [ss, st]
            try:
                self.account.token_store.save_token(t)
                log.info("已把扁平 token 规范化为 miservice 格式（补 %s 键）", SID)
            except Exception as e:
                log.warning("规范化 token 落盘失败：%s", e)

    async def reload(self):
        """扫码登录写入新 token 后热生效，不用重启容器"""
        self._new_account()
        self.mina = MiNAService(self.account)
        log.info("认证已重载，userId=%s 有 serviceToken=%s",
                 self.user_id, self.has_service_token)
        return self.has_service_token

    async def login_password(self, account, password):
        """账号密码登录（备用通道）。成功后写回配置，便于下次自动续期。"""
        if not account or not password:
            return {"ok": False, "error": "账号和密码都要填"}
        self.cfg.set("mi", "account", value=account)
        self.cfg.set("mi", "password", value=password)
        self.cfg.save()

        self._new_account()
        # 强制走账号密码流程，不要拿旧 token 短路
        self.account.token = {"deviceId": "".join(
            random.choices("0123456789ABCDEF", k=16))}
        ok = await self.account.login(SID)
        if not ok:
            self.mina = MiNAService(self.account) if self.has_service_token else self.mina
            return {"ok": False,
                    "error": "登录失败：多为小米风控（滑块/短信验证）。建议改用扫码登录。"}
        self.mina = MiNAService(self.account)
        if os.path.isfile(self.token_path):
            shutil.copy2(self.token_path, self.token_path + ".bak")
        return {"ok": True, "user_id": self.user_id}

    # ---------- 信息 ----------
    @property
    def has_service_token(self):
        t = self.account.token if self.account else None
        return bool(t and isinstance(t.get(SID), (list, tuple)) and len(t[SID]) > 1
                    and t[SID][1])

    def status(self):
        """给 WebUI 用的登录态摘要（不暴露 token 本体）"""
        st = os.stat(self.token_path) if os.path.isfile(self.token_path) else None
        tok = self.token or {}
        return {
            "logged_in": self.has_service_token,
            "needs_login": not self.has_service_token,
            "auth_error": "" if self.has_service_token else self.last_auth_error,
            "login_retry_in": max(0, int(self._login_block_until - time.time())),
            "user_id": self.user_id,
            "account": self.cfg.mi.get("account", ""),
            "has_password": bool(self.cfg.mi.get("password")),
            "token_path": self.token_path,
            "token_updated": int(st.st_mtime) if st else 0,
            "device_id": self.cfg.device.get("device_id", ""),
            "sid": SID,
        }

    @property
    def token(self):
        return self.account.token if self.account else None

    @property
    def user_id(self):
        t = self.token or {}
        return str(t.get("userId", ""))

    @property
    def service_token(self):
        t = self.token or {}
        pair = t.get(SID)
        return pair[1] if isinstance(pair, (list, tuple)) and len(pair) > 1 else ""

    def conversation_cookies(self, device_id):
        return {
            "deviceId": device_id,
            "serviceToken": self.service_token,
            "userId": self.user_id,
        }

    async def ensure_login(self):
        """没有 serviceToken 才尝试登录，且失败后要冷却。

        原来的写法是「没有 token 就 login()」，但监听循环 0.6 秒一轮：
        小米一旦要求验证码（87001），就会变成每 0.6 秒撞一次、每次打一条 ERROR +
        traceback，而且 miservice 登录失败会把 token 置 None 并写空文件。
        2026-10-28 19:14 起刷了 3 万条、15MB 日志，凭据也被清掉。
        """
        if self.has_service_token:
            return True

        now = time.time()
        if now < self._login_block_until:
            return False                       # 冷却中，安静跳过

        ok = await self._try_login()
        return ok

    def _root_token(self):
        """auth.json 里的长期凭据（passToken/userId/deviceId/cUserId），只读，永不写回缓存"""
        path = os.path.join(os.path.dirname(self.token_path), "auth.json")
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(d, dict):
            return {}
        return {k: d[k] for k in
                ("passToken", "userId", "deviceId", "cUserId") if d.get(k)}

    async def _mi_login(self, sid=SID):
        """自己走一遍 serviceLogin，绕开 miservice 的 KeyError 和丢 cookie

        三个关键点：
        1. miservice 里 `self.token["passToken"] = resp["passToken"]`，小米在
           「passToken 无需轮换」时不返回该字段 → KeyError → 判成登录失败。
        2. miservice 的 `_securityTokenService` 只读 `r.cookies["serviceToken"]`，
           把同一次响应里 Set-Cookie 的**新 passToken / cUserId** 全丢了。
        3. 上面两步叠加：p派生的新 passToken 没落盘，下次还用旧的 → 70016。

        返回 (state, detail)，state ∈ {"ok", "need_password", "fail"}。
        """
        acc = self.account
        if not isinstance(acc.token, dict):
            acc.token = {}
        tok = acc.token
        tok.setdefault("deviceId", "".join(random.choices("0123456789ABCDEF", k=16)))

        # 登录前清一次 cookie jar：上一轮 401 残留的 serviceToken/cUserId
        # 会盖在回调响应上，xiaomusic 的 _patch_account 也是这么干的
        try:
            acc.session.cookie_jar.clear()
        except Exception:
            pass

        resp = await acc._serviceLogin(f"serviceLogin?sid={sid}&_json=true")
        code = resp.get("code")
        if code != 0:
            return "need_password", f"code={code} {resp.get('description', '')}".strip()
        if not (resp.get("location") and resp.get("nonce") and resp.get("ssecurity")):
            return "fail", "响应缺少 location/nonce/ssecurity"

        tok["userId"] = resp.get("userId") or tok.get("userId")
        if resp.get("passToken"):
            tok["passToken"] = resp["passToken"]      # 仅在服务端轮换时覆盖

        # 带 clientSign 取回调，并把这一次响应的 Set-Cookie 全部收下
        ssecurity = resp["ssecurity"]
        nsec = "nonce=" + str(resp["nonce"]) + "&" + ssecurity
        sign = base64.b64encode(hashlib.sha1(nsec.encode()).digest()).decode()
        location = resp["location"]
        url = location + ("&" if "?" in location else "?") + "clientSign=" + quote(sign)
        service_token = ""
        jar = {}
        try:
            async with acc.session.get(url) as r:
                for key, morsel in r.cookies.items():
                    jar[key] = morsel.value if hasattr(morsel, "value") else str(morsel)
                if jar.get("serviceToken"):
                    service_token = jar["serviceToken"]
                else:                                  # 少数情况下走 body
                    text = await r.text()
                    service_token = text.strip() if text.strip() and len(text) < 512 else ""
        except Exception as e:
            log.warning("取 serviceToken 回调异常：%s", e)
            service_token = ""
        # session jar 里也可能有值（follow redirect 时种下的）
        try:
            for c in acc.session.cookie_jar:
                jar.setdefault(c.key, c.value)
        except Exception:
            pass
        if not service_token:
            return "fail", "回调里没有 serviceToken"

        # ★ 关键：轮换后的 passToken / cUserId 在这里，立刻写进 token
        rotated = False
        for k in ("passToken", "cUserId", "userId", "yetAnotherServiceToken"):
            v = jar.get(k) or ""
            if not v:
                continue
            if k == "userId":
                tok["userId"] = v
            elif tok.get(k) != v:
                if k == "passToken":
                    rotated = True
                tok[k] = v
        if rotated:
            log.info("小米轮换了 passToken，已采用新值（这一步以前丢掉了，才导致 70016）")

        tok[sid] = (ssecurity, service_token)
        tok["saveTime"] = int(time.time() * 1000)
        acc.token = tok
        self._persist(tok)
        return "ok", ""

    # ---------- 落盘 ----------
    def _persist(self, tok):
        """原子写 .mi.token（miservice 认的那份）+ auth.json（根凭据，只增不删）"""
        try:
            dirname = os.path.dirname(self.token_path)
            tmp = self.token_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(tok, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.token_path)
            try:
                os.chmod(self.token_path, 0o600)
            except OSError:
                pass
        except OSError as e:
            log.warning("写 .mi.token 失败：%s", e)

        root = {k: tok.get(k) for k in
                ("passToken", "userId", "deviceId", "cUserId", "saveTime") if tok.get(k)}
        if not root.get("passToken"):
            return
        path = os.path.join(dirname, "auth.json")
        try:
            try:
                with open(path, encoding="utf-8") as f:
                    old = json.load(f)
                if isinstance(old, dict):
                    root = {**old, **root}
            except (OSError, ValueError):
                pass
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(root, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError as e:
            log.warning("写 auth.json 失败：%s", e)

    def _adopt_root(self, force=False):
        """把 auth.json 的长期凭据并进内存 token；force 时以文件为准

        返回是否真的换了 passToken（用于「服务端说 70016，换份凭据再试一次」）。
        """
        root = self._root_token()
        if not root:
            return False
        t = self.account.token if isinstance(self.account.token, dict) else {}
        changed = False
        for k, v in root.items():
            if force or not t.get(k):
                if k == "passToken" and t.get(k) and t.get(k) != v:
                    changed = True
                t[k] = v
        self.account.token = t
        return changed

    async def _try_login(self, sid=SID):
        """尝试登录一次

        参照 xiaomusic：登录前先用 auth.json 的 passToken 覆盖内存 token，
        这样 .mi.token 被写坏也不影响登录。
        注意：失败后**不再**用 .bak 无条件覆盖 —— 一次成功的手工续期之后
        服务端已经轮换过 passToken，把旧备份还原回来等于自己把凭据作废。
        """
        bak = self.token_path + ".bak"
        had = os.path.isfile(self.token_path)
        if had:
            try:
                shutil.copy2(self.token_path, bak)
            except OSError:
                pass

        self._adopt_root()

        state, why = "fail", ""
        try:
            state, why = await self._mi_login(sid)
        except Exception as e:                 # 网络异常等，兜一层
            state, why = "fail", f"{type(e).__name__}: {e}"

        if state == "need_password" and self._adopt_root(force=True):
            # 内存里的 passToken 可能比 auth.json 旧，用文件里的再试一次
            log.info("passToken 被拒，改用 auth.json 里的凭据重试")
            try:
                state, why = await self._mi_login(sid)
            except Exception as e:
                state, why = "fail", f"{type(e).__name__}: {e}"

        if state == "need_password":
            # 免密通道被拒（passToken 失效 / 需要验证），有账号密码才退到密码登录
            if self.cfg.mi.get("account") and self.cfg.mi.get("password"):
                try:
                    real = getattr(self, "_real_login", None) or self.account.login
                    ok_pwd = await real(sid)
                    state = "ok" if ok_pwd else "fail"
                    why = "" if ok_pwd else "账号密码登录失败"
                except Exception as e:
                    state, why = "fail", f"{type(e).__name__}: {e}"
            else:
                why = "passToken 已失效，且未配置账号密码，只能扫码登录"

        ok = state == "ok"
        if not ok and why:
            self.last_auth_error = why
            log.warning("免密登录未通过：%s", why)

        if ok:
            self._login_fail_count = 0
            self._login_block_until = 0.0
            self.last_auth_error = ""
            log.info("小米登录成功 userId=%s", self.user_id)
            return True

        self._login_fail_count += 1
        # 只有「miservice 把内存 token 清空了」才需要兜底还原；
        # 正常失败（70016）时内存 token 完好，绝不能拿旧备份去覆盖。
        if not isinstance(self.account.token, dict) or not self.account.token:
            if had and os.path.isfile(bak):
                try:
                    shutil.copy2(bak, self.token_path)
                    self._new_account()
                    log.info("内存 token 被清空，已从 .bak 还原")
                except OSError:
                    pass

        if self._login_fail_count >= 3:
            self._login_block_until = time.time() + 600
            self.last_auth_error = f"登录连续失败，已暂停 10 分钟（{why or '原因未知'}）"
            log.error("小米登录连续失败 %d 次，暂停 10 分钟：%s。请在 WebUI 重新扫码登录。",
                      self._login_fail_count, why or "原因未知")
        else:
            self._login_block_until = time.time() + 60
            self.last_auth_error = why or "登录失败"
            log.warning("小米登录失败（第 %d 次）：%s。60 秒内不再重试。",
                        self._login_fail_count, why or "原因未知")
        return False

    async def device_list(self):
        return await self.mina.device_list()

    async def close(self):
        if self.session:
            await self.session.close()
