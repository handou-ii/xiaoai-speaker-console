#!/usr/bin/env python3
"""小爱音响控制台 · Hub 后端

只用标准库，不依赖任何 pip 包，避免在 NAS 上装环境。

职责：
  1. 提供静态页面（app/ui）
  2. 反向代理到本机各应用端口，前端因此不用处理跨域
  3. 探测应用是否在线
"""
import hashlib
import hmac
import json
import os
import posixpath
import secrets
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, urlsplit, unquote

VERSION = "1.5.5"

PORT = int(os.environ.get("HUB_PORT") or 8900)
# 桌面 iframe 走平台网关时挂在 /app/xiaoai-console/ 前缀下；直连端口则挂在 /
GATEWAY_PREFIX = "/app/xiaoai-console"
# 网关套接字：应用根目录/xiaoai-console.sock（ui/config 的 gatewaySocket 指向它）
SOCK = os.environ.get("HUB_SOCK") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "xiaoai-console.sock"))
UI_DIR = os.path.abspath(os.environ.get("HUB_UI")
                         or os.path.join(os.path.dirname(__file__), "..", "ui"))
DATA_DIR = os.path.abspath(os.environ.get("HUB_DATA")
                           or os.path.join(os.path.dirname(__file__), "..", "..", "var"))
ETC_DIR = os.path.abspath(os.environ.get("HUB_ETC")
                          or os.path.join(os.path.dirname(__file__), "..", "..", "etc"))

# 小爱音箱桥接服务
BRIDGE_DEFAULT = os.environ.get("HUB_BRIDGE") or "http://127.0.0.1:8790"

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def log(*a):
    print("[hub]", *a, file=sys.stderr, flush=True)


# ---------------- 更新 ----------------
APPNAME = "xiaoai-console"
UPDATE_REPO = os.environ.get("HUB_UPDATE_REPO") or "handou-ii/xiaoai-speaker-console"
# 只认这些目录里的安装包，避免任意路径被执行。
# 默认只有应用自己的 update 目录；想额外监看自己放安装包的地方，
# 在 ${ETC_DIR}/update.dirs 里一行写一个路径（# 开头为注释），
# 或用环境变量 HUB_UPDATE_DIR 指定（多个用冒号分隔）。这里不写死任何具体路径。
def update_dirs():
    dirs = [os.path.join(ETC_DIR, "update")]
    raw = os.environ.get("HUB_UPDATE_DIR") or ""
    try:
        with open(os.path.join(ETC_DIR, "update.dirs"), encoding="utf-8") as f:
            raw += "\n" + f.read()
    except OSError:
        pass
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line not in dirs:
            dirs.append(line)
    return dirs
# 状态与日志放 /tmp：升级要卸载应用，@appconf 下的文件在卸载/重装过程中不可靠
UPDATE_WORK = os.environ.get("HUB_UPDATE_WORK") or "/tmp/xc-update"
UPDATE_STATE = os.path.join(UPDATE_WORK, "state.json")
UPDATE_LOG = os.path.join(UPDATE_WORK, "install.log")


def ver_key(v):
    """"1.5.1" -> (1, 5, 1)；非数字段按 0 处理"""
    parts = []
    for seg in str(v or "").strip().split("."):
        digits = "".join(ch for ch in seg if ch.isdigit())
        parts.append(int(digits or 0))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def fpk_info(path):
    """读 fpk 外层 manifest，取 appname / version，并带上文件大小与时间"""
    info = {"path": path, "name": os.path.basename(path),
            "size": os.path.getsize(path), "mtime": int(os.path.getmtime(path)),
            "appname": "", "version": "", "valid": False}
    try:
        with tarfile.open(path, "r:gz") as tf:
            f = tf.extractfile("manifest")
            raw = f.read().decode("utf-8", "replace") if f else ""
        meta = {}
        for line in raw.splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                meta[k.strip()] = v.strip()
        info["appname"] = meta.get("appname", "")
        info["version"] = meta.get("version", "")
        info["valid"] = (info["appname"] == APPNAME and bool(info["version"]))
    except Exception as e:
        info["error"] = type(e).__name__
    return info


def scan_local():
    seen, out = set(), []
    for d in update_dirs():
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if not n.lower().endswith(".fpk"):
                continue
            p = os.path.join(d, n)
            rp = os.path.realpath(p)
            if rp in seen or not os.path.isfile(rp):
                continue
            seen.add(rp)
            out.append(fpk_info(rp))
    out.sort(key=lambda x: (ver_key(x.get("version")), x.get("mtime") or 0), reverse=True)
    return out


def gh_latest(timeout=8):
    """查远端最新版本；私有仓库未授权时返回 404，按不可用处理"""
    url = "https://api.github.com/repos/%s/releases/latest" % UPDATE_REPO
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "xiaoai-console/" + VERSION,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            return {"ok": False, "error": "远端未开放（%d）" % e.code}
        return {"ok": False, "error": "远端返回 %d" % e.code}
    except Exception as e:
        return {"ok": False, "error": "%s" % type(e).__name__}
    tag = (d.get("tag_name") or "").lstrip("v")
    assets = [a for a in (d.get("assets") or [])
              if str(a.get("name") or "").lower().endswith(".fpk")]
    a0 = assets[0] if assets else {}
    return {"ok": True, "latest": tag, "name": d.get("name") or "",
            "url": a0.get("browser_download_url") or "", "size": a0.get("size") or 0,
            "notes": (d.get("body") or "")[:400],
            "has_update": bool(tag) and ver_key(tag) > ver_key(VERSION)}


def update_check():
    local = scan_local()
    newer = [x for x in local if x.get("valid") and ver_key(x["version"]) > ver_key(VERSION)]
    gh = gh_latest()
    return {"ok": True, "current": VERSION, "repo": UPDATE_REPO,
            "dirs": update_dirs(), "unsafe_dirs": dir_warnings(),
            "local": local[:10], "newer": newer[:5],
            "best_local": newer[0] if newer else None, "github": gh,
            "has_update": bool(newer) or bool(gh.get("has_update")),
            "state": _read_state()}


def _write_state(obj):
    try:
        os.makedirs(UPDATE_WORK, exist_ok=True)
        tmp = UPDATE_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, UPDATE_STATE)
    except Exception as e:
        log("写更新状态失败: %s" % e)


def _read_state():
    try:
        with open(UPDATE_STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"status": "idle"}


def _volume_index():
    """应用装在哪个卷：从 /vol1/@appconf/... 反推 1"""
    for s in ETC_DIR.split(os.sep):
        if len(s) > 3 and s.startswith("vol") and s[3:].isdigit():
            return int(s[3:])
    return 1


def _shq(s):
    """POSIX 单引号转义：值原样交给 shell，不做任何展开"""
    return "'" + str(s).replace("'", "'\\''") + "'"


def _upgrade_script():
    """生成升级脚本。

    安装包路径与版本号一律作为位置参数传入（$1 / $2），不拼进脚本文本。
    拼在双引号里时，文件名带 $(...) 或反引号照样会被 shell 展开，
    等于让「文件名」变成一条命令；改成参数传递就没有这层解释。
    """
    return """#!/bin/sh
set -u
PKG="$1"
VER="$2"
LOG=%(log)s
exec >>"$LOG" 2>&1
echo "$(date '+%%F %%T') === 升级 $VER ==="
appcenter-cli stop %(app)s
echo "stop rc=$?"
sleep 2
appcenter-cli uninstall %(app)s
echo "uninstall rc=$?"
sleep 2
appcenter-cli install-fpk "$PKG" -v %(vol)s
echo "install rc=$?"
sleep 2
appcenter-cli start %(app)s
echo "start rc=$?"
echo "$(date '+%%F %%T') === 结束 ==="
""" % {"log": _shq(UPDATE_LOG), "app": _shq(APPNAME),
       "vol": _shq(str(_volume_index()))}


def dir_warnings():
    """受信任目录里，组或其他用户可写的那些

    一键更新最终是 root 执行包里 cmd/ 的脚本，所以「谁能往这个目录写文件」
    才是真正的安全边界。放在共享盘上的目录要提醒一句。
    """
    out = []
    for d in update_dirs():
        try:
            st = os.stat(d)
        except OSError:
            continue
        if st.st_mode & 0o022:
            out.append(d)
    return out


def _install_worker(path, target):
    # 先把 HTTP 响应送出去，再动应用
    time.sleep(1.5)
    try:
        os.makedirs(UPDATE_WORK, exist_ok=True)
        script = os.path.join(UPDATE_WORK, "upgrade.sh")
        with open(script, "w", encoding="utf-8", newline="\n") as f:
            f.write(_upgrade_script())
        os.chmod(script, 0o755)
        with open(UPDATE_LOG, "a", encoding="utf-8") as lf:
            lf.write("\n")
        # start_new_session：hub 被 stop 掉后脚本继续跑完
        subprocess.Popen(["/bin/sh", script, path, target],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
        log("已启动升级脚本 %s" % script)
    except Exception as e:
        _write_state({"status": "failed", "target": target, "path": path,
                      "message": "%s: %s" % (type(e).__name__, e), "ts": int(time.time())})


def _reconcile_state():
    """进程重启后收敛上一条 installing 记录：版本对上才算成功"""
    st = _read_state()
    if st.get("status") != "installing":
        return
    tgt = str(st.get("target") or "")
    okv = bool(tgt) and ver_key(VERSION) >= ver_key(tgt)
    st.update({"status": "done" if okv else "failed", "now": VERSION,
               "finished": int(time.time())})
    if not okv:
        st["message"] = "升级后版本仍为 %s（目标 %s）" % (VERSION, tgt)
    _write_state(st)
    log("升级状态收敛：%s（当前 %s）" % (st["status"], VERSION))


def update_apply(obj):
    src = str(obj.get("source") or "local")
    if src != "local":
        return {"ok": False, "error": "当前只支持安装本机目录里的安装包"}
    path = os.path.realpath(str(obj.get("path") or ""))
    allowed = [os.path.realpath(d) for d in update_dirs()]
    if not any(path == a or path.startswith(a + os.sep) for a in allowed):
        return {"ok": False, "error": "安装包必须放在受信任目录内"}
    if not os.path.isfile(path):
        return {"ok": False, "error": "安装包不存在"}
    if not path.lower().endswith(".fpk"):
        return {"ok": False, "error": "只接受 .fpk 安装包"}
    info = fpk_info(path)
    if not info.get("valid"):
        return {"ok": False, "error": "安装包校验失败：appname 不是 %s 或 manifest 缺失" % APPNAME}
    if ver_key(info["version"]) <= ver_key(VERSION):
        return {"ok": False, "error": "该包版本 %s 不高于当前 %s" % (info["version"], VERSION)}
    _write_state({"status": "installing", "current": VERSION, "target": info["version"],
                  "path": path, "started": int(time.time())})
    threading.Thread(target=_install_worker,
                     args=(path, info["version"]), daemon=True).start()
    log("开始升级 %s -> %s" % (VERSION, info["version"]))
    return {"ok": True, "started": True, "target": info["version"]}


def update_log_tail(n=60):
    try:
        with open(UPDATE_LOG, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except Exception:
        return ""


# ---------------- 曲库目录 ----------------
# 容器只挂曲库里配置的那些目录，所以「新增目录」必须由宿主机这边判断路径真不真实：
# 容器里看不见一个刚加上的目录是正常的（还没挂进去），不能当成路径写错。
def _bridge_call(method, path, obj=None):
    body = json.dumps(obj).encode("utf-8") if obj is not None else None
    try:
        st, ct, data, ms = proxy(method, path, body,
                                 {"Content-Type": "application/json"})
    except Exception as e:
        return {"ok": False, "error": "桥接没响应：%s" % type(e).__name__}
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        return {"ok": False, "error": "桥接返回格式异常"}


def library_roots():
    r = _bridge_call("GET", "/api/library/roots")
    if not r.get("ok"):
        return r
    return {"ok": True, "roots": r.get("roots") or []}


def library_add(path):
    raw = (path or "").strip()
    if not raw:
        return {"ok": False, "error": "路径为空"}
    if not os.path.isabs(raw):
        return {"ok": False, "error": "请填 NAS 上的绝对路径，例如 /vol1/1000/音乐"}
    p = os.path.realpath(raw).rstrip("/") or "/"
    if not os.path.isdir(p):
        return {"ok": False, "error": "这台 NAS 上找不到目录：%s" % p}
    if not os.access(p, os.R_OK):
        return {"ok": False, "error": "目录存在但没有读权限（用 ACL 授权给运行用户）"}
    r = _bridge_call("POST", "/api/library/roots", {"path": p, "force": True})
    if r.get("ok"):
        # 容器按目录挂载，新目录要重启应用才会挂进去
        r["restart_required"] = bool(r.get("needs_restart"))
    return r


def library_remove(path):
    r = _bridge_call("POST", "/api/library/roots/remove", {"path": path})
    if r.get("ok"):
        r["restart_required"] = True
    return r


def app_restart():
    """重启整个应用（含桥接容器），让新的挂载生效。

    stop 会杀掉 hub 自己，所以脚本必须独立进程跑。
    """
    script = os.path.join(UPDATE_WORK, "restart.sh")
    try:
        os.makedirs(UPDATE_WORK, exist_ok=True)
        with open(script, "w", encoding="utf-8", newline="\n") as f:
            f.write("#!/bin/sh\nsleep 1\nappcenter-cli stop %s\nsleep 3\n"
                    "appcenter-cli start %s\n" % (APPNAME, APPNAME))
        os.chmod(script, 0o755)
        subprocess.Popen(["/bin/sh", script], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True,
                         close_fds=True)
        log("已触发应用重启")
        return {"ok": True, "started": True}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


# ---------------- 访问保护 ----------------
# 控制台直接监听局域网端口，默认谁都能开。这里做一层口令：
#   * 口令本身只存加盐哈希，cookie 里放的是另生成的随机 token，
#     所以拿到 token 也反推不出口令；
#   * token 额外落一份到桥接容器的 conf 目录，桥接据此做同一套校验，
#     两边共用同一个 cookie（cookie 不区分端口，登录一次两个端口都通）。
# 没有配置口令时保持开放（老用户升级后行为不变），界面上会提示未设防。
AUTH_FILE = os.path.join(ETC_DIR, "access.json")
BRIDGE_CONF = os.path.abspath(os.environ.get("HUB_BRIDGE_CONF")
                              or os.path.join(DATA_DIR, "bridge", "conf"))
COOKIE_NAME = "xc_token"
# 这些路径不做校验：/health 给探活用，音频给音箱自己拉（设备带不了 cookie）
OPEN_PATHS = ("/health", "/media", "/m/", "/audio/")

_auth_cache = {"mtime": -1.0, "data": {}}
_fail_lock = threading.Lock()
_fails = {}


def _load_auth():
    """读 access.json；文件没变就不重复解析"""
    try:
        st = os.stat(AUTH_FILE)
    except OSError:
        _auth_cache.update({"mtime": -1.0, "data": {}})
        return {}
    if st.st_mtime != _auth_cache["mtime"]:
        try:
            with open(AUTH_FILE, encoding="utf-8") as f:
                _auth_cache["data"] = json.load(f) or {}
        except Exception:
            _auth_cache["data"] = {}
        _auth_cache["mtime"] = st.st_mtime
    return _auth_cache["data"]


def auth_enabled():
    return bool(_load_auth().get("token"))


def _hash_pw(salt, pw):
    return hashlib.sha256((salt + "|" + pw).encode("utf-8")).hexdigest()


def _write_bridge_token(token):
    """把 token 同步给桥接容器（读不到就当没配，桥接那边会自己降级为开放）"""
    try:
        os.makedirs(BRIDGE_CONF, exist_ok=True)
        p = os.path.join(BRIDGE_CONF, "access.token")
        if token:
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(token + "\n")
            os.replace(tmp, p)
        else:
            try:
                os.remove(p)
            except OSError:
                pass
    except Exception as e:
        log("同步桥接口令失败：%s" % e)


def auth_set(password):
    """设置/清除口令。空字符串 = 关闭保护"""
    password = (password or "").strip()
    _auth_cache["mtime"] = -1.0
    if not password:
        for p in (AUTH_FILE, os.path.join(BRIDGE_CONF, "access.token")):
            try:
                os.remove(p)
            except OSError:
                pass
        _write_bridge_token("")
        log("访问保护已关闭")
        return {"ok": True, "enabled": False}
    if len(password) < 4:
        return {"ok": False, "error": "口令至少 4 位"}
    salt = secrets.token_hex(8)
    token = secrets.token_urlsafe(24)
    obj = {"salt": salt, "hash": _hash_pw(salt, password), "token": token,
           "ts": int(time.time())}
    try:
        os.makedirs(ETC_DIR, exist_ok=True)
        tmp = AUTH_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, AUTH_FILE)
    except Exception as e:
        return {"ok": False, "error": "写入口令文件失败：%s" % e}
    _auth_cache.update({"mtime": -1.0, "data": obj})
    _write_bridge_token(token)
    log("访问保护已开启")
    return {"ok": True, "enabled": True, "token": token}


def auth_check(password, ip):
    """校验口令。同一来源 60 秒内错 5 次就锁一会儿"""
    with _fail_lock:
        now = time.time()
        rec = [t for t in _fails.get(ip, []) if now - t < 60]
        _fails[ip] = rec
        if len(rec) >= 5:
            return {"ok": False, "error": "错误次数过多，请 1 分钟后再试"}
    d = _load_auth()
    if not d.get("hash"):
        return {"ok": False, "error": "未设置口令"}
    if hmac.compare_digest(_hash_pw(d.get("salt", ""), password or ""), d["hash"]):
        with _fail_lock:
            _fails.pop(ip, None)
        return {"ok": True, "token": d.get("token", "")}
    with _fail_lock:
        _fails.setdefault(ip, []).append(now)
    return {"ok": False, "error": "口令不对"}


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>小爱音响控制台 · 需要口令</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
 background:#14120f;color:#e9e2d6;
 font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.box{width:min(360px,92vw);padding:28px 26px 22px;border:1px solid #2e2a24;border-radius:14px;
 background:#1b1814;box-shadow:0 18px 50px rgba(0,0,0,.45)}
h1{margin:0 0 6px;font-size:17px;font-weight:600;letter-spacing:.02em}
p{margin:0 0 18px;color:#8f867a;font-size:13px}
input{width:100%;padding:10px 12px;border-radius:9px;border:1px solid #38322a;
 background:#12100d;color:#e9e2d6;font-size:15px;outline:none}
input:focus{border-color:#c8892f}
button{margin-top:14px;width:100%;padding:10px;border:0;border-radius:9px;cursor:pointer;
 background:#c8892f;color:#1a1409;font-size:14px;font-weight:600}
button:disabled{opacity:.6;cursor:default}
.err{margin-top:10px;color:#e0705c;font-size:13px;min-height:18px}
.foot{margin-top:16px;color:#6b6459;font-size:12px;text-align:center}
</style></head><body>
<div class="box">
  <h1>小爱音响控制台</h1>
  <p>该设备已开启访问保护，请输入口令。</p>
  <form id="f"><input id="pw" type="password" autocomplete="current-password" autofocus placeholder="访问口令">
  <button id="b" type="submit">进入</button></form>
  <div class="err" id="e"></div>
  <div class="foot">口令在安装本应用的 NAS 上设置</div>
</div>
<script>
var f=document.getElementById('f'),pw=document.getElementById('pw');
var b=document.getElementById('b'),e=document.getElementById('e');
f.onsubmit=function(ev){
  ev.preventDefault();
  b.disabled=true;e.textContent='';
  fetch('api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
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


# ---------------- 代理 ----------------
def proxy(method, path, body=None, headers=None, timeout=300):
    url = BRIDGE_DEFAULT.rstrip("/") + path
    req = urllib.request.Request(url, data=body, method=method)
    for k in ("Content-Type", "Accept", "Authorization"):
        if headers and headers.get(k):
            req.add_header(k, headers[k])
    # 桥接那侧也要过口令：这里用本机身份替浏览器带上
    tok = _load_auth().get("token")
    if tok:
        req.add_header("X-XC-Token", tok)
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        return r.status, r.headers.get("Content-Type", "application/json"), data, int((time.time() - t0) * 1000)


def localize(url):
    """把探测地址一律换成本机回环。

    hub 与桥接永远同机部署，但浏览器传来的 url 用的是 LAN IP
    （http://<NAS 的局域网地址>:8790/）。fnOS 的防火墙会把自己访问自己
    LAN IP 的连接吞掉（实测 5s 超时），导致概览卡长期显示「离线」。
    """
    try:
        u = urlsplit(url)
    except Exception:
        return url
    if not u.hostname:
        return url
    try:
        port = u.port
    except ValueError:
        port = None
    if not port:
        return url
    return f"http://127.0.0.1:{port}{u.path or '/'}" + (f"?{u.query}" if u.query else "")


def probe(url, timeout=3.0):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(64)
            return {"ok": r.status < 500, "status": r.status, "ms": int((time.time() - t0) * 1000)}
    except urllib.error.HTTPError as e:
        # 4xx 说明服务活着（只是路径/鉴权问题）；5xx 视为挂了
        return {"ok": e.code < 500, "status": e.code, "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "ms": int((time.time() - t0) * 1000)}


class Handler(BaseHTTPRequestHandler):
    server_version = "xiaoai-console/" + VERSION
    protocol_version = "HTTP/1.1"

    # ---------- 工具 ----------
    def address_string(self):
        try:
            return self.client_address[0]
        except Exception:
            return "unix"

    @staticmethod
    def _norm(path):
        """去掉网关前缀 /app/xiaoai-console（网关剥没剥都要兼容）"""
        if path == GATEWAY_PREFIX:
            return "/"
        if path.startswith(GATEWAY_PREFIX + "/"):
            return path[len(GATEWAY_PREFIX):] or "/"
        return path

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if ctype.startswith("application/json") else "public, max-age=60")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n > 0 else b""

    def _host(self):
        h = self.headers.get("Host") or ""
        h = h.split(":")[0].strip()
        return h or "127.0.0.1"

    def log_message(self, fmt, *args):
        log("%s - %s" % (self.address_string(), fmt % args))

    # ---------- 访问保护 ----------
    def _cookie_token(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE_NAME:
                return v.strip()
        return ""

    def _given_token(self):
        tok = self._cookie_token()
        if tok:
            return tok
        tok = (self.headers.get("X-XC-Token") or "").strip()
        if tok:
            return tok
        q = parse_qs(urlparse(self.path).query)
        return (q.get("token") or [""])[0].strip()

    def _authed(self):
        want = _load_auth().get("token") or ""
        if not want:
            return True
        got = self._given_token()
        return bool(got) and hmac.compare_digest(got, want)

    def _set_cookie(self, token, clear=False):
        if clear or not token:
            c = "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" % COOKIE_NAME
        else:
            c = ("%s=%s; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax"
                 % (COOKIE_NAME, token))
        return {"Set-Cookie": c}

    def _gate(self, p):
        """返回 None 表示放行；否则已经把响应写回去了"""
        if not auth_enabled():
            return False
        if p.startswith(OPEN_PATHS):
            return False
        if self._authed():
            return False
        if p == "/" or p == "/index.html":
            body = LOGIN_PAGE.encode("utf-8")
            self._send(401, body, "text/html; charset=utf-8")
        else:
            self._json(401, {"ok": False, "error": "需要口令", "auth": True})
        return True

    # ---------- 静态 ----------
    def _static(self, path):
        rel = unquote(path).lstrip("/")
        if rel in ("", "/"):
            rel = "index.html"
        # 阻止路径穿越
        rel = posixpath.normpath(rel)
        if rel.startswith("..") or rel.startswith("/"):
            return self._json(404, {"ok": False, "error": "not found"})
        full = os.path.join(UI_DIR, rel)
        if not os.path.isfile(full):
            return self._json(404, {"ok": False, "error": "not found", "path": rel})
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            data = f.read()
        return self._send(200, data, MIME.get(ext, "application/octet-stream"))

    # ---------- 路由 ----------
    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        p = self._norm(u.path)

        if self._gate(p):
            return

        if p == "/health":
            return self._json(200, {"ok": True, "version": VERSION})

        if p == "/api/auth/status":
            return self._json(200, {"ok": True, "enabled": auth_enabled(),
                                    "authed": self._authed()})

        if p == "/api/hub/meta":
            return self._json(200, {
                "ok": True, "version": VERSION,
                "host": self._host(), "port": PORT,
                "bridge": BRIDGE_DEFAULT,
                "auth": auth_enabled(),
            })

        if p == "/api/update/check":
            try:
                return self._json(200, update_check())
            except Exception as e:
                return self._json(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

        if p == "/api/update/state":
            return self._json(200, {"ok": True, "current": VERSION, "state": _read_state()})

        if p == "/api/update/log":
            return self._json(200, {"ok": True, "log": update_log_tail()})

        if p == "/api/library/roots":
            try:
                return self._json(200, library_roots())
            except Exception as e:
                return self._json(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

        if p.startswith("/proxy/bridge"):
            path = p[len("/proxy/bridge"):] or "/"
            if u.query:
                path += "?" + u.query
            try:
                st, ct, data, ms = proxy("GET", path, None,
                                         {"Content-Type": self.headers.get("Content-Type", "")})
                return self._send(st, data, ct, {"X-Proxy-Ms": str(ms)})
            except urllib.error.HTTPError as e:
                return self._send(e.code, e.read(), e.headers.get("Content-Type", "application/json"))
            except Exception as e:
                return self._json(502, {"ok": False, "error": f"小爱音箱桥接服务连不上：{type(e).__name__}"})

        return self._static(p)

    def do_POST(self):
        u = urlparse(self.path)
        p = self._norm(u.path)
        raw = self._body()

        # 登录必须放在口令校验之前，否则永远进不去
        if p == "/api/auth/login":
            try:
                obj = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                obj = {}
            r = auth_check(str(obj.get("password") or ""), self.address_string())
            if not r.get("ok"):
                return self._json(401, r)
            # 桥接（8790）先被打开时，会把口令转发到这里校验，
            # 再拿返回的 token 自己下发同一枚 cookie，所以这里要带上
            return self._send(200,
                              json.dumps({"ok": True, "token": r.get("token", "")}).encode("utf-8"),
                              "application/json; charset=utf-8",
                              self._set_cookie(r.get("token", "")))

        if self._gate(p):
            return

        if p == "/api/auth/save":
            try:
                obj = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                obj = {}
            r = auth_set(str(obj.get("password") or ""))
            if not r.get("ok"):
                return self._json(400, r)
            body = {k: v for k, v in r.items() if k != "token"}
            return self._send(200, json.dumps(body, ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8",
                              self._set_cookie(r.get("token", ""), clear=not r.get("enabled")))

        if p == "/api/library/roots":
            try:
                obj = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                obj = {}
            act = str(obj.get("action") or "add")
            try:
                res = library_remove(obj.get("path")) if act == "remove" \
                    else library_add(obj.get("path"))
            except Exception as e:
                res = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            return self._json(200 if res.get("ok") else 400, res)

        if p == "/api/app/restart":
            return self._json(200, app_restart())

        if p == "/api/probe":
            try:
                obj = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                obj = {}
            url = str(obj.get("url") or "").strip()
            if not url:
                url = BRIDGE_DEFAULT
            return self._json(200, probe(localize(url)))

        if p == "/api/update/apply":
            try:
                obj = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                obj = {}
            try:
                res = update_apply(obj)
            except Exception as e:
                res = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            return self._json(200 if res.get("ok") else 400, res)

        if p.startswith("/proxy/bridge"):
            path = p[len("/proxy/bridge"):] or "/"
            if u.query:
                path += "?" + u.query
            try:
                st, ct, data, ms = proxy("POST", path, raw,
                                         {"Content-Type": self.headers.get("Content-Type", "application/json")})
                return self._send(st, data, ct, {"X-Proxy-Ms": str(ms)})
            except urllib.error.HTTPError as e:
                return self._send(e.code, e.read(), e.headers.get("Content-Type", "application/json"))
            except Exception as e:
                return self._json(502, {"ok": False, "error": f"小爱音箱桥接服务连不上：{type(e).__name__}"})

        return self._json(404, {"ok": False, "error": "not found"})


class UnixHTTPServer(ThreadingHTTPServer):
    """在 unix 套接字上提供 HTTP，供平台网关转发 /app/xiaoai-console/ 进来"""
    address_family = socket.AF_UNIX

    def server_bind(self):
        try:
            os.unlink(self.server_address)
        except OSError:
            pass
        self.socket.bind(self.server_address)
        try:
            os.chmod(self.server_address, 0o666)
        except OSError:
            pass
        self.server_address = self.socket.getsockname()
        self.server_name = "localhost"
        self.server_port = 0


def main():
    for d in (DATA_DIR, ETC_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception as e:
            log("创建目录失败 %s: %s" % (d, e))

    log("UI_DIR=%s" % UI_DIR)
    log("ETC_DIR=%s" % ETC_DIR)
    log("BRIDGE=%s" % BRIDGE_DEFAULT)

    try:
        _reconcile_state()
    except Exception as e:
        log("收敛升级状态失败: %s" % e)

    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.daemon_threads = True

    try:
        os.makedirs(os.path.dirname(SOCK), exist_ok=True)
        usock = UnixHTTPServer(SOCK, Handler)
        usock.daemon_threads = True
        threading.Thread(target=usock.serve_forever, daemon=True).start()
        log("listening on unix:%s" % SOCK)
    except Exception as e:
        log("unix 套接字不可用（仅 TCP）：", e)

    log("listening on 0.0.0.0:%d" % PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
