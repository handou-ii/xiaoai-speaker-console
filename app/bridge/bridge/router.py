"""意图路由：能在本地做完的，绝不惊动 OpenClaw

处理顺序（命中即停）：

  1. 自定义固定指令  —— 用户在 WebUI 里配的（含"只播某个文件夹"这类固定动作）
  2. 内置指令        —— 播放/暂停/切歌/音量/顺序模式/状态，纯本地
  3. OpenClaw        —— 只剩"NAS 相关的问答与操作"这类真正需要 agent 的事

设计边界（重要）：
  * 播放链路 = 曲库检索 + 推流 + 队列推进，**全在 bridge 内完成**，
    speaker 只认小米账号的 serviceToken，跟 OpenClaw 有没有连上无关。
  * OpenClaw 不可用时的表现是「问 NAS 的事答不上来」，而不是「放不了歌」。
"""
import json
import logging
import os
import random
import re
import time
import uuid

from .rules import normalize, strip_device_wake

log = logging.getLogger("router")

# 需要交给 OpenClaw 的话题：都是"NAS 上有什么/怎么样"，本地曲库答不了
DEFAULT_OPENCLAW_KEYWORDS = [
    "nas", "硬盘", "磁盘", "存储", "空间", "容量", "内存", "cpu", "温度",
    "docker", "容器", "进程", "服务", "日志", "系统", "网络", "ip", "备份",
    "下载", "任务", "相册", "文件", "文件夹", "目录", "同步", "关机", "重启",
]

ACTION_TYPES = ["play", "play_folder", "play_track", "pause", "resume", "next",
                "prev", "stop", "volume", "mode", "say", "status", "rescan",
                "openclaw"]

MODE_NAMES = {"seq": "顺序", "shuffle": "随机", "loop": "循环"}

# ---------- 内置指令 ----------
RE_PAUSE = re.compile(r"^(暂停|暂停播放|先停|停一下|停一会儿|别放了|放一边|停停)")
RE_RESUME = re.compile(r"^(继续|继续播|继续放|接着放|接着播|恢复|恢复播放|往下放)")
RE_NEXT = re.compile(r"^(下一首|下首|下一个|切歌|换一首|换首歌|换一个|跳一首|过一首)")
RE_PREV = re.compile(r"^(上一首|上首|上一个|前一首|回上一首|倒回去)")
RE_STOP = re.compile(r"^(停止|停止播放|停下|关掉音乐|关闭音乐|别唱了|关了|退出音乐|不听了)")
RE_STATUS = re.compile(r"(现在放的?是?什么|在放什么|放的什么|什么歌|播放状态|在播什么|放的是谁)")
RE_RESCAN = re.compile(r"(重新扫描|刷新曲库|重建索引|更新曲库|重新索引)")

# 本地唤醒词后面跟的到底是一首歌还是一个问句？
# 问句（「本地纳斯还有多少空间」）交给后台，其余当点播处理。
RE_QUESTION = re.compile(
    r"(多少|几个|几点|怎么|怎样|什么|为什么|哪|谁|是不是|能不能|可不可以|"
    r"有没有|如何|多久|多大|多远|吗|呢|帮我查|查一下|看看|告诉我)")
RE_VOL_SET = re.compile(r"(?:音量|声音|音量调到|音量调成)[^0-9]{0,4}(\d{1,3})")
RE_VOL_UP = re.compile(r"(大点声|大声点|大一点声|音量大点|音量调大|调大音量|声音大点|听不清)")
RE_VOL_DOWN = re.compile(r"(小点声|小声点|小一点声|音量小点|音量调小|调小音量|声音小点|太吵)")
RE_MODE_SEQ = re.compile(r"(顺序播放|按顺序|顺序放|顺序播)")
RE_MODE_SHUFFLE = re.compile(r"(随机播放|随机放|随便放|打乱)")
RE_MODE_LOOP = re.compile(r"(循环播放|单曲循环|一直循环|循环放)")
RE_PLAY = re.compile(
    r"^(播放|放一首|放点|放一下|放|来一首|来点|来一段|来|听一首|听点|听|"
    r"我想听|想听|唱一首|唱|整一首|整点)(.*)$")

# 查询词里要剥掉的尾巴/前缀
TRIM_HEAD = re.compile(r"^(一下|一点|一些|首|点|个|几首|些|的|吧|呗|啊|呀|呢|\s)+")
TRIM_TAIL = re.compile(r"(的歌|歌曲|的音乐|音乐|歌|吧|呗|啊|呀|呢|好吗|好不好|\s)+$")

# 用户说「播放回忆」时，唤醒词后面通常带一个播放动词；自定义指令里一般不会写。
# 匹配前先剥掉这层动词再做精确/开头比较，否则"三巨头早年歌曲"这种精确指令永远命中不了。
PLAY_LEAD = re.compile(
    r"^(?:请|帮我|给我|我想|想听|想|播放|放一首|放点|放一下|放|来一首|来点|来|"
    r"听一首|听点|听|唱一首|整一首|整点|整)+")

PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _core(intent):
    """去掉说话时的播放动词，用于精确/开头匹配"""
    s = PLAY_LEAD.sub("", intent or "").strip()
    return s or (intent or "")



class Plan:
    """一次指令的处理计划：先说话，再动作（避免播报把刚起的音乐掐掉）"""

    def __init__(self, handled, reply="", action=None, source="local",
                 note="", detail=None, owns_playback=False):
        self.handled = handled
        self.reply = reply
        self.action = action
        self.source = source
        self.note = note
        self.detail = detail or {}
        # 这个动作会不会接管播放？会的话说完话不必把旧队列接回来
        self.owns_playback = owns_playback

    def as_dict(self):
        return {
            "handled": self.handled,
            "reply": self.reply,
            "source": self.source,
            "note": self.note,
            "detail": self.detail,
            "has_action": callable(self.action),
            "owns_playback": self.owns_playback,
        }


def _fill(tpl, **kw):
    def sub(m):
        return str(kw.get(m.group(1), m.group(0)))
    return PLACEHOLDER.sub(sub, tpl or "")


# --------------------------------------------------------------------------
class CommandStore:
    """自定义固定指令，落盘 conf/commands.json（跟着 conf 卷持久化）"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.path = cfg.abspath(cfg.router.get("commands_file") or "./conf/commands.json")
        self.items = []
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.items = [c for c in (data.get("commands") or []) if c.get("patterns")]
        except FileNotFoundError:
            self.items = []
        except Exception as e:
            log.warning("自定义指令读取失败：%s", e)
            self.items = []
        return self.items

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"commands": self.items}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        return True

    def list(self):
        return sorted(self.items, key=lambda c: -int(c.get("priority", 0)))

    def get(self, cid):
        for c in self.items:
            if c.get("id") == cid:
                return c
        return None

    def add(self, data):
        cmd = self._normalize(data, new=True)
        self.items.append(cmd)
        self.save()
        return cmd

    def update(self, cid, data):
        cur = self.get(cid)
        if not cur:
            return None
        cmd = self._normalize({**cur, **data, "id": cid}, new=False)
        self.items = [cmd if c.get("id") == cid else c for c in self.items]
        self.save()
        return cmd

    def delete(self, cid):
        before = len(self.items)
        self.items = [c for c in self.items if c.get("id") != cid]
        if len(self.items) != before:
            self.save()
            return True
        return False

    def _normalize(self, data, new):
        action = data.get("action") or {}
        atype = action.get("type") or "play"
        if atype not in ACTION_TYPES:
            raise ValueError(f"未知动作类型：{atype}")
        patterns = [str(p).strip() for p in (data.get("patterns") or []) if str(p).strip()]
        if not patterns:
            raise ValueError("至少需要一个触发词")
        match = data.get("match") or "contains"
        if match not in ("contains", "exact", "startswith", "regex"):
            raise ValueError(f"未知匹配方式：{match}")
        if match == "regex":
            for p in patterns:                       # 提前编译，坏正则当场报错
                re.compile(p)
        return {
            "id": data.get("id") or f"cmd_{uuid.uuid4().hex[:8]}",
            "name": (data.get("name") or patterns[0])[:40],
            "enabled": bool(data.get("enabled", True)),
            "match": match,
            "patterns": patterns[:12],
            "action": action,
            "reply": (data.get("reply") or "").strip(),
            "priority": int(data.get("priority", 50)),
            "created_at": data.get("created_at") or int(time.time()),
            "hit": int(data.get("hit", 0)),
        }


# --------------------------------------------------------------------------
class Router:
    def __init__(self, bridge):
        self.bridge = bridge
        self.cfg = bridge.cfg
        self.store = CommandStore(self.cfg)
        self.stats = {"local": 0, "custom": 0, "openclaw": 0, "fallback": 0}
        self._wake_all = False

    # ---------- 对外 ----------
    async def plan(self, intent, wake_hit=True, wake_scope=None, wake_name=None):
        """wake_scope: local = 本地唤醒词（只在本机做）；openclaw = 触发词（整句给 agent）"""
        intent = (intent or "").strip()
        if not intent:
            return Plan(True, "在呢，想听点什么？", source="local")

        # 命中 OpenClaw 触发词（如「让小龙虾」「跟小龙虾说」）：整句转出去，
        # 不查固定指令、也不猜内置意图 —— 这两套唤醒词是互不相干的。
        if wake_scope == "openclaw":
            self._wake_all = True
            try:
                return self._to_openclaw(intent)
            finally:
                self._wake_all = False

        # 本地唤醒词（或没叫名字）：固定指令 → 内置意图 → 本地回绝，绝不转 agent
        plan = self._custom(intent)
        if plan:
            self.stats["custom"] += 1
            return plan
        plan = await self._builtin(intent)
        if plan:
            self.stats["local"] += 1
            return plan
        # 「播放本地 晴天」这种：剥掉唤醒词后只剩歌名/歌手，没有动词，
        # 内置规则匹配不上。先当点播试一次，本地曲库能命中就本地放。
        if intent and not RE_QUESTION.search(intent):
            _mode = self.bridge.player.mode
            if _mode not in ("seq", "shuffle", "loop"):
                _mode = "shuffle"
            p = self._plan_play(intent, _mode, 30, "", "local", "播放")
            if p and (p.detail or {}).get("first"):
                self.stats["local"] += 1
                return p
        # 本地确实答不了（比如「本地纳斯还剩多少空间」）：别硬拒绝，
        # 后台开着就交给它；没开才回那句提示。
        if getattr(self.bridge.agent, "enabled", False):
            return self._to_openclaw(intent)
        return Plan(True, self._local_only_tip(), source="local",
                    note="本地唤醒词：没有对应的固定指令")

    @property
    def wake_local_intents(self):
        """不管命中哪个唤醒词都用内置意图本地处理（默认否）"""
        return bool(self.cfg.router.get("wake_local_intents"))

    @property
    def bare_enabled(self):
        """没带唤醒词时是否仍响应自定义指令（WebUI 开关，默认关）"""
        return bool(self.cfg.wake.get("bare_commands"))

    def bare_plan(self, text):
        """没叫唤醒词时的兜底：只认自定义指令

        为什么只认自定义指令：小爱会把用户的每句话都记进对话，如果内置的
        「播放X」也参与兜底，那么「播放周杰伦」这类本来该由小爱自己处理的请求
        会被一起劫持。自定义指令是用户显式建的，命中即代表「这句我就是要本地做」。
        """
        if not self.bare_enabled or not (text or "").strip():
            return None
        clean = strip_device_wake(normalize(text))
        if not clean:
            return None
        return self._custom(clean)

    def preview(self, intent):
        """给 WebUI 的"指令测试"用：同步版本，只判断归属，不执行"""
        intent = (intent or "").strip()
        plan = self._custom(intent)
        source, reply, note = None, "", ""
        if plan:
            source, reply, note = plan.source, plan.reply, plan.note
        else:
            # 内置匹配是 async 的（可能要读音量），这里只做形态判断
            hit = self._builtin_match_only(intent)
            if hit:
                source, reply, note = "local", hit[1], hit[0]
            else:
                p = self._to_openclaw(intent, dry=True)
                source, reply, note = p.source, p.reply, p.note
        return {"intent": intent, "source": source, "reply": reply, "note": note}

    # ---------- 1. 自定义指令 ----------
    def _custom(self, intent):
        for c in self.store.list():
            if not c.get("enabled", True):
                continue
            if not self._match(c, intent):
                continue
            c["hit"] = int(c.get("hit", 0)) + 1
            try:
                self.store.save()
            except Exception:
                pass
            return self._build(c, intent)
        return None

    @staticmethod
    def _match(cmd, intent):
        how = cmd.get("match", "contains")
        core = _core(intent)
        for p in cmd["patterns"]:
            p_core = _core(p)
            if how == "exact" and (intent == p or core == p or core == p_core):
                return True
            if how == "startswith" and (intent.startswith(p)
                                        or core.startswith(p_core)):
                return True
            if how == "contains" and (p in intent or p_core in core):
                return True
            if how == "regex":
                try:
                    if re.search(p, intent):
                        return True
                except re.error:
                    continue
        return False

    def _build(self, cmd, intent):
        a = cmd.get("action") or {}
        atype = a.get("type", "play")
        name = cmd.get("name") or cmd["patterns"][0]
        reply = cmd.get("reply") or ""

        if atype == "openclaw":
            return Plan(False, source=f"custom:{cmd['id']}",
                        note=f"自定义指令「{name}」显式转给 OpenClaw",
                        detail={"command": name})

        if atype == "play":
            lim = a.get("limit")
            return self._plan_play(a.get("query", ""), a.get("mode", "shuffle"),
                                   int(lim) if lim is not None else 30, reply,
                                   f"custom:{cmd['id']}", name)

        if atype == "play_folder":
            # limit 留空 / 0 = 整个文件夹（不截断）
            lim = a.get("limit")
            return self._plan_play(a.get("query", ""), a.get("mode", "seq"),
                                   int(lim) if lim is not None else 0, reply,
                                   f"custom:{cmd['id']}", name, scope=a.get("path"))

        if atype == "play_track":
            t = self.bridge.library.get(a.get("id", ""))
            if not t:
                return Plan(True, "这首歌的索引没了，重新扫描一下曲库吧",
                            source=f"custom:{cmd['id']}")
            return Plan(True, reply or f"放{t.name}",
                        lambda: self.bridge.player.start_tracks([t], "one"),
                        source=f"custom:{cmd['id']}", note=f"固定曲目 {name}",
                        owns_playback=True)

        if atype == "volume":
            return self._plan_volume(a.get("value"), a.get("delta"), reply,
                                     f"custom:{cmd['id']}", name)

        if atype == "mode":
            return self._plan_mode(a.get("mode", "seq"), reply,
                                   f"custom:{cmd['id']}", name)

        if atype == "say":
            return Plan(True, reply or _fill(a.get("text", ""), query=intent),
                        source=f"custom:{cmd['id']}")

        simple = {"pause": ("pause", "好"), "resume": ("resume", "接着放"),
                  "next": ("next", "下一首"), "prev": ("prev", "上一首"),
                  "stop": ("stop", "停了")}
        if atype in simple:
            fn, word = simple[atype]
            # next()/prev() 不收 reason，pause()/resume()/stop() 收；
            # 统一传参会 TypeError（2026-10-31 21:34「播放下一首本地歌曲」就是这么挂的）
            if atype in ("pause", "resume", "stop"):
                action = lambda fn=fn: getattr(self.bridge.player, fn)(f"指令：{name}")
            else:
                action = lambda fn=fn: getattr(self.bridge.player, fn)()
            return Plan(True, reply or word, action,
                        source=f"custom:{cmd['id']}", note=name,
                        owns_playback=True)

        if atype == "status":
            return Plan(True, "", self._status_reply,
                        source=f"custom:{cmd['id']}", note=name)

        if atype == "rescan":
            return Plan(True, reply or "正在重新扫曲库",
                        self.bridge.scan_library, source=f"custom:{cmd['id']}")

        return Plan(True, "这个指令的动作还没实现", source=f"custom:{cmd['id']}")

    # ---------- 2. 内置指令 ----------
    def _builtin_match_only(self, intent):
        """预览用的形态判断（不查音量）"""
        for rx, tag in ((RE_PAUSE, "pause"), (RE_RESUME, "resume"),
                        (RE_NEXT, "next"), (RE_PREV, "prev"), (RE_STOP, "stop")):
            if rx.search(intent):
                return tag, {"pause": "先停一下", "resume": "接着放",
                             "next": "换下一首", "prev": "回上一首",
                             "stop": "好，停了"}[tag]
        if RE_VOL_SET.search(intent):
            return "volume", "调音量"
        if RE_VOL_UP.search(intent) or RE_VOL_DOWN.search(intent):
            return "volume", "调音量"
        if RE_MODE_SEQ.search(intent) or RE_MODE_SHUFFLE.search(intent) or RE_MODE_LOOP.search(intent):
            return "mode", "切换播放模式"
        if RE_STATUS.search(intent):
            return "status", ""
        if RE_RESCAN.search(intent):
            return "rescan", "重新扫曲库"
        m = RE_PLAY.match(intent)
        if m:
            q = TRIM_TAIL.sub("", TRIM_HEAD.sub("", m.group(2).strip()))
            return "play", f"播放「{q}」" if q else "播放"
        return None

    async def _builtin(self, intent):
        if RE_PAUSE.search(intent):
            return Plan(True, "先停一下", lambda: self.bridge.player.pause("语音暂停"),
                        owns_playback=True)
        if RE_RESUME.search(intent):
            return Plan(True, "接着放", lambda: self.bridge.player.resume("语音恢复"),
                        owns_playback=True)
        if RE_NEXT.search(intent):
            return Plan(True, "换下一首", lambda: self.bridge.player.next(),
                        owns_playback=True)
        if RE_PREV.search(intent):
            return Plan(True, "回上一首", lambda: self.bridge.player.prev(),
                        owns_playback=True)
        if RE_STOP.search(intent):
            return Plan(True, "好，停了", lambda: self.bridge.player.stop("语音停止"),
                        owns_playback=True)

        m = RE_VOL_SET.search(intent)
        if m:
            return self._plan_volume(int(m.group(1)), None, "", "local", "音量")
        if RE_VOL_UP.search(intent):
            return self._plan_volume(None, 10, "", "local", "调大")
        if RE_VOL_DOWN.search(intent):
            return self._plan_volume(None, -10, "", "local", "调小")

        if RE_MODE_SEQ.search(intent):
            return self._plan_mode("seq", "", "local", "顺序播放")
        if RE_MODE_SHUFFLE.search(intent):
            return self._plan_mode("shuffle", "", "local", "随机播放")
        if RE_MODE_LOOP.search(intent):
            return self._plan_mode("loop", "", "local", "循环播放")

        if RE_STATUS.search(intent):
            return Plan(True, "", self._status_reply)
        if RE_RESCAN.search(intent):
            return Plan(True, "正在重新扫曲库", self.bridge.scan_library)

        m = RE_PLAY.match(intent)
        if m:
            q = TRIM_TAIL.sub("", TRIM_HEAD.sub("", m.group(2).strip()))
            mode = self.bridge.player.mode or "shuffle"
            if mode not in ("seq", "shuffle", "loop"):
                mode = "shuffle"
            if not q:
                return Plan(True, "想听什么？可以说歌手名或者直接说随便放点",
                            source="local", note="缺查询词")
            return self._plan_play(q, mode, 30, "", "local", "播放")
        return None

    # ---------- 计划构造 ----------
    def _plan_play(self, query, mode, limit, reply, source, note, scope=None):
        """limit<=0 = 不限（整个文件夹 / 整个曲库）"""
        lib = self.bridge.library
        limit = int(limit or 0)
        if scope:
            tracks = lib.search_in(scope, query, limit,
                                   shuffle=(mode == "shuffle"))
        elif query:
            tracks = lib.pick(query, mode, limit)
            if not tracks:
                # 找不到就重扫一次再试：agent 刚下载的歌还没进索引，
                # 不重扫的话「播放刚下载的」永远失败。
                try:
                    n = lib.scan(True)
                    log.info("曲库重扫（%d 首）后重试：%s", n, query)
                    tracks = lib.pick(query, mode, limit)
                except Exception:
                    log.exception("重扫曲库失败")
        else:
            tracks = lib.random_pick(limit)
        if not tracks:
            tip = (f"这个文件夹里没有歌" if scope and query
                   else f"本地没找到「{query}」")
            return Plan(True, tip, source=source, note=note,
                        detail={"query": query, "scope": scope})
        first = str(tracks[0])
        count = len(tracks)
        mode_name = MODE_NAMES.get(mode, "随机")
        text = reply or (f"放上了，{first}，一共{count}首" if count > 1
                         else f"放上了，{first}")
        text = _fill(text, now=first, count=count, query=query,
                     mode=mode_name)
        return Plan(True, text,
                    lambda: self.bridge.player.start_tracks(list(tracks), mode),
                    source=source, note=note, owns_playback=True,
                    detail={"query": query, "count": count, "mode": mode,
                            "scope": scope, "first": first})

    def _plan_volume(self, value, delta, reply, source, note):
        async def run():
            cur = None
            if delta is not None:
                st = await self.bridge.speaker.get_state(ttl=0)
                cur = st.get("volume")
                if cur is None:
                    cur = 30
            target = value if value is not None else max(0, min(100, int(cur) + delta))
            return await self.bridge.speaker.set_volume(target)

        target = value
        if delta is not None:
            target = f"{'+' if delta > 0 else ''}{delta}"
        return Plan(True, reply or (f"音量{target}" if value is not None else "调一下音量"),
                    run, source=source, note=note,
                    detail={"volume": value, "delta": delta})

    def _plan_mode(self, mode, reply, source="local", note=""):
        async def run():
            await self.bridge.player.set_mode(mode)
            return {"ok": True, "mode": mode}

        name = MODE_NAMES.get(mode, mode)
        return Plan(True, reply or f"改成{name}播放", run, source=source,
                    note=note, detail={"mode": mode})

    async def _status_reply(self):
        p = self.bridge.player.status()
        if not p.get("playing"):
            return {"ok": True, "spoken": "现在没在放"}
        spoken = f"正在放{p['artist']}的{p['title']}，第{p['index']}首，共{p['total']}首"
        return {"ok": True, "spoken": spoken}

    # ---------- 3. OpenClaw ----------
    def _to_openclaw(self, intent, dry=False):
        router_cfg = self.cfg.router or {}
        mode = router_cfg.get("openclaw_mode", "fallback")   # fallback|keyword_only|off
        kws = [k.lower() for k in (router_cfg.get("openclaw_keywords")
                                   or DEFAULT_OPENCLAW_KEYWORDS)]
        hit_kw = next((k for k in kws if k in intent.lower()), None)

        # 后台被关掉（或在 WebUI 里没启用）时，直接给本地兜底，不去等超时
        if not getattr(self.bridge.agent, "enabled", False):
            self.stats["fallback"] += 1
            return Plan(True, self._local_only_tip(), source="fallback",
                        note="OpenClaw 未启用")

        if mode == "off":
            self.stats["fallback"] += 1
            return Plan(True, self._local_only_tip(), source="fallback",
                        note="路由器设置为不调用 OpenClaw")
        if mode == "keyword_only" and not hit_kw:
            self.stats["fallback"] += 1
            return Plan(True, self._local_only_tip(), source="fallback",
                        note="只把 NAS 相关问题交给 OpenClaw")

        self.stats["openclaw"] += 1
        if dry:
            note = ""
        elif getattr(self, "_wake_all", False):
            note = "命中 OpenClaw 触发词，整句交给它"
        elif hit_kw:
            note = f"交给 OpenClaw（关键词：{hit_kw}）"
        else:
            note = "本地无法处理，交给 OpenClaw 兜底"
        return Plan(False, source="openclaw", note=note)

    def _local_only_tip(self):
        wakes = [str(w).strip() for w in ((self.cfg.router or {}).get("openclaw_wakes") or [])
                 if str(w).strip()]
        if wakes:
            return (f"这个本地没有对应的固定指令。"
                    f"要用到后台，说「小爱同学，{wakes[0]}……」")
        return "这个本地没有对应的固定指令。要用到后台，先在网页里配一个 OpenClaw 触发词。"
