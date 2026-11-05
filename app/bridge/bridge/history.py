"""运行期事件环形缓冲

WebUI 要展示"刚刚发生了什么"：语音指令、agent 回答、耗时、播放动作。
这些都只对当前进程有意义，所以放内存，不落盘。
"""
import collections
import time


class History:
    def __init__(self, maxlen=200):
        self._items = collections.deque(maxlen=maxlen)
        self._seq = 0

    def add(self, kind, **fields):
        self._seq += 1
        item = {"seq": self._seq, "kind": kind, "ts": time.time()}
        item.update(fields)
        self._items.appendleft(item)      # 最新在前
        return item

    def command(self, query, intent="", reply="", ms=None, ok=True, stage="",
                **extra):
        """一条语音指令的完整生命周期

        extra 用来带 route / note / plan 这类附加字段 —— main.py 会往里塞
        "这条指令归谁处理"，WebUI 的历史列表直接展示，不要写死参数表。
        """
        return self.add("command", query=query, intent=intent, reply=reply,
                        ms=ms, ok=ok, stage=stage, **extra)

    def action(self, text, **fields):
        """WebUI 或 MCP 触发的动作"""
        return self.add("action", text=text, **fields)

    def list(self, limit=50, kind=None):
        items = list(self._items)
        if kind:
            items = [i for i in items if i["kind"] == kind]
        return items[:limit]

    def stats(self, window=20):
        """最近若干条语音指令的耗时与成功率，给概览卡用"""
        cmds = [i for i in list(self._items) if i["kind"] == "command"][:window]
        done = [c for c in cmds if c.get("ms")]
        if not done:
            return {"count": len(cmds), "avg_ms": None, "max_ms": None,
                    "fail": 0}
        return {
            "count": len(cmds),
            "avg_ms": int(sum(c["ms"] for c in done) / len(done)),
            "max_ms": int(max(c["ms"] for c in done)),
            "fail": sum(1 for c in cmds if c.get("ok") is False),
        }
