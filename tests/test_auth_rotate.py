"""桩测试：续期时能否收下小米轮换的 passToken（不联网）

模拟两次续期：
  1) serviceLogin 返回 code=0 + location，回调响应里 Set-Cookie 带了新 passToken
     → 必须采用新值并同时落盘 .mi.token 与 auth.json
  2) serviceLogin 返回 70016 → 判 need_password，且**不能**用 .bak 里的旧
     passToken 覆盖刚轮换的新值
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, "/app/bridge")
from auth import Auth                              # noqa: E402


class FakeCfg:
    mi = {"token_store": "./.mi.token"}

    def __init__(self, d):
        self.d = d

    def abspath(self, p):
        return os.path.join(self.d, os.path.basename(p))


class FakeResp:
    def __init__(self, cookies):
        self.cookies = cookies
        self._text = "ok"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return self._text


class FakeSession:
    def __init__(self, cookies):
        self.cookie_jar = []
        self._cookies = cookies
        self.closed = False

    def get(self, url, *a, **kw):
        return FakeResp(dict(self._cookies))

    async def close(self):
        self.closed = True


class FakeAccount:
    def __init__(self, token, resp, cookies):
        self.token = token
        self._resp = resp
        self.session = FakeSession(cookies)

    async def _serviceLogin(self, uri, data=None):
        return self._resp


def build(tmp, token, resp, cookies):
    a = Auth(FakeCfg(tmp))
    a.account = FakeAccount(token, resp, cookies)
    return a


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {extra}" if extra and not cond else ""))
    return cond


async def main():
    tmp = tempfile.mkdtemp()
    ok = True

    # --- 1. 轮换：新 passToken 必须被收下 ---
    token = {"deviceId": "wb_test", "userId": 319, "passToken": "PT_OLD",
             "micoapi": ["ss_old", "ST_OLD"]}
    with open(os.path.join(tmp, ".mi.token"), "w") as f:
        json.dump(token, f)
    resp = {"code": 0, "location": "https://sts.example/x?a=1", "nonce": "n1",
            "ssecurity": "ss_new", "userId": 319}
    cookies = {"serviceToken": "ST_NEW", "passToken": "PT_NEW", "cUserId": "CU_NEW"}
    a = build(tmp, dict(token), resp, cookies)
    state, why = await a._mi_login("micoapi")
    ok &= check("续期状态 ok", state == "ok", why)
    ok &= check("采用轮换后的 passToken", a.account.token.get("passToken") == "PT_NEW",
                repr(a.account.token.get("passToken")))
    ok &= check("cUserId 一并更新", a.account.token.get("cUserId") == "CU_NEW")
    ok &= check("serviceToken 落进 micoapi", a.account.token.get("micoapi") == ("ss_new", "ST_NEW"))

    disk = json.load(open(os.path.join(tmp, ".mi.token")))
    ok &= check(".mi.token 已落盘新 passToken", disk.get("passToken") == "PT_NEW", repr(disk.get("passToken")))
    root = json.load(open(os.path.join(tmp, "auth.json")))
    ok &= check("auth.json 已落盘新 passToken", root.get("passToken") == "PT_NEW", repr(root.get("passToken")))
    ok &= check("auth.json 带 cUserId", root.get("cUserId") == "CU_NEW")

    # --- 2. 70016：不得用旧备份覆盖刚轮换的凭据 ---
    with open(os.path.join(tmp, ".mi.token.bak"), "w") as f:
        json.dump({"passToken": "PT_OLD", "userId": 319, "deviceId": "wb_test",
                   "micoapi": ["ss_old", "ST_OLD"]}, f)
    a2 = build(tmp, {"deviceId": "wb_test", "userId": 319, "passToken": "PT_NEW",
                     "micoapi": ["ss_new", "ST_NEW"]},
               {"code": 70016, "description": "登录验证失败"}, {})
    st2, why2 = await a2._mi_login("micoapi")
    ok &= check("70016 判为 need_password", st2 == "need_password", why2)
    ok &= check("失败后内存 token 未被清空", a2.account.token.get("passToken") == "PT_NEW")
    disk2 = json.load(open(os.path.join(tmp, ".mi.token")))
    ok &= check("失败后磁盘 token 仍是新值", disk2.get("passToken") == "PT_NEW",
                repr(disk2.get("passToken")))

    # --- 3. _try_login 失败路径也不能把 .bak 还原回去 ---
    a3 = build(tmp, {"deviceId": "wb_test", "userId": 319, "passToken": "PT_NEW",
                     "micoapi": ["ss_new", "ST_NEW"]},
               {"code": 70016, "description": "登录验证失败"}, {})
    r = await a3._try_login("micoapi")
    disk3 = json.load(open(os.path.join(tmp, ".mi.token")))
    ok &= check("_try_login 返回 False", r is False)
    ok &= check("_try_login 后磁盘仍是新 passToken", disk3.get("passToken") == "PT_NEW",
                repr(disk3.get("passToken")))

    print("\nRESULT:", "ALL PASS" if ok else "HAS FAILURE")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
