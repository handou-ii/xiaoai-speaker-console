#!/usr/bin/env python3
"""小爱音响控制台 · Hub 后端

只用标准库，不依赖任何 pip 包，避免在 NAS 上装环境。

职责：
  1. 提供静态页面（app/ui）
  2. 反向代理到本机各应用端口，前端因此不用处理跨域
  3. 探测应用是否在线
"""
import json
import os
import posixpath
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, urlsplit, unquote

VERSION = "1.5.4"

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
            "dirs": update_dirs(), "local": local[:10], "newer": newer[:5],
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


def _upgrade_script(path, target):
    """生成升级脚本。

    appcenter-cli 的 install-fpk 遇到同 appname 已安装时只打印
    「Application [x] is installed.」就返回 0，根本不覆盖文件，
    所以升级必须自己拆成 卸载 -> 安装 -> 启动 三步。
    stop 会杀掉 hub 本身，这整段因此要交给独立进程执行。
    """
    return """#!/bin/sh
set -u
LOG="{log}"
STATE="{state}"
exec >>"$LOG" 2>&1
echo "$(date '+%F %T') === 升级 {target} ==="
appcenter-cli stop {app}
echo "stop rc=$?"
sleep 2
appcenter-cli uninstall {app}
echo "uninstall rc=$?"
sleep 2
appcenter-cli install-fpk "{path}" -v {vol}
echo "install rc=$?"
sleep 2
appcenter-cli start {app}
echo "start rc=$?"
echo "$(date '+%F %T') === 结束 ==="
""".format(log=UPDATE_LOG, state=UPDATE_STATE, target=target,
           app=APPNAME, path=path, vol=_volume_index())


def _install_worker(path, target):
    # 先把 HTTP 响应送出去，再动应用
    time.sleep(1.5)
    try:
        os.makedirs(UPDATE_WORK, exist_ok=True)
        script = os.path.join(UPDATE_WORK, "upgrade.sh")
        with open(script, "w", encoding="utf-8", newline="\n") as f:
            f.write(_upgrade_script(path, target))
        os.chmod(script, 0o755)
        with open(UPDATE_LOG, "a", encoding="utf-8") as lf:
            lf.write("\n")
        # start_new_session：hub 被 stop 掉后脚本继续跑完
        subprocess.Popen(["/bin/sh", script],
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


# ---------------- 代理 ----------------
def proxy(method, path, body=None, headers=None, timeout=300):
    url = BRIDGE_DEFAULT.rstrip("/") + path
    req = urllib.request.Request(url, data=body, method=method)
    for k in ("Content-Type", "Accept", "Authorization"):
        if headers and headers.get(k):
            req.add_header(k, headers[k])
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

        if p == "/health":
            return self._json(200, {"ok": True, "version": VERSION})

        if p == "/api/hub/meta":
            return self._json(200, {
                "ok": True, "version": VERSION,
                "host": self._host(), "port": PORT,
                "bridge": BRIDGE_DEFAULT,
                "ui_dir": UI_DIR, "etc_dir": ETC_DIR,
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
