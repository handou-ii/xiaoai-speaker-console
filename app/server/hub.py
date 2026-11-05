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
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, urlsplit, unquote

VERSION = "1.4.13"

PORT = int(os.environ.get("HUB_PORT") or 8900)
# 桌面 iframe 走平台网关时挂在 /app/fnoshub/ 前缀下；直连端口则挂在 /
GATEWAY_PREFIX = "/app/fnoshub"
# 网关套接字：应用根目录/fnoshub.sock（ui/config 的 gatewaySocket 指向它）
SOCK = os.environ.get("HUB_SOCK") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "fnoshub.sock"))
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
    server_version = "fnos-hub/" + VERSION
    protocol_version = "HTTP/1.1"

    # ---------- 工具 ----------
    def address_string(self):
        try:
            return self.client_address[0]
        except Exception:
            return "unix"

    @staticmethod
    def _norm(path):
        """去掉网关前缀 /app/fnoshub（网关剥没剥都要兼容）"""
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
    """在 unix 套接字上提供 HTTP，供平台网关转发 /app/fnoshub/ 进来"""
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
