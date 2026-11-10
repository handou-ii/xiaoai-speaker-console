"""给大模型用的播放域工具

模型只负责「选哪个工具、填什么参数」，执行权在这里：每个参数都要校验，
路径必须在曲库根目录内、曲目 id 必须在索引里、音量夹在 0–100。
除了这些动作，不向模型暴露任何文件 / 系统 / 网络能力。
"""
import json
import logging
import os

from bridge.tagging import (MOODS, TEMPOS, VOCALS, SCENES, ENERGIES)

log = logging.getLogger("llmtools")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_library",
            "description": "在本地曲库里搜歌，返回匹配的歌曲摘要。不确定有没有这首歌时先调它。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "歌手、歌名或关键词"},
                    "limit": {"type": "integer", "description": "最多返回几条，默认 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_by_tags",
            "description": "按歌曲画像检索播放：情绪/节奏/人声/场景/能量。"
                           "用户说的是一类感觉（安静、适合睡前、想听伤感的、跑步听的）时优先用它。"
                           "情绪取值：平静/温柔/伤感/治愈/热血/欢快/忧郁/孤独/浪漫/神秘；"
                           "节奏：慢/中/快；人声：纯器乐/有歌词；"
                           "场景：睡前/工作/通勤/运动/雨天/独处/聚会；能量：低/中/高。"
                           "只能填这些值，不要自创。",
            "parameters": {
                "type": "object",
                "properties": {
                    "mood": {"type": "array", "items": {"type": "string"},
                             "description": "情绪，可多选，最多 2 个"},
                    "tempo": {"type": "string", "description": "节奏：慢/中/快"},
                    "vocal": {"type": "string", "description": "纯器乐/有歌词"},
                    "scene": {"type": "array", "items": {"type": "string"},
                              "description": "场景，可多选"},
                    "energy": {"type": "string", "description": "能量：低/中/高"},
                    "vibe": {"type": "string",
                             "description": "用户的原话，例如「安静」「适合睡前听」，用于在候选里精排"},
                    "note": {"type": "string",
                             "description": "只在需要推断时才填（人群、年代、隐喻说法，"
                                            "例如「80后常听的华语经典」）；"
                                            "用户直接说歌手/流派/情绪时不要填，避免废话"},
                    "limit": {"type": "integer", "description": "播几首，默认 20"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "similar_to",
            "description": "按「跟正在播的这首像」再挑一批：用户说再来点类似的、像刚才那首的、同一种感觉时用它。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "播几首，默认 20"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pick_by_vibe",
            "description": "按氛围/风格挑歌：先取一批候选，再由你从中选出最符合的。"
                           "用户说安静、节奏舒缓、适合睡前、有点燃这类感觉时用它，"
                           "不要用 play_search 拿这些词去撞歌名。",
            "parameters": {
                "type": "object",
                "properties": {
                    "vibe": {"type": "string", "description": "用户要的感觉，例如 安静、节奏缓慢、适合睡前"},
                    "genres": {"type": "array", "items": {"type": "string"},
                               "description": "限定流派，可留空；留空则全库取样"},
                    "limit": {"type": "integer", "description": "播几首，默认 15"},
                },
                "required": ["vibe"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_search",
            "description": "按关键词随机/顺序播放曲库里匹配的歌。用户说放某歌手、某首歌、随便放点时用它。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "歌手、歌名或关键词；按流派放时留空"},
                    "genre": {"type": "string", "description": "流派，例如 电子、民谣、纯音乐（先用 list_genres 确认）"},
                    "mode": {"type": "string", "enum": ["shuffle", "seq", "loop"],
                             "description": "shuffle 随机（默认）、seq 顺序、loop 单曲循环"},
                    "limit": {"type": "integer", "description": "最多放几首，默认 20，0 表示不限"},
                    "note": {"type": "string",
                             "description": "只在需要推断时才填（人群、年代、隐喻说法，"
                                            "例如「80后常听的华语经典」）；"
                                            "用户直接说歌手/流派/情绪时不要填，避免废话"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_names",
            "description": "一次验证多个歌手/艺名在库里有没有，各多少首。"
                           "用户说的是库里没有的维度（某年代的人、某地区、某榜单）时，"
                           "先把它们联想成 3–6 个代表歌手名，用它一次问清楚，"
                           "再把命中的歌手拿去播。",
            "parameters": {
                "type": "object",
                "properties": {
                    "names": {"type": "array", "items": {"type": "string"},
                              "description": "要验证的歌手名，最多 8 个"},
                },
                "required": ["names"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_genres",
            "description": "列出曲库的流派及歌数（来自文件标签，已归一成大类）。用户说某个流派时先调它确认真实名称。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_folders",
            "description": "列出曲库目录（带歌数）。查询词搜不到、或用户说的是一类歌（纯电音、适合睡觉的、外国歌）时先调它，看清有哪些目录再决定播哪个。",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string",
                               "description": "要展开的目录绝对路径；留空列曲库根目录下的一级目录"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_artists",
            "description": "列出歌手（带歌数），可按关键词过滤。不知道用户说的是不是某个歌手时用它确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "过滤关键词，可留空"},
                    "limit": {"type": "integer", "description": "最多几条，默认 20"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_folder",
            "description": "只播某个文件夹里的歌。path 必须是曲库根目录下的绝对路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件夹绝对路径，例如 /vol1/1000/音乐文件/周杰伦"},
                    "mode": {"type": "string", "enum": ["shuffle", "seq", "loop"]},
                    "limit": {"type": "integer", "description": "最多放几首，0 表示整个文件夹"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_track",
            "description": "播放指定的一首歌，id 来自 search_library 的结果。",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "control",
            "description": "播放控制：暂停、继续、下一首、上一首、停止。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["pause", "resume", "next", "prev", "stop"]},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_mode",
            "description": "切换播放模式，不影响正在播的歌。",
            "parameters": {
                "type": "object",
                "properties": {"mode": {"type": "string", "enum": ["shuffle", "seq", "loop"]}},
                "required": ["mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_volume",
            "description": "调节音箱音量。给 value 是设成固定值，给 delta 是相对增减。",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {"type": "integer", "description": "目标音量 0–100"},
                    "delta": {"type": "integer", "description": "相对变化，例如 +10 / -10"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "speak",
            "description": "什么都不做，只说一句话给用户（例如曲库里没有这首歌）。",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
]

# 命中数少于这个值就标记 weak=true，提示模型「可能不是用户要的那一类」。
# 用数量而不是词表：任何新词、怪词都会自然落到这条规则里。
WEAK_HITS = 5


MODE_NAMES = {"shuffle": "随机", "seq": "顺序", "loop": "循环"}


def _roots(bridge):
    roots = []
    for r in ((bridge.cfg.get("music") or {}).get("roots") or []):
        try:
            roots.append(os.path.realpath(str(r)))
        except OSError:
            continue
    return roots


def _under_roots(path, roots):
    try:
        p = os.path.realpath(path)
    except OSError:
        return False
    return any(p == r or p.startswith(r + os.sep) for r in roots)


def _clip(v, lo, hi, default):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _list_folders(bridge, prefix=""):
    """列 prefix 下的一层目录及歌数；prefix 为空时列每个曲库根目录的一级目录"""
    lib = bridge.library
    lib.scan()
    roots = [os.path.realpath(str(r)) for r in lib.roots]
    if prefix:
        base = os.path.realpath(prefix)
        if not _under_roots(base, roots):
            return {"ok": False, "error": "这个目录不在曲库范围内"}
        targets = [base]
    else:
        targets = roots
    out = []
    for base in targets:
        counts = {}
        for t in lib.tracks:
            p = os.path.realpath(t.path)
            if not p.startswith(base + os.sep):
                continue
            rel = os.path.relpath(p, base)
            parts = rel.split(os.sep)
            key = parts[0] if len(parts) > 1 else "(根目录散文件)"
            counts[key] = counts.get(key, 0) + 1
        items = sorted(counts.items(), key=lambda kv: -kv[1])[:30]
        for name, cnt in items:
            path = base if name == "(根目录散文件)" else os.path.join(base, name)
            out.append({"name": name, "count": cnt, "path": path})
    return {"ok": True, "count": len(out), "folders": out}


def _vibe_pool(lib, genres, size=40):
    """按流派取候选；没指定流派就全库随机取样"""
    pool = []
    for g in genres:
        pool.extend(lib.pick_genre(g, "shuffle", size))
        if len(pool) >= size:
            break
    if not pool:
        pool = lib.random_pick(size)
    # 去重后截断
    seen = set()
    out = []
    for t in pool:
        if t.tid in seen:
            continue
        seen.add(t.tid)
        out.append(t)
        if len(out) >= size:
            break
    return out


async def _rank_by_vibe(bridge, vibe, pool, limit):
    """把候选交给模型挑：只回序号，不生成新歌名，避免它编造"""
    lines = []
    for i, t in enumerate(pool, 1):
        g = ("/" + t.genre) if t.genre else ""
        lines.append("%d. %s - %s%s" % (i, t.artist or "未知", t.name, g))
    prompt = ("用户想听「%s」的歌。下面是本地曲库里的一批候选，"
              "请只挑选最符合这个感觉的，最多 %d 首。\n"
              "只回一个 JSON 数组，元素是序号，例如 [3,7,12]，不要任何解释。\n\n%s"
              % (vibe, limit, "\n".join(lines)))
    try:
        r = await bridge.llm.chat(
            [{"role": "system", "content": "你只做挑歌，只输出 JSON 数组。"},
             {"role": "user", "content": prompt}], timeout=12)
    except Exception:
        r = None
    picks = []
    if r and r.get("ok"):
        text = r.get("reply") or ""
        start, end = text.find("["), text.rfind("]")
        if start >= 0 and end > start:
            try:
                nums = json.loads(text[start:end + 1])
                for n in nums:
                    try:
                        idx = int(n) - 1
                    except (TypeError, ValueError):
                        continue
                    if 0 <= idx < len(pool):
                        picks.append(pool[idx])
            except Exception:
                picks = []
    if not picks:
        picks = pool[:limit]          # 模型没给出可用结果：按原顺序取前几首
    seen = set()
    out = []
    for t in picks:
        if t.tid in seen:
            continue
        seen.add(t.tid)
        out.append(t)
        if len(out) >= limit:
            break
    return out


def _want_text(want):
    """把画像条件拼成一句人话，给精排用（模型没有原话时用）"""
    parts = []
    if want.get("mood"):
        parts.append("情绪%s" % "/".join(want["mood"]))
    if want.get("scene"):
        parts.append("适合%s" % "/".join(want["scene"]))
    if want.get("tempo"):
        parts.append("节奏%s" % want["tempo"])
    if want.get("vocal"):
        parts.append(want["vocal"])
    if want.get("energy"):
        parts.append("能量%s" % want["energy"])
    return "、".join(parts) or "这类歌"


async def _rank_tagged(bridge, desire, pool, limit):
    """在候选里按用户原话精排：只回序号，不让它生成歌名

    返回排好序的曲目列表；模型失败或返回不可用时返回 None，
    调用方会退回「按原顺序取前几首」。
    """
    tg = getattr(bridge, "tagger", None)
    lines = []
    for i, t in enumerate(pool, 1):
        tgv = (tg.tags_of(t) if tg else None) or {}
        bits = []
        if tgv.get("mood"):
            bits.append("/".join(tgv["mood"]))
        if tgv.get("tempo"):
            bits.append("节奏%s" % tgv["tempo"])
        if tgv.get("vocal"):
            bits.append(tgv["vocal"])
        if tgv.get("energy"):
            bits.append("能量%s" % tgv["energy"])
        tag = ("（%s）" % "、".join(bits)) if bits else ""
        lines.append("%d. %s - %s%s" % (i, t.artist or "未知", t.name, tag))
    prompt = ("用户想听：%s\n"
              "下面是曲库里符合这个描述的候选（括号里是这首歌的画像标签）。\n"
              "请挑出最贴合的，最多 %d 首，按贴合度从高到低排列。\n"
              "明显不符合的（例如有人声的音乐剧、太吵的）直接跳过。\n"
              "只回 JSON 数组，元素是序号，例如 [3,7,12]，不要任何解释。\n\n%s"
              % (desire, limit, "\n".join(lines)))
    try:
        r = await bridge.llm.chat(
            [{"role": "system", "content": "你只做挑歌，只输出 JSON 数组。"},
             {"role": "user", "content": prompt}], timeout=15)
    except Exception:
        r = None
    if not (r and r.get("ok")):
        return None
    text = r.get("reply") or ""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        nums = json.loads(text[start:end + 1])
    except Exception:
        return None
    out, seen = [], set()
    for n in nums:
        try:
            idx = int(n) - 1
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(pool)):
            continue
        t = pool[idx]
        if t.tid in seen:
            continue
        seen.add(t.tid)
        out.append(t)
        if len(out) >= limit:
            break
    return out or None


def _enum_list(raw, allowed, limit=2):
    """把模型给的值收敛到受控枚举内（自创值直接丢掉，不猜）"""
    if not isinstance(raw, (list, tuple)):
        raw = [raw] if raw else []
    out = []
    for v in raw:
        v = str(v or "").strip()
        if v in allowed and v not in out:
            out.append(v)
    return out[:limit]


def _nice(track):
    """播报用的短名字（去编号、去码率后缀）"""
    return getattr(track, "pretty", None) or str(track)


async def run_tool(name, args, bridge):
    """执行一个工具，返回给模型看的摘要（尽量短，省 token）"""
    a = args or {}
    lib = bridge.library
    player = bridge.player
    speaker = bridge.speaker

    if name == "search_library":
        q = str(a.get("query") or "").strip()
        if not q:
            return {"ok": False, "error": "查询词为空"}
        tracks = lib.search(q, limit=_clip(a.get("limit"), 1, 20, 5))
        if not tracks:
            # 搜不到要当成失败回给模型，它才会改口说「没有」而不是继续硬播
            return {"ok": False, "error": "曲库里没有「%s」" % q}
        out = {"ok": True, "count": len(tracks),
               "tracks": [{"id": t.tid, "name": t.name, "artist": t.artist,
                           "genre": t.genre} for t in tracks[:5]]}
        if len(tracks) < WEAK_HITS:
            out["weak"] = True
            out["hint"] = "命中很少，可能不是用户要的那一类；可以 list_folders 看目录"
        return out

    if name == "search_by_tags":
        tg = getattr(bridge, "tagger", None)
        if not tg:
            return {"ok": False, "error": "画像模块没初始化"}
        want = {}
        moods = _enum_list(a.get("mood"), MOODS)
        if moods:
            want["mood"] = moods
        scenes = _enum_list(a.get("scene"), SCENES)
        if scenes:
            want["scene"] = scenes
        for key, allowed in (("tempo", TEMPOS), ("vocal", VOCALS), ("energy", ENERGIES)):
            v = str(a.get(key) or "").strip()
            if v in allowed:
                want[key] = v
        if not want:
            return {"ok": False,
                    "error": "没有可用的画像条件。如果用户说的是「年代」（八零年代、老歌）、"
                             "「语言」（英文歌）或「地区」这类，曲库里没有这些字段；"
                             "改用 list_artists 找具体歌手，或 list_folders 看目录，"
                             "都对不上就用 speak 直说没有这一类。"}
        limit = _clip(a.get("limit"), 3, 60, 20)
        # 先多取一些候选（最多 60），再交给模型按用户原话精排——
        # 画像只能保证「都在这一类里」，贴合度排序需要模型看曲名/歌手再判断
        tracks, dropped = tg.search(want, max(limit, 60))
        if not tracks:
            st = tg.status()
            return {"ok": False,
                    "error": "按这个画像没找到歌；已标画像 %d/%d 首，"
                             "可能这一类的歌还没标到，或换个说法再试"
                             % (st.get("covered", 0), st.get("total", 0))}
        ranked = False
        if len(tracks) > limit:
            desire = str(a.get("vibe") or "").strip() or _want_text(want)
            picked = await _rank_tagged(bridge, desire, tracks, limit)
            if picked:
                tracks = picked
                ranked = True
        # 精排过就按顺序播（最贴的在最前），没精排才随机
        await player.start_tracks(tracks, "seq" if ranked else "shuffle")
        out = {"ok": True, "count": len(tracks), "mode": "顺序" if ranked else "随机",
               "ranked": ranked,
               "understanding": str(a.get("note") or "").strip(),
               "first": _nice(tracks[0]),
               "picked": [_nice(t) for t in tracks[:5]]}
        if dropped:
            out["relaxed"] = dropped
            out["hint"] = "严格条件下歌太少，已放宽：%s" % "、".join(dropped)
        return out

    if name == "similar_to":
        tg = getattr(bridge, "tagger", None)
        if not tg:
            return {"ok": False, "error": "画像模块没初始化"}
        cur = None
        if 0 <= getattr(player, "cursor", -1) < len(getattr(player, "queue", []) or []):
            cur = player.queue[player.cursor]
        if not cur:
            return {"ok": False, "error": "现在没在播歌，不知道跟哪首像"}
        limit = _clip(a.get("limit"), 3, 60, 20)
        tracks = tg.similar(cur, limit)
        if not tracks:
            return {"ok": False,
                    "error": "《%s》还没有画像，或者库里没有相近的歌" % _nice(cur)}
        await player.start_tracks(tracks, "shuffle")
        return {"ok": True, "count": len(tracks), "mode": "随机",
                "based_on": _nice(cur), "first": _nice(tracks[0]),
                "picked": [_nice(t) for t in tracks[:5]]}

    if name == "pick_by_vibe":
        vibe = str(a.get("vibe") or "").strip()
        if not vibe:
            return {"ok": False, "error": "没给氛围描述"}
        limit = _clip(a.get("limit"), 3, 30, 15)
        genres = [str(x).strip() for x in (a.get("genres") or []) if str(x).strip()]
        # 候选池：按流派取，或全库随机抽 60 首，交给模型从里面挑
        pool = _vibe_pool(lib, genres, size=60)
        if not pool:
            return {"ok": False, "error": "曲库里没有可挑的歌"}
        picked = await _rank_by_vibe(bridge, vibe, pool, limit)
        if not picked:
            return {"ok": False, "error": "没能从候选里挑出符合「%s」的歌" % vibe}
        log.info("vibe 挑选 vibe=%s 候选=%d 选中=%d → %s", vibe, len(pool),
                 len(picked), "；".join(_nice(t) for t in picked[:6]))
        await player.start_tracks(picked, "shuffle")
        return {"ok": True, "count": len(picked), "vibe": vibe,
                "first": _nice(picked[0]),
                "picked": [_nice(t) for t in picked[:5]]}

    if name == "play_search":
        q = str(a.get("query") or "").strip()
        g = str(a.get("genre") or "").strip()
        mode = a.get("mode") if a.get("mode") in ("shuffle", "seq", "loop") else "shuffle"
        limit = _clip(a.get("limit"), 0, 60, 20)
        if g and not q:
            tracks = lib.pick_genre(g, mode, limit or 20)
            if not tracks:
                return {"ok": False, "error": "曲库里没有「%s」这个流派的歌" % g}
        else:
            tracks = lib.pick(q, mode, limit) if q else lib.random_pick(limit or 20)
        if not tracks:
            return {"ok": False, "error": "曲库里没有匹配的歌" if q else "曲库是空的"}
        await player.start_tracks(tracks, mode)
        out = {"ok": True, "count": len(tracks), "mode": MODE_NAMES[mode],
               "first": _nice(tracks[0]),
               "understanding": str(a.get("note") or "").strip()}
        if q and len(tracks) < WEAK_HITS:
            out["weak"] = True
            out["hint"] = ("只按名字撞到 %d 首，可能不是用户要的那一类；"
                           "可以先 list_folders 看有没有更像的目录" % len(tracks))
        return out

    if name == "list_genres":
        items = lib.genres()[:25]
        total = sum(c for _, c in lib.genres())
        return {"ok": True, "covered": total, "total": len(lib.tracks),
                "genres": [{"name": k, "count": v} for k, v in items]}

    if name == "list_folders":
        prefix = str(a.get("prefix") or "").strip()
        return _list_folders(bridge, prefix)

    if name == "resolve_names":
        names = [str(x).strip() for x in (a.get("names") or []) if str(x).strip()][:8]
        if not names:
            return {"ok": False, "error": "没给歌手名"}
        lib.scan()
        counts = {}
        for t in lib.tracks:
            nm = (t.artist or "").strip()
            if nm:
                counts[nm] = counts.get(nm, 0) + 1
        out = []
        for n in names:
            low = n.lower()
            hit = [(k, v) for k, v in counts.items() if low in k.lower()]
            hit.sort(key=lambda kv: -kv[1])
            out.append({"name": n,
                        "count": sum(v for _, v in hit),
                        "artists": [{"name": k, "count": v} for k, v in hit[:3]]})
        total = sum(x["count"] for x in out)
        res = {"ok": True, "total": total, "names": out}
        if total == 0:
            res["hint"] = ("这些名字库里都没有；也许用户指的是别的写法，"
                           "或者换个方向（目录、流派、画像）再找")
        return res

    if name == "list_artists":
        q = str(a.get("query") or "").strip().lower()
        limit = _clip(a.get("limit"), 1, 50, 20)
        lib.scan()
        counts = {}
        for t in lib.tracks:
            nm = (t.artist or "").strip()
            if not nm:
                continue
            if q and q not in nm.lower():
                continue
            counts[nm] = counts.get(nm, 0) + 1
        items = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
        return {"ok": True, "count": len(items),
                "artists": [{"name": k, "count": v} for k, v in items]}

    if name == "play_folder":
        path = str(a.get("path") or "").strip()
        roots = _roots(bridge)
        if not path or not _under_roots(path, roots):
            return {"ok": False, "error": "路径不在曲库目录里"}
        mode = a.get("mode") if a.get("mode") in ("shuffle", "seq", "loop") else "shuffle"
        limit = _clip(a.get("limit"), 0, 60, 20)
        tracks = lib.search_in(path, "", limit, shuffle=(mode == "shuffle"))
        if not tracks:
            return {"ok": False, "error": "这个文件夹里没有歌"}
        await player.start_tracks(tracks, mode)
        return {"ok": True, "count": len(tracks), "mode": MODE_NAMES[mode],
                "first": _nice(tracks[0])}

    if name == "play_track":
        t = lib.get(str(a.get("id") or ""))
        if not t:
            return {"ok": False, "error": "曲库里没有这个 id"}
        await player.start_tracks([t], "one")
        return {"ok": True, "first": _nice(t)}

    if name == "control":
        act = str(a.get("action") or "")
        table = {"pause": ("pause", "暂停了"), "resume": ("resume", "接着放"),
                 "next": ("next", "下一首"), "prev": ("prev", "上一首"),
                 "stop": ("stop", "停了")}
        if act not in table:
            return {"ok": False, "error": "不认识的控制动作"}
        fn, word = table[act]
        # pause/resume/stop 收 reason 参数，next/prev 不收，统一传参会 TypeError
        if act in ("pause", "resume", "stop"):
            await getattr(player, fn)("语音")
        else:
            await getattr(player, fn)()
        return {"ok": True, "done": word}

    if name == "set_mode":
        mode = str(a.get("mode") or "")
        if mode not in ("shuffle", "seq", "loop"):
            return {"ok": False, "error": "模式只能是 shuffle/seq/loop"}
        await player.set_mode(mode)
        return {"ok": True, "mode": MODE_NAMES[mode]}

    if name == "set_volume":
        if a.get("value") is not None:
            target = _clip(a.get("value"), 0, 100, 40)
        elif a.get("delta") is not None:
            st = await speaker.get_state(ttl=2.0)
            cur = st.get("volume")
            if cur is None:
                return {"ok": False, "error": "读不到当前音量"}
            target = _clip(int(cur) + _clip(a.get("delta"), -50, 50, 0), 0, 100, int(cur))
        else:
            return {"ok": False, "error": "要么给 value，要么给 delta"}
        r = await speaker.set_volume(target)
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error") or "设置失败"}
        return {"ok": True, "volume": target}

    if name == "speak":
        text = str(a.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "没有要说的内容"}
        return {"ok": True, "spoken": text}

    return {"ok": False, "error": "没有这个工具"}


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False)
