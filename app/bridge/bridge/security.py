"""访问保护：与控制台（hub）共用同一个口令 token

分工：
  * 口令的哈希与 token 都由 hub 生成，hub 把 token 落到本容器的
    /app/conf/access.token，桥接只读这一个文件，不重复做口令校验；
  * 浏览器在 8900 登录后拿到的 cookie 名为 xc_token，cookie 不区分端口，
    因此直接打开 8790 也认同一个登录态；
  * 若先打开的是 8790，桥接上的登录框会把口令转给 hub 校验，
    成功后由桥接自己下发同样的 cookie。
文件不存在或为空 → 视为未启用保护，行为与旧版本一致。
"""
import hmac
import logging
import os

import aiohttp
from aiohttp import web

log = logging.getLogger("security")

TOKEN_FILE = os.environ.get("XC_TOKEN_FILE", "/app/conf/access.token")
HUB_BASE = os.environ.get("XC_HUB") or "http://127.0.0.1:8900"
COOKIE_NAME = "xc_token"

# 音箱自己来拉音频，带不了 cookie；health 给探活用
OPEN_PREFIXES = ("/health", "/media/", "/m/", "/audio/", "/api/auth/login")

_cache = {"mtime": -1.0, "token": ""}


def token():
    """读共享 token；文件没变就不重复读"""
    try:
        st = os.stat(TOKEN_FILE)
    except OSError:
        _cache.update({"mtime": -1.0, "token": ""})
        return ""
    if st.st_mtime != _cache["mtime"]:
        try:
            with open(TOKEN_FILE, encoding="utf-8") as f:
                _cache["token"] = f.read().strip()
        except OSError:
            _cache["token"] = ""
        _cache["mtime"] = st.st_mtime
    return _cache["token"]


def enabled():
    return bool(token())


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>小爱桥接 · 需要口令</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
 background:#14120f;color:#e9e2d6;
 font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.box{width:min(360px,92vw);padding:28px 26px 22px;border:1px solid #2e2a24;border-radius:14px;
 background:#1b1814;box-shadow:0 18px 50px rgba(0,0,0,.45)}
h1{margin:0 0 6px;font-size:17px;font-weight:600}
p{margin:0 0 18px;color:#8f867a;font-size:13px}
input{width:100%;padding:10px 12px;border-radius:9px;border:1px solid #38322a;
 background:#12100d;color:#e9e2d6;font-size:15px;outline:none}
input:focus{border-color:#c8892f}
button{margin-top:14px;width:100%;padding:10px;border:0;border-radius:9px;cursor:pointer;
 background:#c8892f;color:#1a1409;font-size:14px;font-weight:600}
button:disabled{opacity:.6;cursor:default}
.err{margin-top:10px;color:#e0705c;font-size:13px;min-height:18px}
.foot{margin-top:16px;color:#6b6459;font-size:12px;text-align:center}
a{color:#c8892f}
</style></head><body>
<div class="box">
  <h1>小爱桥接 · 8790</h1>
  <p>该设备已开启访问保护，请输入控制台里设置的口令。</p>
  <form id="f"><input id="pw" type="password" autocomplete="current-password" autofocus placeholder="访问口令">
  <button id="b" type="submit">进入</button></form>
  <div class="err" id="e"></div>
  <div class="foot">已在控制台登录过的话，回到 <a href="http://127.0.0.1:8900/">8900</a> 即可</div>
</div>
<script>
var f=document.getElementById('f'),pw=document.getElementById('pw');
var b=document.getElementById('b'),e=document.getElementById('e');
f.onsubmit=function(ev){
  ev.preventDefault();
  b.disabled=true;e.textContent='';
  fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({password:pw.value})})
   .then(function(r){return r.json()})
   .then(function(d){
     if(d&&d.ok){location.reload();return}
     e.textContent=(d&&d.error)||'登录失败';b.disabled=false;
   })
   .catch(function(){e.textContent='网络错误';b.disabled=false});
};
</script>
</body></html>
"""


def _cookie_token(request):
    try:
        return request.cookies.get(COOKIE_NAME) or ""
    except Exception:
        return ""


@web.middleware
async def guard(request, handler):
    if not enabled() or request.path.startswith(OPEN_PREFIXES):
        return await handler(request)
    got = (_cookie_token(request) or request.headers.get("X-XC-Token")
           or request.query.get("token") or "").strip()
    if got and hmac.compare_digest(got, token()):
        return await handler(request)
    if request.method == "GET" and request.path in ("/", "/index.html"):
        return web.Response(text=LOGIN_PAGE, content_type="text/html", status=401)
    return web.json_response({"ok": False, "error": "需要口令", "auth": True}, status=401)


async def api_login(request):
    """把口令转给 hub 校验，成功则由桥接自己下发同一枚 cookie"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    pw = str((body or {}).get("password") or "")
    try:
        timeout = aiohttp.ClientTimeout(total=6)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(HUB_BASE + "/api/auth/login",
                              json={"password": pw}) as r:
                data = await r.json(content_type=None) or {}
                status = r.status
    except Exception as e:
        log.warning("转发登录到 hub 失败：%s", e)
        return web.json_response(
            {"ok": False, "error": "控制台（8900）没响应，请先在控制台登录"}, status=502)
    tok = str(data.get("token") or "")
    if status == 200 and data.get("ok") and tok:
        resp = web.json_response({"ok": True})
        resp.set_cookie(COOKIE_NAME, tok, max_age=31536000, path="/",
                        httponly=True, samesite="Lax")
        return resp
    return web.json_response({"ok": False,
                              "error": data.get("error") or "口令不对"},
                             status=401)
