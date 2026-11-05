"""唤醒词匹配

规则：必须出现唤醒词，命中后返回剥离唤醒词之后的"指令正文"。

唤醒词有两种写法（都在 config.json 的 wake 节点里）：
  * wake.names     —— 纯文字名字，如 "播放本地"。程序自动补上"让/叫/帮我/麻烦/请"
                      这类动词前缀，并容忍字间被 ASR 插空格；
  * wake.patterns  —— 正则，留给需要特殊表达的场景（老配置也走这里）。

设计要点：
  1. 指令正文为空或过短 → 判为未命中，避免句中偶然出现同字就被切走；
  2. 容忍"播放本地啊""本地纳斯了"这类 ASR 多字；
  3. 若装了 pypinyin，追加拼音同音容错，能吃下"bo fang ben di"这类写法。
"""
import logging
import re
import unicodedata

log = logging.getLogger("wake")

# 没有配置任何唤醒词时的兜底：本地口径的词，别用「小龙虾」——
# 那是 OpenClaw 触发词的语感，混在一起会让人以为叫了名字却没人接话。
VARIANTS = ["播放本地", "本地纳斯", "本地歌曲"]

# 名字前面可能出现的动词/语气词，命中后整段剥掉
VERB_PREFIX = r"(让|叫|帮我|麻烦|请|给|来)?\s*"

FILLERS = ("们", "了", "啊", "呀", "吧", "呢", "哈", "那", "然后", "帮我", "给我", "一下",
           "歌曲", "首歌", "音乐")   # 「播放本地歌曲 晴天」→「晴天」

# 设备自己的唤醒词，会一起进对话记录。没带自定义唤醒名时要先剥掉，
# 否则「完全一致」这类匹配永远对不上。
DEVICE_WAKE = re.compile(r"^(?:小爱同学|小爱小爱|小爱|小艾同学|小艾)+")

# 拼音兜底时预置的前缀音节
_PY_PREFIX = ("", "rang", "jiao", "bangwo", "mafan", "qing", "gei", "lai")

_MIN_INTENT = 2


def normalize(text):
    """全角转半角、去标点、压缩空白、转小写"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[\s\u3000]+", "", text)
    text = re.sub(r"[，。！？、,.!?~～…：:；;“”\"'（）()]", "", text)
    return text.lower()


def strip_device_wake(text):
    """剥掉开头的「小爱同学」，只用于没带自定义唤醒名时的兜底匹配"""
    return DEVICE_WAKE.sub("", text or "").strip()


def _strip_fillers(intent):
    changed = True
    while changed and intent:
        changed = False
        for f in FILLERS:
            if intent.startswith(f):
                intent = intent[len(f):]
                changed = True
    return intent


def name_to_regex(name):
    """把「播放本地」变成可匹配「让播放本地」「叫 播放本地」的正则"""
    chars = [c for c in str(name or "") if not c.isspace()]
    if not chars:
        return ""
    body = r"\s*".join(re.escape(c) for c in chars)
    return VERB_PREFIX + body


_PREFIX_RE = re.compile(
    r"^\^?(?:\((?:让|叫|帮我|麻烦|请|给|来)(?:\|(?:让|叫|帮我|麻烦|请|给|来))*\)\?\\s\*)?")


def names_from_patterns(patterns):
    """老配置迁移：尽可能从正则里反推出纯文字的名字

    只有「动词前缀 + 纯文字」这种简单正则能还原，其它（含分组/字符类的）
    还原不了就丢掉，仍留在 patterns 里继续生效。
    """
    out = []
    for p in patterns or []:
        rest = p
        rest = _PREFIX_RE.sub("", rest)
        rest = re.sub(r"^\\s\*", "", rest)
        rest = re.sub(r"\\s\*", "", rest)
        rest = re.sub(r"\\(.)", r"\1", rest)   # 反转义 \. \-
        if rest and not re.search(r"[\[\](){}|?*+^$\\]", rest):
            if rest not in out:
                out.append(rest)
    return out


def wake_names(cfg):
    """当前生效的唤醒名字列表（老配置会自动迁移出名字）"""
    names = cfg.wake.get("names")
    if names is None:
        names = names_from_patterns(cfg.wake.get("patterns") or [])
    names = [str(n).strip() for n in (names or []) if str(n).strip()]
    return names or list(VARIANTS)


def openclaw_wakes(cfg):
    """OpenClaw 触发词：独立的一套，命中就整句交给 agent

    与本地唤醒词（wake.names）互不相干：本地那几个切完句子只用本地能力，
    这边的词切完直接把正文送 OpenClaw。
    """
    raw = (cfg.router or {}).get("openclaw_wakes")
    return [str(x).strip() for x in (raw or []) if str(x).strip()]


def wake_patterns(cfg):
    """额外的正则唤醒词（老配置里能被名字覆盖的部分不再重复编译）"""
    return [p for p in (cfg.wake.get("patterns") or []) if isinstance(p, str) and p.strip()]


class WakeMatcher:
    def __init__(self, cfg):
        self.names = wake_names(cfg)
        # 每条正则都记住它来自哪个名字：路由要按"命中的是哪个唤醒词"分流
        # （「小龙虾」= 走本地；「让小龙虾」= 交给 agent）
        self.agent_wakes = openclaw_wakes(cfg)
        # 触发词排前面：本地唤醒词会自动补「让/叫/帮我」这类前缀，若本地名是
        # 「小龙虾」，它同样能吃下「让小龙虾…」，排在前面就把触发词抢走了。
        # 同名时也以触发词为准，所以本地名单里要把这些同名字去掉。
        local_only = [n for n in self.names if n not in self.agent_wakes]
        # 长词优先：「播放本地歌曲」要排在「播放本地」前面，否则只剩「歌曲晴天」
        local_only.sort(key=len, reverse=True)
        wakes_sorted = sorted(self.agent_wakes, key=len, reverse=True)
        # (名字, 作用域, 正则)：local = 本机处理，openclaw = 整句转 agent
        named = [(w, "openclaw", name_to_regex(w)) for w in wakes_sorted]
        named += [(n, "local", name_to_regex(n)) for n in local_only]
        named += [(None, None, p) for p in wake_patterns(cfg)]
        self._pats = []
        for name, scope, p in named:
            try:
                self._pats.append((name, scope, re.compile(p)))
            except re.error as e:
                log.warning("唤醒词正则无效，已跳过：%r（%s）", p, e)
        self.patterns = [rx for _, _, rx in self._pats]
        self.last_name = None      # 最近一次命中的词
        self.last_scope = None     # local / openclaw / None

        # 拼音兜底用的 key，由名字生成（"播放本地" → bofangbendi）
        self._pinyin = None
        self._py_keys = []
        try:  # 可选增强，未安装则跳过
            from pypinyin import lazy_pinyin

            self._pinyin = lazy_pinyin
            self._py_map = {}
            # 触发词后遍历，同音 key 撞车时以触发词为准
            for n in local_only + self.agent_wakes:
                base = "".join(lazy_pinyin(n)).replace(" ", "")
                for pre in _PY_PREFIX:
                    self._py_keys.append(pre + base)
                    self._py_map[pre + base] = n
        except ImportError:
            pass
        log.info("唤醒词：%s", " / ".join(self.names))

    def match(self, text):
        """返回 (命中?, 指令正文)"""
        norm = normalize(text)
        if not norm:
            return False, ""

        self.last_name = None
        self.last_scope = None
        # 唤醒词必须出现在句首（前面可以有「小爱同学」这类设备唤醒词）。
        # 用 search 会在句子中间乱匹配：曾经「暂停播放本地歌曲」里的"播放本地"
        # 被当成唤醒词，句子被切成"歌曲"，直接跳过了用户配的整句指令。
        head = strip_device_wake(norm) or norm
        for name, scope, p in self._pats:
            m = p.match(head)
            if not m:
                continue
            intent = _strip_fillers(head[m.end():].strip())
            if len(intent) >= _MIN_INTENT:
                self.last_name = name
                self.last_scope = scope
                return True, intent

        # 拼音兜底：把前 12 个字符转拼音，找同音写法
        if self._pinyin and self._py_keys:
            char_py = self._pinyin(head[:12])   # lazy_pinyin 与汉字一一对应
            head = "".join(char_py)
            for key in self._py_keys:
                idx = head.find(key)
                if idx < 0:
                    continue
                # 拼音下标换算成汉字下标：累计长度
                cut, acc = 0, 0
                for ch_py in char_py:
                    acc += len(ch_py)
                    cut += 1
                    if acc >= idx + len(key):
                        break
                intent = _strip_fillers(head[cut:].strip())
                if len(intent) >= _MIN_INTENT:
                    hit = getattr(self, "_py_map", {}).get(key)
                    self.last_name = hit
                    self.last_scope = ("openclaw" if hit in self.agent_wakes
                                       else "local" if hit else None)
                    return True, intent
        return False, ""
