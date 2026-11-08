#!/usr/bin/env python3
"""把 xiaoai MCP 切到宿主 python3，并强制网关重新加载（删条目 → 再加回）

网关不会重试启动失败的 MCP server，热加载只改配置不改进程，
所以用"删掉再加回"触发一次全新的 initialize。
"""
import copy
import json
import os
import time

CONF = "/vol1/@apphome/trim.openclaw/data/home/.openclaw/openclaw.json"
NEW_CMD = "/usr/bin/python3"


def load():
    with open(CONF, encoding="utf-8") as f:
        return json.load(f)


def save(data):
    st = os.stat(CONF)
    tmp = CONF + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, st.st_mode)
    os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, CONF)


def main():
    data = load()
    servers = data["mcp"]["servers"]
    old = servers.get("xiaoai")
    if not old:
        raise SystemExit("找不到 xiaoai 条目")

    old = copy.deepcopy(old)
    old["command"] = NEW_CMD
    old.pop("args", None)
    old["args"] = ["/path/to/xiaoai-bridge/mcp_server.py"]
    old.setdefault("env", {})["XIAOAI_BRIDGE_URL"] = "http://127.0.0.1:8790"

    # 1) 先删，让网关做一次 reload（此时会关掉旧的 MCP 进程）
    del servers["xiaoai"]
    save(data)
    print("removed, waiting for reload...")
    time.sleep(12)

    # 2) 加回，网关重新 spawn
    data = load()
    data["mcp"]["servers"]["xiaoai"] = old
    save(data)
    print("re-added with command =", NEW_CMD)
    time.sleep(12)

    data = load()
    print(json.dumps(data["mcp"]["servers"]["xiaoai"], ensure_ascii=False))


if __name__ == "__main__":
    main()
