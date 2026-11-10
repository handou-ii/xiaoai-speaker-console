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

_last_rescan = 0.0   # 上次强制重扫曲库的时间（节流用）

# 本地关键词只撞到这么少几首、又不是精确歌名/歌手/目录时，判为「弱匹配」，
# 交给模型复核。用命中数量当信号，而不是维护一张流派词表——新词、怪词
# （后摇、city pop、蒸汽波）都能落到这条规则里，不需要改代码。
WEAK_HITS = 5

# AI 模型通道（直连 OpenAI 兼容端点 + 播放域工具）
LLM_BUDGET = 28.0        # 一次指令最多耗多久，超了就回落 OpenClaw
LLM_MAX_ROUNDS = 3       # 最多几轮工具调用（搜一次 → 看目录 → 播，三轮封顶）
LLM_CALL_TIMEOUT = 25.0  # 单轮模型往返上限（端点波动大，实测有 >20s）
# 这些工具会真的改变播放状态；只有它们的成功才算"办完了"
ACTION_TOOLS = {"play_search", "play_folder", "play_track", "pick_by_vibe",
                "search_by_tags", "similar_to",
                "control", "set_mode", "set_volume", "speak"}
LLM_SYSTEM = (
    "你是小爱音箱的播放助手。只通过工具操作音箱。\n"
    "1. 用户说歌名或歌手 → search_library 确认，再 play_track / play_search。\n"
    "2. 用户说的是感觉/场景/风格（安静、适合睡前、想听伤感的、跑步听的）→ 优先用 search_by_tags，"
    "把原话翻译成画像取值（例：安静→情绪[平静,温柔] 节奏慢 能量低；适合睡前→场景[睡前] 能量低；"
    "想听燃的→情绪[热血] 能量高），同时把用户原话填进 vibe（精排要用）；"
    "一个都命中不了再用 pick_by_vibe。"
    "绝对不要拿这些词去 play_search 撞歌名。\n"
    "2.1 用户说「再来点像刚才那首的」→ 用 similar_to。\n"
    "2.2 用户说的是「某代人喜欢听的」「八零年代的人」「80 后」「老一辈」这类——"
    "注意这是指人群的口味，不是发行年代：先想 3–6 个那个群体常听的歌手，"
    "用 resolve_names 一次验证；命中的歌手用 play_search 播，"
    "并把你的理解填进 note（例如「80 后常听的华语经典」），这句会念给用户听。"
    "一个都没命中，再按目录或流派找最接近的，或者直说没有。\n"
    "3. 用户说流派（电音、民谣、纯音乐、摇滚）→ list_genres 确认真实流派名，"
    "再 play_search 传 genre。\n"
    "4. 用户说随便放点、来点歌 → play_search，query 留空。\n"
    "5. 搜不到或结果 weak=true → 换一种方式（目录、流派、pick_by_vibe）再试一次；"
    "仍然没有就用 speak 直说没有，不要编歌名，也不要拿别的类别凑数。\n"
    "6. 曲库只有曲名、歌手、专辑、目录、流派，没有情绪标签——"
    "所以氛围要靠你自己从曲名和流派判断。"
)

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
# 句尾的模式词：「三巨头早年 随机播放」这种把点播和模式揉在一起的说法，
# 切掉尾巴后剩下的当查询词；命中就直接按这个模式放，不要只切模式不放歌。
RE_MODE_TAIL = re.compile(r"(随机播放|随机放|随便放|打乱|顺序播放|顺序放|"
                          r"循环播放|单曲循环|循环放)\s*$")
# 查询词尾部常被 ASR 带上的类别词
RE_QUERY_TAIL = re.compile(r"(文件|歌曲|首歌|音乐|的歌)+$")
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

# 当查询词用的最短长度：一个字的词进曲库搜基本是噪音
_MIN_QUERY = 2


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


def _reply_from_tool(name, out):
    """按工具结果拼一句播报，不额外请求模型"""
    if not isinstance(out, dict):
        return "好"
    if out.get("spoken"):
        return str(out["spoken"])
    if not out.get("ok"):
        err = str(out.get("error") or "")
        # 内部错误不直接念给用户：工具返回的技术性提示（缺参数、字段不存在）
        # 念出来像机器故障，听感很差。只有已经是人话的那几种才原样播。
        human = ("曲库里没有", "没找到", "没有这一类", "目录不在曲库")
        if err and any(err.startswith(k) or k in err for k in human):
            return err
        log.info("工具失败话术已脱敏：%s", err[:80])
        return "这句我没听明白，再说一遍？"
    if name in ("play_search", "play_folder", "pick_by_vibe", "search_by_tags"):
        # 模型如果交代了它的理解（「80后常听的华语经典」），先念出来——
        # 猜错了用户能立刻纠正，而不是听完一片不知道哪来的歌
        # 只有模型确实做了推断（说明超过几个字）才念，避免「按安静的、节奏舒缓的歌」这种废话
        und = str(out.get("understanding") or "").strip()
        head = ("按%s，" % und) if len(und) >= 4 else ""
        return "%s放上了，%s，一共%s首" % (head, out.get("first") or "", out.get("count"))
    if name == "similar_to":
        return "找了 %s 首像《%s》的，先放%s" % (
            out.get("count"), out.get("based_on") or "", out.get("first") or "")
    if name == "play_track":
        return "放上了，%s" % (out.get("first") or "")
    if name == "control":
        return str(out.get("done") or "好")
    if name == "set_mode":
        return "改成%s播放" % out.get("mode", "")
    if name == "set_volume":
        return "音量 %s" % out.get("volume")
    if name == "search_library":
        return "找到 %s 首" % out.get("count", 0)
    if name == "list_folders":
        return "曲库里有 %s 个目录" % out.get("count", 0)
    if name == "list_artists":
        return "曲库里有 %s 位歌手" % out.get("count", 0)
    return "好"


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
        self.stats = {"local": 0, "custom": 0, "openclaw": 0, "fallback": 0, "llm": 0}
        self._wake_all = False

    # ---------- 对外 ----------
    async def plan(self, intent, wake_hit=True, wake_scope=None, wake_name=None):
        """wake_scope: local = 本地唤醒词（只在本机做）；openclaw = 触发词（整句给 agent）"""
        intent = (intent or "").strip()
        if not intent:
            # 只说唤醒词、没有具体内容（「播放本地歌曲」）：按「随便放点」处理。
            # 以前这里回一句「在呢，想听点什么？」，用户会以为它没听懂。
            if wake_scope != "openclaw":
                _m = self.bridge.player.mode
                if _m not in ("seq", "shuffle", "loop"):
                    _m = "shuffle"
                p = self._plan_play("", _m, 20, "", "local", "播放")
                if p and (p.detail or {}).get("first"):
                    self.stats["local"] += 1
                    return p
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

        # 模型优先：命令词（暂停/切歌/音量/模式）直通，其余「播放什么」全交模型
        if self._llm_first():
            plan = await self._builtin(intent, play=False)
            if plan:
                self.stats["local"] += 1
                return plan
            # 剥掉「一些 / 来点 / 歌曲」后什么都不剩：这句话没有可理解的对象，
            # 直接随机放，没必要让模型跑一轮（7 秒变 0.03 秒）。
            # 注意这是结构判断（剥完是否为空），不是「句子里有没有某个词」。
            _bare = RE_QUERY_TAIL.sub(
                "", TRIM_TAIL.sub("", TRIM_HEAD.sub("", intent))).strip()
            # 「播放一些本地歌曲」剥完会剩下「播放一些本地」，里面没有可检索的对象。
            # 这里只是词形剥离（动词、量词、指代本地库的词），不涉及任何语义类别：
            # 全部剔掉后为空 = 这句话没有具体对象 = 随便放点。
            _bare = re.sub(
                r"(随便|随意|都行|什么都行|啥都行|一些|一点|点儿|几首|首歌"
                r"|歌曲|音乐|本地|纳斯|局域网|播放|来点|想听|要听"
                r"|首|点|个|些|的|歌)", "", _bare).strip()
            _bare = re.sub(r"^(放|来|听|要)+", "", _bare).strip()
            if not _bare:
                _m = self.bridge.player.mode
                if _m not in ("seq", "shuffle", "loop"):
                    _m = "shuffle"
                p = self._plan_play("", _m, 20, "", "local", "播放")
                if p and (p.detail or {}).get("first"):
                    self.stats["local"] += 1
                    return p
            plan = await self._to_llm(intent)
            if plan:
                return plan
            return self._llm_fallback(intent)

        # ---------- 没有配模型：纯正则 + 关键词检索（不转 agent）----------
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
            _q = RE_QUERY_TAIL.sub("", TRIM_TAIL.sub("", TRIM_HEAD.sub("", intent))).strip()
            if not _q:
                p = self._plan_play("", _mode, 20, "", "local", "播放")
                if p and (p.detail or {}).get("first"):
                    self.stats["local"] += 1
                    return p
            folder = self._folder_match(_q or intent)
            if folder:
                p = self._plan_play("", _mode, 30, "", "local", "播放", scope=folder)
                if p and (p.detail or {}).get("first"):
                    self.stats["local"] += 1
                    return p
            p = self._plan_play(_q or intent, _mode, 30, "", "local", "播放")
            if p and (p.detail or {}).get("first"):
                self.stats["local"] += 1
                return p
        # 没配模型时的兜底（不转 OpenClaw）：
        # 短查询（像人名/歌名）就如实说没有；整句话（像"想听点有气氛的"）
        # 就随机放一批并说明，比只回一句"没有对应的固定指令"有用。
        _m = self.bridge.player.mode
        if _m not in ("seq", "shuffle", "loop"):
            _m = "shuffle"
        _qraw = RE_QUERY_TAIL.sub(
            "", TRIM_TAIL.sub("", TRIM_HEAD.sub("", intent or ""))).strip()
        # 判空用「剥干净」的版本；说"没有 X"时用 _qraw，免得把歌名/人名剥坏
        # （「海来阿木」里的"来"不能被当成动词剥掉）
        _qbare = re.sub(
            r"(随便|随意|都行|什么都行|啥都行|一些|一点|点儿|几首|首歌"
            r"|歌曲|音乐|本地|纳斯|局域网|播放|来点|想听|要听"
            r"|首|点|个|些|的|歌)", "", _qraw).strip()
        _qbare = re.sub(r"^(放|来|听|要)+", "", _qbare).strip()
        if not _qbare:
            # 剥干净后什么都不剩（随便放点 / 播放一些歌曲）：就是让我放歌，
            # 正常播报即可，别加"没找到"这种丧气话。
            p = self._plan_play("", _m, 20, "", "local", "随便放点")
            if p and (p.detail or {}).get("first"):
                self.stats["local"] += 1
                return p
        if _qbare and len(_qbare) <= 6:
            return Plan(True, "曲库里没有「%s」" % _qbare, source="local",
                        note="没找到")
        p = self._plan_play("", _m, 20,
                            "没找到对得上的，先随便放点", "local", "兜底随机")
        if p and (p.detail or {}).get("first"):
            self.stats["local"] += 1
            return p
        return Plan(True, self._local_only_tip(), source="local",
                    note="本地唤醒词：没有对应的固定指令")

    def _llm_fallback(self, intent):
        """模型没给出结论时的兜底：随机放，或直说没听清

        这里**不回到关键词检索**：模型失败时拿原句去撞歌名，会播出一批
        「字面上像、意思上不对」的歌（「安静的歌曲」→ 歌名带安静的那几首），
        用户会以为模型理解错了。宁可随机，也不给一个看起来对的结果。
        """
        try:
            p = self._plan_play("", self.bridge.player.mode or "shuffle", 20,
                                "这次没想好，先随机放 20 首", "llm", "模型无结论")
            if p and (p.detail or {}).get("first"):
                return p
        except Exception:
            log.exception("模型兜底随机播放失败")
        return Plan(True, "这句我没听明白，再说一遍？", source="llm",
                    note="模型无结论")

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
            q = str(a.get("query") or "").strip()
            scope = str(a.get("path") or "").strip()
            # 常见误填：把目录写进了「搜索词」。歌名不可能以 / 开头，
            # 见到就当文件夹用，否则会拿整条路径去匹配歌名、必然找不到。
            if not scope and q.startswith("/"):
                q, scope = "", q
            return self._plan_play(q, a.get("mode", "seq"),
                                   int(lim) if lim is not None else 0, reply,
                                   f"custom:{cmd['id']}", name, scope=scope)

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
        if RE_MODE_SEQ.search(intent) or RE_MODE_SHUFFLE.search(intent) \
                or RE_MODE_LOOP.search(intent):
            # 「随便放点」「随机放几首歌」本质是「放歌」而不是「切播放模式」，
            # 真正想切模式时说的是「改成顺序播放」这类，句子里不会有 点/歌/曲/首。
            if not re.search(r"(点|歌|曲|首|听)", intent):
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

    async def _builtin(self, intent, play=True):
        """play=False 时只处理命令词（暂停/切歌/音量/模式/状态），不判"播放什么" """
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

        # 「歌名 + 随机播放」一口气说完（「播放本地三巨头早年歌曲文件随机播放」）：
        # 先把尾部的模式词切掉，剩下的当查询词，本地搜到就按这个模式直接放。
        # 只切模式不放歌是错的——用户明显是想边放边随机。
        mm = RE_MODE_TAIL.search(intent)
        if mm and mm.start() >= 2:
            q = TRIM_TAIL.sub("", TRIM_HEAD.sub("", intent[:mm.start()]))
            q = RE_QUERY_TAIL.sub("", q).strip()
            if len(q) >= _MIN_QUERY:
                w = mm.group(1)
                mode = ("shuffle" if ("随机" in w or "打乱" in w)
                        else "loop" if "循环" in w else "seq")
                p = self._plan_play(q, mode, 30, "", "local", "播放")
                if (p.detail or {}).get("first"):
                    return p

        # 「随便放点」「随机放几首歌」是让我放歌，不是切换播放模式；
        # 真正要切模式说的是「改成顺序播放」这种，句中不会有点/歌/曲/首/听。
        if not re.search(r"(点|歌|曲|首|听)", intent):
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

        if not play:
            return None              # 「播放什么」交给模型，命令词到此为止
        m = RE_PLAY.match(intent)
        if m:
            q = TRIM_TAIL.sub("", TRIM_HEAD.sub("", m.group(2).strip()))
            if q and self._weak_local(q, self.bridge.library.pick(q, "shuffle", 30)):
                return None          # 命中太弱，交给模型复核
            mode = self.bridge.player.mode or "shuffle"
            if mode not in ("seq", "shuffle", "loop"):
                mode = "shuffle"
            if not q:
                # 没有对象就别反问了（这句多半就是「播放一些歌曲」）：
                # 直接随机放 20 首，比问一句再等一轮更省事。
                p = self._plan_play("", mode, 20, "", "local", "播放")
                if p and (p.detail or {}).get("first"):
                    return p
                return Plan(True, "想听什么？可以说歌手名或者直接说随便放点",
                            source="local", note="缺查询词")
            return self._plan_play(q, mode, 30, "", "local", "播放")
        return None

    # ---------- 计划构造 ----------
    def _llm_first(self):
        """模型优先：意图判断全交模型，代码只保留固定指令与命令词

        配置 llm.first=false 可回到「规则先判、模型兜底」的老行为。
        """
        llm = getattr(self.bridge, "llm", None)
        if not llm or not getattr(llm, "enabled", False) or not llm.configured:
            return False
        c = self.bridge.cfg.get("llm") or {}
        return c.get("first", True) is not False

    def _catalog_hint(self):
        """把曲库概况压成一段提示，省掉模型先查目录/流派那一轮"""
        now = time.time()
        if getattr(self, "_cat_at", 0) and now - self._cat_at < 300:
            return self._cat
        lib = self.bridge.library
        try:
            lib.scan()
            gs = lib.genres()[:10]
            dirs = []
            seen = set()
            for t in lib.tracks:
                d = os.path.basename(os.path.dirname(t.path))
                if d and d not in seen:
                    seen.add(d)
                    dirs.append(d)
                if len(dirs) >= 18:
                    break
            arts = {}
            for t in lib.tracks:
                if t.artist:
                    arts[t.artist] = arts.get(t.artist, 0) + 1
            top = sorted(arts.items(), key=lambda kv: -kv[1])[:15]
            self._cat = ("曲库概况（共 %d 首）：\n流派：%s\n目录：%s\n歌手：%s" % (
                len(lib.tracks),
                "、".join("%s%d首" % (g, c) for g, c in gs) or "（无标签）",
                "、".join(dirs),
                "、".join("%s%d首" % (a, c) for a, c in top)))
        except Exception:
            self._cat = ""
        self._cat_at = now
        return self._cat

    def _folder_match(self, q):
        """q 是不是某个目录名 → 返回该目录；「民谣」「纯音乐」这类直接按目录播更准"""
        q = (q or "").strip().lower()
        if len(q) < 2:
            return ""
        try:
            for t in self.bridge.library.tracks:
                d = os.path.dirname(t.path)
                if os.path.basename(d).lower() == q:
                    return d
        except Exception:
            pass
        return ""

    def _weak_local(self, q, tracks):
        """本地关键词匹配太弱 → 让模型复核

        判据（全部满足才算弱）：
          * 只命中很少几首（< WEAK_HITS）
          * q 不是某首歌的名字、也不是某个歌手名
          * q 没有出现在任何目录名里
        精确点歌、按歌手放、按目录放都不受影响。
        """
        q = (q or "").strip()
        if not q or len(tracks) >= WEAK_HITS:
            return False
        try:
            for t in self.bridge.library.tracks:
                if (t.name or "").strip() == q or (t.artist or "").strip() == q:
                    return False
                if q in os.path.dirname(t.path):
                    return False
        except Exception:
            pass
        return True

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
                # 找不到就重扫一次再试：agent 刚下载的歌还没进索引。
                # 但要节流——每条失败指令都扫全库是白费 IO（60 秒内只扫一次）。
                global _last_rescan
                if time.time() - _last_rescan > 60:
                    _last_rescan = time.time()
                    # 先看目录有没有变化：曲库没动时重扫要 13–110 秒，纯浪费。
                    # 只有文件数或最近修改时间变了才真的重建索引。
                    snap = lib.snapshot()
                    prev = getattr(self, "_lib_snap", None)
                    if prev is None:
                        # 第一次只记下来，不扫：刚启动时索引本来就是新的
                        self._lib_snap = snap
                    elif snap != prev:
                        self._lib_snap = snap
                        try:
                            n = lib.scan(True)
                            log.info("曲库有变化，重扫（%d 首）后重试：%s", n, query)
                            tracks = lib.pick(query, mode, limit)
                        except Exception:
                            log.exception("重扫曲库失败")
                    else:
                        log.info("曲库无变化，跳过重扫：%s", query)
        else:
            tracks = lib.random_pick(limit)
        if not tracks:
            tip = (f"这个文件夹里没有歌" if scope and query
                   else f"本地没找到「{query}」")
            return Plan(True, tip, source=source, note=note,
                        detail={"query": query, "scope": scope})
        first = getattr(tracks[0], "pretty", None) or str(tracks[0])
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

    # ---------- 2.5 直连模型（带播放工具） ----------
    async def _to_llm(self, intent):
        """交给直连模型 + 播放域工具。返回 None = 它没给出结论，调用方回落

        分工：模型只负责「选哪个工具、填什么参数」，话术由本地按工具结果拼，
        因此成功路径只有一轮模型往返（约 5 秒），不会为了寒暄多等一次。
        参数不合法会在 llmtools 里被拒，模型拿不到文件 / 系统 / 网络能力。
        """
        llm = getattr(self.bridge, "llm", None)
        if not llm or not getattr(llm, "enabled", False) or not llm.configured:
            return None
        from .llmtools import TOOLS, run_tool, dumps

        cat = self._catalog_hint()
        msgs = [{"role": "system",
                 "content": LLM_SYSTEM + ("\n" + cat if cat else "")},
                {"role": "user", "content": intent}]
        spoken = ""
        called = False
        last = None                      # 最近一次工具结果 (name, out)
        last_action = None               # 最近一次「动作类」结果，收尾优先用它
        deadline = time.time() + LLM_BUDGET
        for _ in range(LLM_MAX_ROUNDS):
            left = deadline - time.time()
            if left <= 1.0:
                break
            r = await llm.chat_tools(msgs, TOOLS, timeout=min(left, LLM_CALL_TIMEOUT))
            if not r.get("ok"):
                # 工具已经执行过就别回落：否则同一个动作会被 OpenClaw 再做一遍
                if called:
                    log.warning("llm 第二轮失败（%s），直接用工具结果收尾", r.get("error"))
                    break
                log.warning("llm 调用失败（%s），回落到 OpenClaw", r.get("error"))
                return None
            msg = r.get("message") or {}
            calls = msg.get("tool_calls") or []
            if not calls:
                text = (msg.get("content") or "").strip()
                if text:
                    spoken = text
                break
            called = True
            msgs.append({"role": "assistant",
                         "content": msg.get("content") or "",
                         "tool_calls": calls})
            for c in calls:
                fn = c.get("function") or {}
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                try:
                    out = await run_tool(name, args, self.bridge)
                except Exception as e:
                    log.exception("llm 工具执行失败：%s", name)
                    out = {"ok": False, "error": str(e)[:80]}
                log.info("llm 工具 %s → %s", name, dumps(out)[:120])
                last = (name, out)
                if name in ACTION_TOOLS:
                    last_action = (name, out)
                msgs.append({"role": "tool", "tool_call_id": c.get("id") or "",
                             "content": dumps(out)})
            if last and last[1].get("ok") and last[0] in ACTION_TOOLS:
                # 命中太弱（weak）不算办好：给模型再看一眼目录的机会
                if last[1].get("weak") and time.time() < deadline - 2.0:
                    msgs.append({"role": "user",
                                 "content": "结果 weak=true，先看看目录里有没有更合适的类别，"
                                            "没有就直说没有。"})
                    continue
                spoken = _reply_from_tool(*last)
                break
            # 查询类工具（列目录、列歌手、搜歌）只是给模型看的资料，
            # 成功也不能收尾，必须再给它一轮去决定播什么
            if last and time.time() < deadline - 2.0:
                continue
            if last:
                spoken = _reply_from_tool(*last)
                break
        if not called and not spoken:
            return None
        if not spoken:
            # 只查了目录/流派就没下文（比如模型超时），不能拿「曲库里有 55 个目录」
            # 当回答：要么用真播过的结果，要么让它重说
            if last_action:
                spoken = _reply_from_tool(*last_action)
            else:
                spoken = "这句我没听明白，再说一遍？"
        self.stats["llm"] = self.stats.get("llm", 0) + 1
        return Plan(True, spoken or "好", source="llm", note="AI 模型")

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
