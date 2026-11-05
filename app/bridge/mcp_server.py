#!/usr/bin/env python3
"""MCP Server：把音箱与音乐库暴露成 agent 可调用的工具

传输：stdio，JSON-RPC 2.0，按行分隔。
后端：调用本机 xiaoai-bridge 的 HTTP 接口（默认 http://127.0.0.1:8790）。

OpenClaw 配置示例（~/.openclaw/openclaw.json）：

  "mcpServers": {
    "xiaoai": {
      "transport": "stdio",
      "command": "/vol1/docker/xiaoai-bridge/.venv/bin/python",
      "args": ["/vol1/docker/xiaoai-bridge/mcp_server.py"],
      "env": { "XIAOAI_BRIDGE_URL": "http://127.0.0.1:8790" }
    }
  }
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("XIAOAI_BRIDGE_URL", "http://127.0.0.1:8790").rstrip("/")
SERVER_INFO = {"name": "xiaoai", "version": "0.1.0"}
PROTOCOL = "2024-11-05"

TOOLS = [
    {
        "name": "speaker_say",
        "description": "让小爱音箱说一句话。用于口头回复用户。会自动分段并等待播完。",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要朗读的文本，口语化、简短"}},
            "required": ["text"],
        },
    },
    {
        "name": "speaker_play_local",
        "description": ("在 NAS 本地音乐库里搜索并按队列播放。适合'放周杰伦的歌''随机播粤语歌'这类请求。"
                        "立即返回，后续自动逐首续播。返回队列长度和第一首。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "歌手、歌名或关键词，如 '周杰伦'、'晴天'"},
                "mode": {"type": "string", "enum": ["shuffle", "seq", "one"], "description": "播放模式，默认 shuffle"},
                "limit": {"type": "integer", "description": "最多入队多少首，默认 30"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "speaker_next",
        "description": "切到下一首。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "speaker_prev",
        "description": "回到上一首。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "speaker_stop",
        "description": "停止播放并清空队列。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "speaker_volume",
        "description": ("设置小爱音箱音量（0-100）。用户说「太小声」「太大声」「调到 40」时使用。"
                        "播放本地音乐前如果音量偏低也可以先调到 35 左右。不传 volume 则返回当前音量。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "volume": {"type": "integer", "minimum": 0, "maximum": 100,
                           "description": "目标音量，0-100；不传则查询当前音量"},
            },
        },
    },
    {
        "name": "speaker_status",
        "description": "查询当前播放状态：在播什么、队列第几首、共几首、接下来几首。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "music_search",
        "description": "只搜索不播放。返回匹配的曲目列表（id/歌名/歌手/专辑），用于确认有没有这首歌。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词"},
                "limit": {"type": "integer", "description": "最多返回几条，默认 10"},
            },
            "required": ["query"],
        },
    },
]


def http_json(path, payload=None, timeout=120):
    url = BASE + path
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def call_tool(name, args):
    if name == "speaker_say":
        return http_json("/api/say", {"text": args.get("text", "")}, timeout=300)
    if name == "speaker_play_local":
        return http_json("/api/play_local", {
            "query": args.get("query", ""),
            "mode": args.get("mode", "shuffle"),
            "limit": args.get("limit", 30),
        })
    if name == "speaker_next":
        return http_json("/api/next", {})
    if name == "speaker_prev":
        return http_json("/api/prev", {})
    if name == "speaker_stop":
        return http_json("/api/stop", {})
    if name == "speaker_volume":
        vol = args.get("volume")
        return http_json("/api/volume", {} if vol is None else {"volume": vol})
    if name == "speaker_status":
        return http_json("/api/status")
    if name == "music_search":
        return http_json("/api/search", {
            "query": args.get("query", ""),
            "limit": args.get("limit", 10),
        })
    return {"ok": False, "error": f"未知工具 {name}"}


def reply(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(msg):
    method = msg.get("method")
    id_ = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return reply(id_, {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "tools/list":
        return reply(id_, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            data = call_tool(name, args)
        except Exception as e:
            data = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return reply(id_, {
            "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
            "isError": not data.get("ok", True),
        })
    if method == "ping":
        return reply(id_, {})
    if id_ is not None:
        return reply(id_, error={"code": -32601, "message": f"Method not found: {method}"})
    return None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        handle(msg)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
